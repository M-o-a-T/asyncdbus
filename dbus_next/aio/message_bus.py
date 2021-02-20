from ..message_bus import BaseMessageBus
from .._private.unmarshaller import Unmarshaller
from ..message import Message
from ..constants import BusType, NameFlag, RequestNameReply, ReleaseNameReply, MessageType, MessageFlag, ErrorType
from ..service import ServiceInterface
from ..errors import AuthError, DBusError
from .proxy_object import ProxyObject
from .. import introspection as intr
from ..auth import Authenticator, AuthExternal

import logging
import array
import anyio
import socket
import inspect
from copy import copy
from typing import Optional
from contextlib import asynccontextmanager
from concurrent.futures import CancelledError
import outcome
import attr
import traceback


@attr.s
class ValueEvent:
    """A waitable value useful for inter-task synchronization,
    inspired by :class:`threading.Event`.

    An event object manages an internal value, which is initially
    unset, and a task can wait for it to become True.

    Note that the value can only be read once.
    """

    event = attr.ib(factory=anyio.create_event, init=False)
    value = attr.ib(default=None, init=False)

    def set(self, value):
        """Set the result to return this value, and wake any waiting task."""
        if self.value is not None:
            return
        self.value = outcome.Value(value)
        self.event.set()

    def set_error(self, exc):
        """Set the result to raise this exception, and wake any waiting task."""
        if self.value is not None:
            return
        self.value = outcome.Error(exc)
        self.event.set()

    set_result = set
    set_exception = set_error

    def is_set(self):
        """Check whether the event has occurred."""
        return self.value is not None

    def cancel(self):
        """Send a cancelation to the recipient.

        TODO: Trio can't do that cleanly.
        """
        self.set_error(CancelledError())

    async def get(self):
        """Block until the value is set.

        If it's already set, then this method returns immediately.

        The value can only be read once.
        """
        await self.event.wait()
        return self.value.unwrap()

    def __await__(self):
        return self.get().__await__()

    def __call__(self, reply, err):
        if self.is_set():
            return
        if err:
            self.set_error(err)
        else:
            self.set(reply)


