from ._private.address import get_bus_address, parse_address
from ._private.util import replace_fds_with_idx, replace_idx_with_fds
from ._private.unmarshaller import Unmarshaller
from .message import Message
from .constants import BusType, MessageFlag, MessageType, ErrorType, NameFlag, RequestNameReply, ReleaseNameReply
from .service import ServiceInterface
from .validators import assert_object_path_valid, assert_bus_name_valid
from .errors import DBusError, InvalidAddressError
from .signature import Variant
from .proxy_object import ProxyObject
from . import introspection as intr
from contextlib import suppress
from .auth import Authenticator, AuthExternal

import inspect
import socket
import logging
import xml.etree.ElementTree as ET
import traceback
import attr
import anyio
import outcome
import array
from contextlib import asynccontextmanager
from concurrent.futures import CancelledError
from copy import copy

from typing import Type, Callable, Optional, Union


class MessageBus:
    """The message bus implementation for use with anyio.

    The message bus class is the entry point into all the features of the
    library. It sets up a connection to the DBus daemon and exposes an
    interface to send and receive messages and expose services.

    This class is not meant to be used directly by users. For more information,
    see the documentation for the implementation of the message bus you plan to
    use.

    :param bus_type: The type of bus to connect to. Affects the search path for
        the bus address.
    :type bus_type: :class:`BusType <asyncdbus.BusType>`
    :param bus_address: A specific bus address to connect to. Should not be
        used under normal circumstances.
    :type bus_address: str

    :ivar unique_name: The unique name of the message bus connection. It will
        be :class:`None` until the message bus connects.
    :vartype unique_name: str
    :ivar connected: True if this message bus is expected to be able to send
        and receive messages.
    :vartype connected: bool
    """

    def __init__(self,
                 bus_address: Optional[str] = None,
                 bus_type: BusType = BusType.SESSION,
                 auth: Authenticator = None,
                 negotiate_unix_fd: bool = False):
        self.unique_name = None
        self._disconnected = True

        self._method_return_handlers = {}
        self._serial = 0
        self._user_message_handlers = []
        # the key is the name and the value is the unique name of the owner.
        # This cache is kept up to date by the NameOwnerChanged signal and is
        # used to route messages to the correct proxy object. (used for the
        # high level client only)
        self._name_owners = {}
        # used for the high level service
        self._path_exports = {}
        self._bus_address = parse_address(bus_address) if bus_address else parse_address(
            get_bus_address(bus_type))
        # the bus implementations need this rule for the high level client to
        # work correctly.
        self._name_owner_match_rule = "sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',path='/org/freedesktop/DBus',member='NameOwnerChanged'"
        # _match_rules: the keys are match rules and the values are ref counts
        # (used for the high level client only)
        self._match_rules = {}
        self._high_level_client_initialized = False
        self._ProxyObject = ProxyObject

        # machine id is lazy loaded
        self._machine_id = None

        # anyio support
        self._negotiate_unix_fd = negotiate_unix_fd

        self._reader = None
        self._writer = None
        self._tg = None
        self._write_lock = anyio.create_lock()

        if auth is None:
            self._auth = AuthExternal()
        else:
            self._auth = auth

    @property
    def connected(self):
        if self.unique_name is None or self._disconnected:
            return False
        return True

    async def export(self, path: str, interface: ServiceInterface):
        """Export the service interface on this message bus to make it available
        to other clients.

        :param path: The object path to export this interface on.
        :type path: str
        :param interface: The service interface to export.
        :type interface: :class:`ServiceInterface
            <asyncdbus.service.ServiceInterface>`

        :raises:
            - :class:`InvalidObjectPathError <asyncdbus.InvalidObjectPathError>` - If the given object path is not valid.
            - :class:`ValueError` - If an interface with this name is already exported on the message bus at this path
        """
        assert_object_path_valid(path)
        if not isinstance(interface, ServiceInterface):
            raise TypeError('interface must be a ServiceInterface')

        if path not in self._path_exports:
            self._path_exports[path] = []

        for f in self._path_exports[path]:
            if f.name == interface.name:
                raise ValueError(
                    f'An interface with this name is already exported on this bus at path "{path}": "{interface.name}"'
                )

        self._path_exports[path].append(interface)
        ServiceInterface._add_bus(interface, self)
        await self._emit_interface_added(path, interface)

    async def unexport(self, path: str, interface: Optional[Union[ServiceInterface, str]] = None):
        """Unexport the path or service interface to make it no longer
        available to clients.

        :param path: The object path to unexport.
        :type path: str
        :param interface: The interface instance or the name of the interface
            to unexport. If ``None``, unexport every interface on the path.
        :type interface: :class:`ServiceInterface
            <asyncdbus.service.ServiceInterface>` or str or None

        :raises:
            - :class:`InvalidObjectPathError <asyncdbus.InvalidObjectPathError>` - If the given object path is not valid.
        """
        assert_object_path_valid(path)
        if type(interface) not in [str, type(None)] and not isinstance(interface, ServiceInterface):
            raise TypeError('interface must be a ServiceInterface or interface name')

        if path not in self._path_exports:
            return

        exports = self._path_exports[path]

        if type(interface) is str:
            try:
                interface = next(iface for iface in exports if iface.name == interface)
            except StopIteration:
                return

        removed_interfaces = []
        if interface is None:
            del self._path_exports[path]
            for iface in filter(lambda e: not self._has_interface(e), exports):
                removed_interfaces.append(iface.name)
                ServiceInterface._remove_bus(iface, self)
        else:
            for i, iface in enumerate(exports):
                if iface is interface:
                    removed_interfaces.append(iface.name)
                    del self._path_exports[path][i]
                    if not self._path_exports[path]:
                        del self._path_exports[path]
                    if not self._has_interface(iface):
                        ServiceInterface._remove_bus(iface, self)
                    break
        await self._emit_interface_removed(path, removed_interfaces)

    async def introspect(self, bus_name: str, path: str, timeout: float = 30) -> intr.Node:
        """Get introspection data for the node at the given path from the given
        bus name.

        Calls the standard ``org.freedesktop.DBus.Introspectable.Introspect``
        on the bus for the path.

        :param bus_name: The name to introspect.
        :type bus_name: str
        :param path: The path to introspect.
        :type path: str
        :param timeout: The timeout to introspect.
        :type timeout: float

        :raises:
            - :class:`InvalidObjectPathError <asyncdbus.InvalidObjectPathError>` - If the given object path is not valid.
            - :class:`InvalidBusNameError <asyncdbus.InvalidBusNameError>` - If the given bus name is not valid.
        """
        with anyio.fail_after(timeout):
            reply = await self.call(
                Message(
                    destination=bus_name,
                    path=path,
                    interface='org.freedesktop.DBus.Introspectable',
                    member='Introspect'))

        self._check_method_return(reply, None, 's')
        return intr.Node.parse(reply.body[0])

    async def _emit_interface_added(self, path, interface):
        """Emit the ``org.freedesktop.DBus.ObjectManager.InterfacesAdded`` signal.

        This signal is intended to be used to alert clients when
        a new interface has been added.

        :param path: Path of exported object.
        :type path: str
        :param interface: Exported service interface.
        :type interface: :class:`ServiceInterface
            <asyncdbus.service.ServiceInterface>`
        """
        if self._disconnected:
            return

        body = {interface.name: {}}
        properties = interface._get_properties(interface)

        for prop in properties:
            with suppress(Exception):
                body[interface.name][prop.name] = Variant(prop.signature,
                                                          prop.prop_getter(interface))

        await self.send(
            Message.new_signal(
                path=path,
                interface='org.freedesktop.DBus.ObjectManager',
                member='InterfacesAdded',
                signature='oa{sa{sv}}',
                body=[path, body]))

    async def _emit_interface_removed(self, path, removed_interfaces):
        """Emit the ``org.freedesktop.DBus.ObjectManager.InterfacesRemoved` signal.

        This signal is intended to be used to alert clients when
        a interface has been removed.

        :param path: Path of removed (unexported) object.
        :type path: str
        :param removed_interfaces: List of unexported service interfaces.
        :type removed_interfaces: list[str]
        """
        if self._disconnected:
            return

        await self.send(
            Message.new_signal(
                path=path,
                interface='org.freedesktop.DBus.ObjectManager',
                member='InterfacesRemoved',
                signature='oas',
                body=[path, removed_interfaces]))

    async def request_name(self, name: str, flags: NameFlag = NameFlag.NONE):
        """Request that this message bus owns the given name.

        :param name: The name to request.
        :type name: str
        :param flags: Name flags that affect the behavior of the name request.
        :type flags: :class:`NameFlag <asyncdbus.NameFlag>`
        :param callback: A callback that will be called with the reply of the
            request as a :class:`RequestNameReply <asyncdbus.RequestNameReply>`.
        :type callback: :class:`Callable`

        :raises:
            - :class:`InvalidBusNameError <asyncdbus.InvalidBusNameError>` - If the given bus name is not valid.
        """
        assert_bus_name_valid(name)

        if type(flags) is not NameFlag:
            flags = NameFlag(flags)

        reply = await self.call(
            Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='RequestName',
                signature='su',
                body=[name, flags]))

        self._check_method_return(reply, None, 'u')
        return RequestNameReply(reply.body[0])

    async def release_name(self, name: str):
        """Request that this message bus release the given name.

        :param name: The name to release.
        :type name: str
        :param callback: A callback that will be called with the reply of the
            release request as a :class:`ReleaseNameReply
            <asyncdbus.ReleaseNameReply>`.
        :type callback: :class:`Callable`

        :raises:
            - :class:`InvalidBusNameError <asyncdbus.InvalidBusNameError>` - If the given bus name is not valid.
        """
        assert_bus_name_valid(name)

        reply = await self.call(
            Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='ReleaseName',
                signature='s',
                body=[name]))

        self._check_method_return(reply, None, 'u')
        return ReleaseNameReply(reply.body[0])

    async def get_proxy_object(self, bus_name: str, path: str,
                               introspection: Union[intr.Node, str, ET.Element]) -> ProxyObject:
        """Get a proxy object for the path exported on the bus that owns the
        name. The object is expected to export the interfaces and nodes
        specified in the introspection data.

        This is the entry point into the high-level client.

        :param bus_name: The name on the bus to get the proxy object for.
        :type bus_name: str
        :param path: The path on the client for the proxy object.
        :type path: str
        :param introspection: XML introspection data used to build the
            interfaces on the proxy object.
        :type introspection: :class:`Node <asyncdbus.introspection.Node>` or str or :class:`ElementTree`

        :returns: A proxy object for the given path on the given name.
        :rtype: :class:`ProxyObject <asyncdbus.proxy_object.ProxyObject>`

        :raises:
            - :class:`InvalidBusNameError <asyncdbus.InvalidBusNameError>` - If the given bus name is not valid.
            - :class:`InvalidObjectPathError <asyncdbus.InvalidObjectPathError>` - If the given object path is not valid.
            - :class:`InvalidIntrospectionError <asyncdbus.InvalidIntrospectionError>` - If the introspection data for the node is not valid.
        """
        if self._ProxyObject is None:
            raise Exception('the message bus implementation did not provide a proxy object class')

        await self._init_high_level_client()

        return self._ProxyObject(bus_name, path, introspection, self)

    def next_serial(self) -> int:
        """Get the next serial for this bus. This can be used as the ``serial``
        attribute of a :class:`Message <asyncdbus.Message>` to manually handle
        the serial of messages.

        :returns: The next serial for the bus.
        :rtype: int
        """
        self._serial += 1
        return self._serial

    def add_message_handler(self, handler: Callable[[Message], Optional[Union[Message, bool]]]):
        """Add a custom message handler for incoming messages.

        The handler should be a callable that takes a :class:`Message
        <asyncdbus.Message>`. If the message is a method call, you may return
        another Message as a reply and it will be marked as handled. You may
        also return ``True`` to mark the message as handled without sending a
        reply.

        :param handler: A handler that will be run for every message the bus
            connection received.
        :type handler: :class:`Callable` or None
        """
        error_text = 'a message handler must be callable with a single parameter'
        if not callable(handler):
            raise TypeError(error_text)

        handler_signature = inspect.signature(handler)
        if len(handler_signature.parameters) != 1:
            raise TypeError(error_text)

        self._user_message_handlers.append(handler)

    def remove_message_handler(self, handler: Callable[[Message], Optional[Union[Message, bool]]]):
        """Remove a message handler that was previously added by
        :func:`add_message_handler()
        <asyncdbus.message_bus.MessageBus.add_message_handler>`.

        :param handler: A message handler.
        :type handler: :class:`Callable`
        """
        self._user_message_handlers.remove(handler)

    async def _finalize(self):
        '''should be called after the socket disconnects with the disconnection
        error to clean up resources and put the bus in a disconnected state'''
        if self._disconnected:
            return

        self._disconnected = True

        if self._reader is not None:
            self._reader.cancel()
        if self._writer is not None:
            self._writer.cancel()
        if self._tg is not None:
            self._tg.cancel_scope.cancel()
        self._write = None

        sock, self._sock = self._sock, None
        sock.close()

        for handler in self._method_return_handlers.values():
            try:
                handler(None, CancelledError())
            except Exception:
                logging.warning('a message handler threw an exception on shutdown', exc_info=True)

        self._method_return_handlers.clear()

        for path in list(self._path_exports.keys()):
            await self.unexport(path)

        self._user_message_handlers.clear()

    def _has_interface(self, interface: ServiceInterface) -> bool:
        for _, exports in self._path_exports.items():
            for iface in exports:
                if iface is interface:
                    return True

        return False

    async def _interface_signal_notify(self,
                                       interface,
                                       interface_name,
                                       member,
                                       signature,
                                       body,
                                       unix_fds=[]):
        path = None
        for p, ifaces in self._path_exports.items():
            for i in ifaces:
                if i is interface:
                    path = p

        if path is None:
            raise Exception('Could not find interface on bus (this is a bug in dbus-next)')

        await self.send(
            Message.new_signal(
                path=path,
                interface=interface_name,
                member=member,
                signature=signature,
                body=body,
                unix_fds=unix_fds))

    def _introspect_export_path(self, path):
        assert_object_path_valid(path)

        if path in self._path_exports:
            node = intr.Node.default(path)
            for interface in self._path_exports[path]:
                node.interfaces.append(interface.introspect())
        else:
            node = intr.Node(path)

        children = set()

        for export_path in self._path_exports:
            try:
                child_path = export_path.split(path, maxsplit=1)[1]
            except IndexError:
                continue

            child_path = child_path.lstrip('/')
            child_name = child_path.split('/', maxsplit=1)[0]

            children.add(child_name)

        node.nodes = [intr.Node(name) for name in children if name]

        return node

    def _setup_socket(self):
        err = None

        for transport, options in self._bus_address:
            filename = None
            ip_addr = ''
            ip_port = 0

            if transport == 'unix':
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

                if 'path' in options:
                    filename = options['path']
                elif 'abstract' in options:
                    filename = f'\0{options["abstract"]}'
                else:
                    raise InvalidAddressError('got unix transport with unknown path specifier')

                try:
                    self._sock.connect(filename)
                    self._sock.setblocking(False)
                    break
                except Exception as e:
                    err = e

            elif transport == 'tcp':
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

                if 'host' in options:
                    ip_addr = options['host']
                if 'port' in options:
                    ip_port = int(options['port'])

                try:
                    self._sock.connect((ip_addr, ip_port))
                    self._sock.setblocking(False)
                    break
                except Exception as e:
                    err = e

            else:
                raise InvalidAddressError(f'got unknown address transport: {transport}')

        if err:
            raise err

    async def call(self, msg):
        """Send a method call and wait for a reply from the DBus daemon.

        :param msg: The method call message to send.
        :type msg: :class:`Message <asyncdbus.Message>`

        :returns: A message in reply to the message sent. If the message does
            not expect a reply based on the message flags or type, returns
            ``None`` after the message is sent.
        :rtype: :class:`Message <asyncdbus.Message>` or :class:`None` if no reply is expected.

        :raises:
            - :class:`Exception` - If a connection error occurred.
        """
        if msg.message_type is MessageType.METHOD_CALL and not msg.serial:
            msg.serial = self.next_serial()

        if msg.flags & MessageFlag.NO_REPLY_EXPECTED or msg.message_type is not MessageType.METHOD_CALL:
            await self.send(msg)
            return

        result = None
        evt = anyio.create_event()

        def reply_notify(reply, err):
            nonlocal result, evt
            if err:
                result = outcome.Error(err)
            else:
                result = outcome.Value(reply)
            if reply:
                self._name_owners[msg.destination] = reply.sender
            evt.set()

        self._method_return_handlers[msg.serial] = reply_notify
        await self.send(msg)
        await evt.wait()
        res = result.unwrap()
        if res:
            self._name_owners[msg.destination] = res.sender
        self._check_method_return(res)
        return res

    @staticmethod
    def _check_callback_type(callback):
        """Raise a TypeError if the user gives an invalid callback as a parameter"""

        text = 'a callback must be callable with two parameters'

        if not callable(callback):
            raise TypeError(text)

        fn_signature = inspect.signature(callback)
        if len(fn_signature.parameters) != 2:
            raise TypeError(text)

    @staticmethod
    def _check_method_return(msg, err=None, signature=None):
        if err:
            raise err
        elif msg.message_type == MessageType.METHOD_RETURN and (signature is None
                                                                or msg.signature == signature):
            return
        elif msg.message_type == MessageType.ERROR:
            raise DBusError._from_message(msg)
        else:
            raise DBusError(ErrorType.INTERNAL_ERROR, 'invalid message type for method call', msg)

    async def _on_message(self, msg):
        await self._process_message(msg)

    def _send_reply(self, msg):
        bus = self

        class SendReply:
            def __enter__(self):
                return self

            def __call__(self, reply):
                if msg.flags & MessageFlag.NO_REPLY_EXPECTED:
                    return

                bus.send_soon(reply)

            def __exit__(self, exc_type, exc_value, tb):
                if exc_type is None:
                    return

                if issubclass(exc_type, DBusError):
                    self(exc_value._as_message(msg))
                    return True

                if issubclass(exc_type, Exception):
                    self(
                        Message.new_error(
                            msg, ErrorType.SERVICE_ERROR,
                            f'The service interface raised an error: {exc_value}.\n{traceback.format_tb(tb)}'
                        ))
                    return True

        return SendReply()

    async def _process_message(self, msg):
        handled = False

        for handler in self._user_message_handlers:
            try:
                result = handler(msg)
                if inspect.iscoroutine(result):
                    result = await result
                if result:
                    if type(result) is Message:
                        await self.send(result)
                    handled = True
                    break
            except DBusError as e:
                if msg.message_type == MessageType.METHOD_CALL:
                    await self.send(e._as_message(msg))
                    handled = True
                raise
            except Exception as e:
                if msg.message_type == MessageType.METHOD_CALL:
                    await self.send(
                        Message.new_error(
                            msg, ErrorType.INTERNAL_ERROR,
                            f'An internal error occurred: {e}.\n{traceback.format_exc()}'))
                    handled = True
                raise

        if msg.message_type == MessageType.SIGNAL:
            if msg._matches(
                    sender='org.freedesktop.DBus',
                    path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus',
                    member='NameOwnerChanged'):
                [name, old_owner, new_owner] = msg.body
                if new_owner:
                    self._name_owners[name] = new_owner
                elif name in self._name_owners:
                    del self._name_owners[name]

        elif msg.message_type == MessageType.METHOD_CALL:
            if not handled:
                handler = self._find_message_handler(msg)

                send_reply = self._send_reply(msg)

                with send_reply:
                    if handler:
                        res = handler(msg, send_reply)
                        if inspect.iscoroutine(res):
                            res = await res
                    else:
                        send_reply(
                            Message.new_error(
                                msg, ErrorType.UNKNOWN_METHOD,
                                f'{msg.interface}.{msg.member} with signature "{msg.signature}" could not be found'
                            ))

        else:
            # An ERROR or a METHOD_RETURN
            if msg.reply_serial in self._method_return_handlers:
                if not handled:
                    handler = self._method_return_handlers[msg.reply_serial]
                    handler(msg, None)
                del self._method_return_handlers[msg.reply_serial]

    @staticmethod
    def _make_method_handler(interface, method):
        def handler(msg, send_reply):
            args = ServiceInterface._msg_body_to_args(msg)
            result = method.fn(interface, *args)
            body, fds = ServiceInterface._fn_result_to_body(
                result, signature_tree=method.out_signature_tree)
            send_reply(Message.new_method_return(msg, method.out_signature, body, fds))

        return handler

    def _find_message_handler(self, msg):
        handler = None

        if msg._matches(
                interface='org.freedesktop.DBus.Introspectable', member='Introspect', signature=''):
            return self._default_introspect_handler

        elif msg._matches(interface='org.freedesktop.DBus.Properties'):
            return self._default_properties_handler

        elif msg._matches(interface='org.freedesktop.DBus.Peer'):
            if msg._matches(member='Ping', signature=''):
                return self._default_ping_handler
            elif msg._matches(member='GetMachineId', signature=''):
                return self._default_get_machine_id_handler
        elif msg._matches(
                interface='org.freedesktop.DBus.ObjectManager', member='GetManagedObjects'):
            return self._default_get_managed_objects_handler

        else:
            for interface in self._path_exports.get(msg.path, []):
                for method in ServiceInterface._get_methods(interface):
                    if method.disabled:
                        continue
                    if msg._matches(
                            interface=interface.name, member=method.name,
                            signature=method.in_signature):
                        return self._make_method_handler(interface, method)

        return None

    def _default_introspect_handler(self, msg, send_reply):
        introspection = self._introspect_export_path(msg.path).tostring()
        send_reply(Message.new_method_return(msg, 's', [introspection]))

    def _default_ping_handler(self, msg, send_reply):
        send_reply(Message.new_method_return(msg))

    async def _default_get_machine_id_handler(self, msg, send_reply):
        if self._machine_id:
            send_reply(Message.new_method_return(msg, 's', self._machine_id))
            return

        try:
            reply = await self.call(
                Message(
                    destination='org.freedesktop.DBus',
                    path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus.Peer',
                    member='GetMachineId'))
        except DBusError as err:
            send_reply(Message.new_error(msg, err.reply.error_name, err.reply.body))
        else:
            if reply.message_type == MessageType.METHOD_RETURN:
                self._machine_id = reply.body[0]
                send_reply(Message.new_method_return(msg, 's', [self._machine_id]))
            else:
                send_reply(Message.new_error(msg, ErrorType.FAILED, 'could not get machine_id'))

    def _default_get_managed_objects_handler(self, msg, send_reply):
        result = {}

        for node in self._path_exports:
            if not node.startswith(msg.path + '/') and msg.path != '/':
                continue

            result[node] = {}
            for interface in self._path_exports[node]:
                result[node][interface.name] = self._get_all_properties(interface)

        send_reply(Message.new_method_return(msg, 'a{oa{sa{sv}}}', [result]))

    def _default_properties_handler(self, msg, send_reply):
        methods = {'Get': 'ss', 'Set': 'ssv', 'GetAll': 's'}
        if msg.member not in methods or methods[msg.member] != msg.signature:
            raise DBusError(
                ErrorType.UNKNOWN_METHOD,
                f'properties interface doesn\'t have method "{msg.member}" with signature "{msg.signature}"'
            )

        interface_name = msg.body[0]
        if interface_name == '':
            raise DBusError(
                ErrorType.NOT_SUPPORTED,
                'getting and setting properties with an empty interface string is not supported yet'
            )

        elif msg.path not in self._path_exports:
            raise DBusError(ErrorType.UNKNOWN_OBJECT, f'no interfaces at path: "{msg.path}"')

        match = [iface for iface in self._path_exports[msg.path] if iface.name == interface_name]
        if not match:
            if interface_name in [
                    'org.freedesktop.DBus.Properties', 'org.freedesktop.DBus.Introspectable',
                    'org.freedesktop.DBus.Peer', 'org.freedesktop.DBus.ObjectManager'
            ]:
                # the standard interfaces do not have properties
                if msg.member == 'Get' or msg.member == 'Set':
                    prop_name = msg.body[1]
                    raise DBusError(
                        ErrorType.UNKNOWN_PROPERTY,
                        f'interface "{interface_name}" does not have property "{prop_name}"')
                elif msg.member == 'GetAll':
                    send_reply(Message.new_method_return(msg, 'a{sv}', [{}]))
                    return
                else:
                    assert False
            raise DBusError(
                ErrorType.UNKNOWN_INTERFACE,
                f'could not find an interface "{interface_name}" at path: "{msg.path}"')

        interface = match[0]
        properties = ServiceInterface._get_properties(interface)

        if msg.member == 'Get' or msg.member == 'Set':
            prop_name = msg.body[1]
            match = [prop for prop in properties if prop.name == prop_name and not prop.disabled]
            if not match:
                raise DBusError(
                    ErrorType.UNKNOWN_PROPERTY,
                    f'interface "{interface_name}" does not have property "{prop_name}"')

            prop = match[0]
            if msg.member == 'Get':
                if not prop.access.readable():
                    raise DBusError(ErrorType.UNKNOWN_PROPERTY,
                                    'the property does not have read access')
                prop_value = getattr(interface, prop.prop_getter.__name__)

                body, unix_fds = replace_fds_with_idx(prop.signature, [prop_value])

                send_reply(
                    Message.new_method_return(
                        msg, 'v', [Variant(prop.signature, body[0])], unix_fds=unix_fds))
            elif msg.member == 'Set':
                if not prop.access.writable():
                    raise DBusError(ErrorType.PROPERTY_READ_ONLY, 'the property is readonly')
                value = msg.body[2]
                sig = prop.signature
                if hasattr(sig, 'tree'):
                    sig = sig.tree.signature
                if value.signature != sig:
                    raise DBusError(ErrorType.INVALID_SIGNATURE,
                                    f'wrong signature for property. expected "{prop.signature}"')
                assert prop.prop_setter
                body = replace_idx_with_fds(value.signature, [value.value], msg.unix_fds)
                setattr(interface, prop.prop_setter.__name__, body[0])
                send_reply(Message.new_method_return(msg))

        elif msg.member == 'GetAll':
            body, unix_fds = replace_fds_with_idx('a{sv}', [self._get_all_properties(interface)])
            send_reply(Message.new_method_return(msg, 'a{sv}', body, unix_fds=unix_fds))

        else:
            assert False

    def _get_all_properties(self, interface):
        result = {}

        for prop in ServiceInterface._get_properties(interface):
            if prop.disabled or not prop.access.readable():
                continue
            result[prop.name] = Variant(prop.signature, getattr(interface,
                                                                prop.prop_getter.__name__))

        return result

    async def _init_high_level_client(self):
        '''The high level client is initialized when the first proxy object is
        gotten. Currently just sets up the match rules for the name owner cache
        so signals can be routed to the right objects.'''
        if self._high_level_client_initialized:
            return
        self._high_level_client_initialized = True

        msg = await self.call(
            Message(
                destination='org.freedesktop.DBus',
                interface='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                member='AddMatch',
                signature='s',
                body=[self._name_owner_match_rule]))

        if msg.message_type == MessageType.ERROR:
            raise DBusError._from_message(msg)

    async def _add_match_rule(self, match_rule):
        '''Add a match rule. Match rules added by this function are refcounted
        and must be removed by _remove_match_rule(). This is for use in the
        high level client only.'''
        if match_rule == self._name_owner_match_rule:
            return

        if match_rule in self._match_rules:
            self._match_rules[match_rule] += 1
            return

        self._match_rules[match_rule] = 1

        await self.call(
            Message(
                destination='org.freedesktop.DBus',
                interface='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                member='AddMatch',
                signature='s',
                body=[match_rule]))

    async def _remove_match_rule(self, match_rule):
        '''Remove a match rule added with _add_match_rule(). This is for use in
        the high level client only.'''
        if match_rule == self._name_owner_match_rule:
            return

        if match_rule in self._match_rules:
            self._match_rules[match_rule] -= 1
            if self._match_rules[match_rule] > 0:
                return

        del self._match_rules[match_rule]

        await self.call(
            Message(
                destination='org.freedesktop.DBus',
                interface='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                member='RemoveMatch',
                signature='s',
                body=[match_rule]))

    async def _setup_socket_aio(self):
        err = None

        for transport, options in self._bus_address:

            if transport == 'unix':
                if 'path' in options:
                    filename = options['path']
                elif 'abstract' in options:
                    filename = f'\0{options["abstract"]}'
                else:
                    raise InvalidAddressError('got unix transport with unknown path specifier')

                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
                    sock.setblocking(False)
                    sock.connect(filename)
                except (BlockingIOError, InterruptedError):
                    await anyio.wait_socket_writable(sock)
                    e = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if e:
                        if err is None:
                            err = OSError(err, f'Connect failed for {filename}')
                    else:
                        break
                except Exception as e:
                    if err is None:
                        err = e
                else:
                    break

            elif transport == 'tcp':
                ip_addr = '127.0.0.1'
                ip_port = 0

                if 'host' in options:
                    ip_addr = options['host']
                if 'port' in options:
                    ip_port = int(options['port'])

                try:
                    sock = socket.create_connection((ip_addr, ip_port))
                    sock.setblocking(False)
                except (BlockingIOError, InterruptedError):
                    await anyio.wait_socket_writable(sock)
                    e = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if e:
                        if err is None:
                            err = OSError(err, f'Connect failed for {filename}')
                    else:
                        break
                except Exception as e:
                    if err is None:
                        err = e
                else:
                    break

            else:
                raise InvalidAddressError(f'got unknown address transport: {transport}')

        if err:
            raise err

        self._sock = sock
        self._fd = self._sock.fileno()

    @asynccontextmanager
    async def connect(self) -> 'MessageBus':
        """Connect this message bus to the DBus daemon.

        This method is an async context manager.

        :returns: This message bus for convenience.
        :rtype: :class:`MessageBus <asyncdbus.MessageBus>`

        :raises:
            - :class:`AuthError <asyncdbus.AuthError>` - If authorization to \
              the DBus daemon failed.
            - :class:`Exception` - If there was a connection error.
        """
        if not self._disconnected:
            raise RuntimeError("You can't connect twice")
        await self._setup_socket_aio()
        evt = anyio.create_event()

        async with anyio.create_task_group() as tg:
            self._tg = tg
            await self._authenticate()

            self._write, self._write_r = anyio.create_memory_object_stream(10)
            self._reader = await tg.start(self._message_reader)

            def on_hello(reply, err):
                if err:
                    raise err
                self.unique_name = reply.body[0]
                evt.set()

            hello_msg = Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='Hello',
                serial=self.next_serial())

            self._method_return_handlers[hello_msg.serial] = on_hello
            await anyio.wait_socket_writable(self._sock)
            self._sock.send(hello_msg._marshall())

            await evt.wait()
            self._writer = await tg.start(self._message_writer)

            self._disconnected = False
            try:
                yield self
            finally:
                await self._finalize()
            tg.cancel_scope.cancel()
            pass  # close TG

    async def send(self, msg: Message):
        """Asynchronously send a message on the message bus.

        This method is a coroutine which returns when the message is sent
        successfully. Use `send_soon` if you need a sync version.

        :param msg: The message to send.
        :type msg: :class:`Message <asyncdbus.Message>`

        :returns: Nothing.
        """
        if not msg.serial:
            msg.serial = self.next_serial()

        await self._write_one(
            msg._marshall(negotiate_unix_fd=self._negotiate_unix_fd), copy(msg.unix_fds))

    def send_soon(self, msg: Message):
        """Queue a message on the message bus for transmission.

        This method is not a coroutine and should not be used unless
        absolutely necessary. An async version that waits for transmission
        to be completed is `send`.

        :param msg: The message to send.
        :type msg: :class:`Message <asyncdbus.Message>`

        :returns: Nothing.
        """
        if not msg.serial:
            msg.serial = self.next_serial()

        self._write.send_nowait((msg._marshall(negotiate_unix_fd=self._negotiate_unix_fd),
                                 copy(msg.unix_fds)))

    def _make_method_handler(self, interface, method):
        async def handler(msg, send_reply):
            args = ServiceInterface._msg_body_to_args(msg)
            try:
                result = method.fn(interface, *args)
                if inspect.iscoroutine(result):
                    result = await result

            except DBusError as e:
                if msg.message_type != MessageType.METHOD_CALL:
                    raise
                send_reply(e._as_message(msg))

            except Exception as e:
                if msg.message_type != MessageType.METHOD_CALL:
                    raise
                send_reply(
                    Message.new_error(
                        msg, ErrorType.SERVICE_ERROR,
                        f'An internal error occurred: {e}.\n{traceback.format_exc()}'))
            else:
                body, fds = ServiceInterface._fn_result_to_body(
                    result, signature_tree=method.out_signature_tree)
                send_reply(Message.new_method_return(msg, method.out_signature, body, fds))

        def _handler(msg, send_reply):
            self._tg.start_soon(handler, msg, send_reply)

        return _handler

    async def _message_reader(self, *, task_status):
        unmarshaller = Unmarshaller()
        with anyio.open_cancel_scope() as sc:
            task_status.started(sc)
            while True:
                # data = await self._sock.receive()
                await anyio.wait_socket_readable(self._sock)
                if self._sock is None:
                    return
                data, aux, *_ = self._sock.recvmsg(8192, 4096)
                if not data:
                    raise anyio.EndOfStream
                unmarshaller.feed(data, aux)

                for msg in unmarshaller:
                    self._tg.start_soon(self._on_message, msg)

    async def _message_writer(self, *, task_status):
        with anyio.open_cancel_scope() as sc:
            task_status.started(sc)

            async for msg in self._write_r:
                buf, unix_fds = msg
                await self._write_one(buf, unix_fds)

    async def _write_one(self, buf, unix_fds):
        async with self._write_lock:
            if self._sock is None:
                return  # raise EOFError
            buf = memoryview(buf)
            done = 0
            while done < len(buf) or (unix_fds and self._negotiate_unix_fd):
                await anyio.wait_socket_writable(self._sock)
                if self._sock is None:
                    raise EOFError
                if unix_fds and self._negotiate_unix_fd:
                    ancdata = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", unix_fds))]

                    done2 = self._sock.sendmsg([buf[done:]], ancdata)
                    unix_fds = None
                else:
                    done2 = self._sock.send(buf[done:])
                if not done2:
                    raise EOFError
                done += done2

    async def _auth_readline(self):
        buf = b''
        while buf[-2:] != b'\r\n':
            await anyio.wait_socket_readable(self._sock)
            data = self._sock.recv(1024)
            if not data:
                raise EOFError
            buf += data
        return buf[:-2].decode()

    async def _authenticate(self):
        await anyio.wait_socket_writable(self._sock)
        done = self._sock.send(b'\0')
        if not done:
            raise EOFError

        first_line = self._auth._authentication_start(negotiate_unix_fd=self._negotiate_unix_fd)

        if first_line is not None:
            if not isinstance(first_line, str):
                raise AuthError('authenticator response is %r not str' % (first_line))
            await anyio.wait_socket_writable(self._sock)
            done = self._sock.send(Authenticator._format_line(first_line))

        while True:
            response = self._auth._receive_line(await self._auth_readline())
            if response is not None:
                await anyio.wait_socket_writable(self._sock)
                done = self._sock.send(Authenticator._format_line(response))
            if response == 'BEGIN':
                break
