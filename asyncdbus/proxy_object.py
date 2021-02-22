from .validators import assert_object_path_valid, assert_bus_name_valid
from . import message_bus
from .message import Message
from .constants import MessageType, ErrorType, MessageFlag
from . import introspection as intr
from .errors import DBusError, InterfaceNotFoundError
from ._private.util import replace_idx_with_fds, replace_fds_with_idx
from .signature import Variant, Str, Var, Tuple

from typing import Type, Union, List
import logging
import xml.etree.ElementTree as ET
import inspect
import re


class ProxyInterface:
    """A class representing a proxy to an interface exported on the bus by another client.

    Implementations of this class are not meant to be constructed directly by
    users. Use :func:`ProxyObject.get_interface` to get a proxy interface.
    Each message bus implementation provides its own proxy interface
    implementation that will be returned by that method.

    Proxy interfaces can be used to call methods, get properties, and listen to
    signals on the interface. Proxy interfaces are created dynamically with a
    family of methods for each of these operations based on what members the
    interface exposes. Each proxy interface implementation exposes these
    members in a different way depending on the features of the backend. See
    the documentation of the proxy interface implementation you use for more
    details.

    A *method call* takes this form:    
                        
    .. code-block:: python3
        
        result = await interface.call_[METHOD](*args)
                
    Where ``METHOD`` is the name of the method converted to snake case.

    DBus methods are exposed as coroutines that take arguments that correpond
    to the *in args* of the interface method definition and return a ``result``
    that corresponds to the *out arg*. If the method has more than one out arg,
    they are returned within a :class:`list`.

    To *listen to a signal* use this form:
                
    .. code-block:: python3   

        interface.on_[SIGNAL](callback)
                
    To *stop listening to a signal* use this form:
        
    .. code-block:: python3

        interface.off_[SIGNAL](callback)   
        
    Where ``SIGNAL`` is the name of the signal converted to snake case.

    DBus signals are exposed with an event-callback interface. The provided
    ``callback`` will be called when the signal is emitted with arguments that
    correspond to the *out args* of the interface signal definition.

    To *get or set a property* use this form:

    .. code-block:: python3

        value = await interface.get_[PROPERTY]()
        await interface.set_[PROPERTY](value)

    Where ``PROPERTY`` is the name of the property converted to snake case.

    DBus property getters and setters are exposed as coroutines. The ``value``
    must correspond to the type of the property in the interface definition.

    If the service returns an error for a DBus call, a :class:`DBusError
    <asyncdbus.DBusError>` will be raised with information about the error.

    :ivar bus_name: The name of the bus this interface is exported on.
    :vartype bus_name: str
    :ivar path: The object path exported on the client that owns the bus name.
    :vartype path: str
    :ivar introspection: Parsed introspection data for the proxy interface.
    :vartype introspection: :class:`Node <asyncdbus.introspection.Interface>`
    :ivar bus: The message bus this proxy interface is connected to.
    :vartype bus: :class:`MessageBus <asyncdbus.message_bus.MessageBus>`
    """

    def __init__(self, bus_name, path, introspection, bus):

        self.bus_name = bus_name
        self.path = path
        self.introspection = introspection
        self.bus = bus
        self._signal_handlers = {}
        self._signal_match_rule = f"type='signal',sender={bus_name},interface={introspection.name},path={path}"

    _underscorer1 = re.compile(r'(.)([A-Z][a-z]+)')
    _underscorer2 = re.compile(r'([a-z0-9])([A-Z])')

    @staticmethod
    def _to_snake_case(member):
        subbed = ProxyInterface._underscorer1.sub(r'\1_\2', member)
        return ProxyInterface._underscorer2.sub(r'\1_\2', subbed).lower()

    @staticmethod
    def _check_method_return(msg, signature=None):
        if hasattr(signature, 'tree'):
            signature = signature.tree.signature
        if msg.message_type == MessageType.ERROR:
            raise DBusError._from_message(msg)
        elif msg.message_type != MessageType.METHOD_RETURN:
            raise DBusError(ErrorType.CLIENT_ERROR, 'method call didnt return a method return', msg)
        elif signature is not None and msg.signature != signature:
            raise DBusError(ErrorType.CLIENT_ERROR,
                            f'method call returned unexpected signature: "{msg.signature}"', msg)

    def _add_method(self, intr_method):
        async def method_fn(*args, flags=MessageFlag.NONE):
            input_body, unix_fds = replace_fds_with_idx(intr_method.in_signature, list(args))

            msg = await self.bus.call(
                Message(
                    destination=self.bus_name,
                    path=self.path,
                    interface=self.introspection.name,
                    member=intr_method.name,
                    signature=intr_method.in_signature,
                    body=input_body,
                    flags=flags,
                    unix_fds=unix_fds))

            if flags & MessageFlag.NO_REPLY_EXPECTED:
                return None

            self._check_method_return(msg, intr_method.out_signature)

            out_len = len(intr_method.out_args)

            body = replace_idx_with_fds(msg.signature_tree, msg.body, msg.unix_fds)

            if not out_len:
                return None
            elif out_len == 1:
                return body[0]
            else:
                return body

        method_name = f'call_{self._to_snake_case(intr_method.name)}'
        setattr(self, method_name, method_fn)

    def _add_property(self, intr_property):
        async def property_getter():
            msg = await self.bus.call(
                Message(
                    destination=self.bus_name,
                    path=self.path,
                    interface='org.freedesktop.DBus.Properties',
                    member='Get',
                    signature=Tuple[Str, Str],
                    body=[self.introspection.name, intr_property.name]))

            self._check_method_return(msg, Var)
            variant = msg.body[0]
            sig = intr_property.signature
            if hasattr(sig, 'tree'):
                sig = sig.tree.signature
            if variant.signature != sig:
                raise DBusError(ErrorType.CLIENT_ERROR,
                                f'property returned unexpected signature "{variant.signature}"',
                                msg)

            return replace_idx_with_fds(Var, msg.body, msg.unix_fds)[0].value

        async def property_setter(val):
            variant = Variant(intr_property.signature, val)

            body, unix_fds = replace_fds_with_idx(
                'ssv', [self.introspection.name, intr_property.name, variant])

            msg = await self.bus.call(
                Message(
                    destination=self.bus_name,
                    path=self.path,
                    interface='org.freedesktop.DBus.Properties',
                    member='Set',
                    signature=Tuple[Str, Str, Var],
                    body=body,
                    unix_fds=unix_fds))

            self._check_method_return(msg)

        snake_case = self._to_snake_case(intr_property.name)
        setattr(self, f'get_{snake_case}', property_getter)
        setattr(self, f'set_{snake_case}', property_setter)

    async def _message_handler(self, msg):
        if not msg._matches(
                message_type=MessageType.SIGNAL, interface=self.introspection.name,
                path=self.path) or msg.member not in self._signal_handlers:
            return

        if msg.sender != self.bus_name and self.bus._name_owners.get(self.bus_name,
                                                                     '') != msg.sender:
            # The sender is always a unique name, but the bus name given might
            # be a well known name. If the sender isn't an exact match, check
            # to see if it owns the bus_name we were given from the cache kept
            # on the bus for this purpose.
            return

        match = [s for s in self.introspection.signals if s.name == msg.member]
        if not len(match):
            return
        intr_signal = match[0]
        if intr_signal.signature != msg.signature:
            logging.warning(
                f'got signal "{self.introspection.name}.{msg.member}" with unexpected signature "{msg.signature}"'
            )
            return

        body = replace_idx_with_fds(msg.signature, msg.body, msg.unix_fds)
        for handler in self._signal_handlers[msg.member]:
            res = handler(*body)
            if inspect.iscoroutine(res):
                await res

    def _add_signal(self, intr_signal, interface):
        async def on_signal_fn(fn):
            fn_signature = inspect.signature(fn)
            if not callable(fn) or len(fn_signature.parameters) != len(intr_signal.args):
                raise TypeError(
                    f'reply_notify must be a function with {len(intr_signal.args)} parameters')

            if not self._signal_handlers:
                await self.bus._add_match_rule(self._signal_match_rule)
                self.bus.add_message_handler(self._message_handler)

            if intr_signal.name not in self._signal_handlers:
                self._signal_handlers[intr_signal.name] = []

            self._signal_handlers[intr_signal.name].append(fn)

        async def off_signal_fn(fn):
            try:
                i = self._signal_handlers[intr_signal.name].index(fn)
                del self._signal_handlers[intr_signal.name][i]
                if not self._signal_handlers[intr_signal.name]:
                    del self._signal_handlers[intr_signal.name]
            except (KeyError, ValueError):
                return

            if not self._signal_handlers:
                await self.bus._remove_match_rule(self._signal_match_rule)
                self.bus.remove_message_handler(self._message_handler)

        snake_case = self._to_snake_case(intr_signal.name)
        setattr(interface, f'on_{snake_case}', on_signal_fn)
        setattr(interface, f'off_{snake_case}', off_signal_fn)