class MessageBus(BaseMessageBus):
    """The message bus implementation for use with anyio.

    The message bus class is the entry point into all the features of the
    library. It sets up a connection to the DBus daemon and exposes an
    interface to send and receive messages and expose services.

    You must call :func:`connect() <dbus_next.aio.MessageBus.connect>` before
    using this message bus.

    :param bus_type: The type of bus to connect to. Affects the search path for
        the bus address.
    :type bus_type: :class:`BusType <dbus_next.BusType>`
    :param bus_address: A specific bus address to connect to. Should not be
        used under normal circumstances.
    :param auth: The authenticator to use, defaults to an instance of
        :class:`AuthExternal <dbus_next.auth.AuthExternal>`.
    :type auth: :class:`Authenticator <dbus_next.auth.Authenticator>`
    :param negotiate_unix_fd: Allow the bus to send and receive Unix file
        descriptors (DBus type 'h'). This must be supported by the transport.
    :type negotiate_unix_fd: bool

    :ivar unique_name: The unique name of the message bus connection. It will
        be :class:`None` until the message bus connects.
    :vartype unique_name: str
    :ivar connected: True if this message bus is expected to be able to send
        and receive messages.
    :vartype connected: bool
    """
    def __init__(self,
                 bus_address: str = None,
                 bus_type: BusType = BusType.SESSION,
                 auth: Authenticator = None,
                 negotiate_unix_fd=False):
        super().__init__(bus_address, bus_type, ProxyObject)
        self._negotiate_unix_fd = negotiate_unix_fd

        self._reader = None
        self._writer = None
        self._tg = None

        if auth is None:
            self._auth = AuthExternal()
        else:
            self._auth = auth

        self._disconnect_future = ValueEvent()

    def disconnect(self):
        """Disconnect the message bus by closing the underlying connection asynchronously.
        
        All pending  and future calls will error with a connection error.  
        """
        self._user_disconnect = True
        self._tg.spawn(self._disconnect)

    async def _disconnect(self):
        self._sock.close()


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
                    sock = socket.create_connection((ip_addr,ip_port))
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
        :rtype: :class:`MessageBus <dbus_next.aio.MessageBus>`

        :raises:
            - :class:`AuthError <dbus_next.AuthError>` - If authorization to \
              the DBus daemon failed.
            - :class:`Exception` - If there was a connection error.
        """
        await self._setup_socket_aio()
        did_yield = False
        future = ValueEvent()
        self._disconnect_future = ValueEvent()

        try:
            async with anyio.create_task_group() as tg:
                self._tg = tg
                await self._authenticate()

                self._write, self._write_r = anyio.create_memory_object_stream(10)
                self._reader = await tg.start(self._message_reader)

                def on_hello(reply, err):
                    try:
                        if err:
                            raise err
                        self.unique_name = reply.body[0]
                        future.set(self)
                    except Exception as e:
                        future.set_error(e)
                        self.disconnect()

                hello_msg = Message(destination='org.freedesktop.DBus',
                                    path='/org/freedesktop/DBus',
                                    interface='org.freedesktop.DBus',
                                    member='Hello',
                                    serial=self.next_serial())

                self._method_return_handlers[hello_msg.serial] = on_hello
                await anyio.wait_socket_writable(self._sock)
                self._sock.send(hello_msg._marshall())

                await future
                self._writer = await tg.start(self._message_writer)

                did_yield = True
                yield self
                tg.cancel_scope.cancel()
                pass # close TG

        except Exception as err:
            if not did_yield:
                raise
            self._finalize(err)
        except BaseException:
            self._finalize(CancelledError())
            raise
        else:
            self._finalize()

        await self.wait_for_disconnect()


    async def introspect(self, bus_name: str, path: str, timeout: float = 30.0) -> intr.Node:
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

        :returns: The introspection data for the name at the path.
        :rtype: :class:`Node <dbus_next.introspection.Node>`

        :raises:
            - :class:`InvalidObjectPathError <dbus_next.InvalidObjectPathError>` \
                    - If the given object path is not valid.
            - :class:`InvalidBusNameError <dbus_next.InvalidBusNameError>` - If \
                  the given bus name is not valid.
            - :class:`DBusError <dbus_next.DBusError>` - If the service threw \
                  an error for the method call or returned an invalid result.
            - :class:`Exception` - If a connection error occurred.
            - :class:`TimeoutError` - Waited for future but time run out.
        """
        future = ValueEvent()

        super().introspect(bus_name, path, future)

        with anyio.fail_after(timeout):
            return await future


    async def request_name(self, name: str, flags: NameFlag = NameFlag.NONE) -> RequestNameReply:
        """Request that this message bus owns the given name.

        :param name: The name to request.
        :type name: str
        :param flags: Name flags that affect the behavior of the name request.
        :type flags: :class:`NameFlag <dbus_next.NameFlag>`

        :returns: The reply to the name request.
        :rtype: :class:`RequestNameReply <dbus_next.RequestNameReply>`

        :raises:
            - :class:`InvalidBusNameError <dbus_next.InvalidBusNameError>` - If \
                  the given bus name is not valid.
            - :class:`DBusError <dbus_next.DBusError>` - If the service threw \
                  an error for the method call or returned an invalid result.
            - :class:`Exception` - If a connection error occurred.
        """
        future = ValueEvent()

        super().request_name(name, flags, future)

        return await future

    async def release_name(self, name: str) -> ReleaseNameReply:
        """Request that this message bus release the given name.

        :param name: The name to release.
        :type name: str

        :returns: The reply to the release request.
        :rtype: :class:`ReleaseNameReply <dbus_next.ReleaseNameReply>`

        :raises:
            - :class:`InvalidBusNameError <dbus_next.InvalidBusNameError>` - If \
                  the given bus name is not valid.
            - :class:`DBusError <dbus_next.DBusError>` - If the service threw \
                  an error for the method call or returned an invalid result.
            - :class:`Exception` - If a connection error occurred.
        """
        future = ValueEvent()

        super().release_name(name, future)

        return await future

    async def call(self, msg: Message) -> Optional[Message]:
        """Send a method call and wait for a reply from the DBus daemon.

        :param msg: The method call message to send.
        :type msg: :class:`Message <dbus_next.Message>`

        :returns: A message in reply to the message sent. If the message does
            not expect a reply based on the message flags or type, returns
            ``None`` after the message is sent.
        :rtype: :class:`Message <dbus_next.Message>` or :class:`None` if no reply is expected.

        :raises:
            - :class:`Exception` - If a connection error occurred.
        """
        if msg.flags & MessageFlag.NO_REPLY_EXPECTED or msg.message_type is not MessageType.METHOD_CALL:
            await self.send(msg)
            return None

        future = ValueEvent()
        self._call(msg, future)
        return await future

    def send(self, msg: Message):
        """Asynchronously send a message on the message bus.

        .. note:: This method may change to a couroutine function in the 1.0
            release of the library.

        :param msg: The message to send.
        :type msg: :class:`Message <dbus_next.Message>`

        :returns: A future that resolves when the message is sent or a
            connection error occurs.
        :rtype: :class:`ValueEvent`
        """
        if not msg.serial:
            msg.serial = self.next_serial()

        future = ValueEvent()
        self._schedule_write(msg, future)

        return future

    def get_proxy_object(self, bus_name: str, path: str, introspection: intr.Node) -> ProxyObject:
        return super().get_proxy_object(bus_name, path, introspection)

    async def wait_for_disconnect(self):
        """Wait for the message bus to disconnect.

        :returns: :class:`None` when the message bus has disconnected.
        :rtype: :class:`None`

        :raises:
            - :class:`Exception` - If connection was terminated unexpectedly or \
              an internal error occurred in the library.
        """
        if self._disconnect_future is True:
            return
        df, self._disconnect_future = self._disconnect_future, True
        return await df

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
            self._tg.spawn(handler, msg, send_reply)

        return _handler

    async def _message_reader(self, *, task_status):
        unmarshaller = Unmarshaller()
        with anyio.open_cancel_scope() as sc:
            task_status.started(sc)
            while True:
                # data = await self._sock.receive()
                await anyio.wait_socket_readable(self._sock)
                data,aux,*_ = self._sock.recvmsg(8192,4096)
                if not data:
                    raise anyio.EndOfStream
                unmarshaller.feed(data,aux)

                for msg in unmarshaller:
                    self._on_message(msg)

    async def _message_writer(self, *, task_status):
        with anyio.open_cancel_scope() as sc:
            task_status.started(sc)

            async for msg in self._write_r:
                buf, unix_fds, fut = msg
                buf = memoryview(buf)
                done = 0
                while done < len(buf) or (unix_fds and self._negotiate_unix_fd):
                    await anyio.wait_socket_writable(self._sock)
                    if unix_fds and self._negotiate_unix_fd:
                        ancdata = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                    array.array("i", unix_fds))]

                        done2 = self._sock.sendmsg([buf[done:]], ancdata)
                        unix_fds = None
                    else:
                        done2 = self._sock.send(buf[done:])
                    if not done2:
                        raise EOFError
                    done += done2

                fut.set(None)

    def buffer_message(self, msg: Message, future=None):
        self._write.send_nowait(
            (msg._marshall(negotiate_unix_fd=self._negotiate_unix_fd), copy(msg.unix_fds), future))

    def _schedule_write(self, msg: Message = None, future=None):
        if msg is not None:
            self.buffer_message(msg, future)

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

    def _finalize(self, err=None):
        if self._reader is not None:
            self._reader.cancel()
        if self._writer is not None:
            self._writer.cancel()
        if self._tg is not None:
            self._tg.cancel_scope.cancel()
        self._write = None

        super()._finalize(err)

        if self._disconnect_future is True or self._disconnect_future.is_set():
            return

        if err and not self._user_disconnect:
            self._disconnect_future.set_error(err)
        else:
            self._disconnect_future.set(None)