class ProxyObject:
    """The proxy object implementation for async IO.

    Implementations of this class are not meant to be constructed directly. Use
    :func:`MessageBus.get_proxy_object()
    <asyncdbus.message_bus.MessageBus.get_proxy_object>` to get a proxy
    object. Each message bus implementation provides its own proxy object
    implementation that will be returned by that method.

    The primary use of the proxy object is to select a proxy interface to act
    on. Information on what interfaces are available is provided by
    introspection data provided to this class. This introspection data can
    either be included in your project as an XML file (recommended) or
    retrieved from the ``org.freedesktop.DBus.Introspectable`` interface at
    runtime.

    :ivar bus_name: The name of the bus this object is exported on.
    :vartype bus_name: str
    :ivar path: The object path exported on the client that owns the bus name.
    :vartype path: str
    :ivar introspection: Parsed introspection data for the proxy object.
    :vartype introspection: :class:`Node <asyncdbus.introspection.Node>`
    :ivar bus: The message bus this proxy object is connected to.
    :vartype bus: :class:`MessageBus <asyncdbus.message_bus.MessageBus>`
    :ivar ~.ProxyInterface: The proxy interface class this proxy object uses.
    :vartype ~.ProxyInterface: Type[:class:`ProxyInterface <asyncdbus.proxy_object.ProxyObject>`]
    :ivar child_paths: A list of absolute object paths of the children of this object.
    :vartype child_paths: list(str)

    :raises:
        - :class:`InvalidBusNameError <asyncdbus.InvalidBusNameError>` - If the given bus name is not valid.
        - :class:`InvalidObjectPathError <asyncdbus.InvalidObjectPathError>` - If the given object path is not valid.
        - :class:`InvalidIntrospectionError <asyncdbus.InvalidIntrospectionError>` - If the introspection data for the node is not valid.
    """

    def __init__(self, bus_name: str, path: str, introspection: Union[intr.Node, str, ET.Element],
                 bus: 'message_bus.MessageBus'):
        assert_object_path_valid(path)
        assert_bus_name_valid(bus_name)

        if not isinstance(bus, message_bus.MessageBus):
            raise TypeError('bus must be an instance of MessageBus')

        if type(introspection) is intr.Node:
            self.introspection = introspection
        elif type(introspection) is str:
            self.introspection = intr.Node.parse(introspection)
        elif type(introspection) is ET.Element:
            self.introspection = intr.Node.from_xml(introspection)
        else:
            raise TypeError(
                'introspection must be xml node introspection or introspection.Node class')

        self.bus_name = bus_name
        self.path = path
        self.bus = bus
        self.ProxyInterface = ProxyInterface
        self.child_paths = [f'{path}/{n.name}' for n in self.introspection.nodes]

        self._interfaces = {}

        # lazy loaded by get_children()
        self._children = None

    async def get_interface(self, name: str) -> ProxyInterface:
        """Get an interface exported on this proxy object and connect it to the bus.

        :param name: The name of the interface to retrieve.
        :type name: str

        :raises:
            - :class:`InterfaceNotFoundError <asyncdbus.InterfaceNotFoundError>` - If there is no interface by this name exported on the bus.
        """
        if name in self._interfaces:
            return self._interfaces[name]

        try:
            intr_interface = next(i for i in self.introspection.interfaces if i.name == name)
        except StopIteration:
            raise InterfaceNotFoundError(f'interface not found on this object: {name}')

        interface = self.ProxyInterface(self.bus_name, self.path, intr_interface, self.bus)

        for intr_method in intr_interface.methods:
            interface._add_method(intr_method)
        for intr_property in intr_interface.properties:
            interface._add_property(intr_property)
        for intr_signal in intr_interface.signals:
            interface._add_signal(intr_signal, interface)

        if self.bus_name[0] != ':' and not self.bus._name_owners.get(self.bus_name, ''):
            try:
                reply = await self.bus.call(
                    Message(
                        destination='org.freedesktop.DBus',
                        interface='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        member='GetNameOwner',
                        signature='s',
                        body=[self.bus_name]))
            except DBusError as err:
                if err.type != 'org.freedesktop.DBus.Error.NameHasNoOwner':
                    raise
            else:
                self.bus._name_owners[self.bus_name] = reply.body[0]

        self._interfaces[name] = interface
        return interface

    def get_children(self) -> List['ProxyObject']:
        """Get the child nodes of this proxy object according to the introspection data."""
        if self._children is None:
            self._children = [
                self.__class__(self.bus_name, self.path, child, self.bus)
                for child in self.introspection.nodes
            ]

        return self._children
