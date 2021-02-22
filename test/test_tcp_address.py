from asyncdbus import Message, MessageBus
from asyncdbus._private.address import parse_address

import anyio
import pytest
import os


@pytest.mark.anyio
async def test_tcp_connection_with_forwarding():
    async with anyio.create_task_group() as tg:
        closables = []
        host = '127.0.0.1'
        port = '55556'

        addr_info = parse_address(os.environ.get('DBUS_SESSION_BUS_ADDRESS'))
        assert addr_info
        assert 'abstract' in addr_info[0][1]
        path = f'\0{addr_info[0][1]["abstract"]}'

        async def handle_connection(tcp_sock):
            async with await anyio.connect_unix(path) as unix_sock:
                async with anyio.create_task_group() as tg:

                    async def handle_read():
                        try:
                            while True:
                                data = await tcp_sock.receive()
                                await unix_sock.send(data)
                        except (anyio.ClosedResourceError, anyio.EndOfStream):
                            return

                    async def handle_write():
                        try:
                            while True:
                                data = await unix_sock.receive()
                                await tcp_sock.send(data)
                        except (anyio.ClosedResourceError, anyio.EndOfStream):
                            return

                    tg.spawn(handle_read)
                    tg.spawn(handle_write)

        listener = await anyio.create_tcp_listener(local_port=port, local_host=host)
        tg.spawn(listener.serve, handle_connection)
        await anyio.sleep(0.1)

        try:
            async with MessageBus(bus_address=f'tcp:host={host},port={port}').connect() as bus:

                # basic tests to see if it works
                result = await bus.call(
                    Message(
                        destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus.Peer',
                        member='Ping'))
                assert result

                intr = await bus.introspect('org.freedesktop.DBus', '/org/freedesktop/DBus')
                obj = await bus.get_proxy_object('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                                 intr)
                iface = await obj.get_interface('org.freedesktop.DBus.Peer')
                await iface.call_ping()

                sock = bus._sock.extra(anyio.abc.SocketAttribute.raw_socket) \
                        if hasattr(bus._sock,'extra') else bus._sock
                assert sock.getpeername()[0] == host
                assert sock.getsockname()[0] == host
                assert sock.gettimeout() == 0

                pass  # A
        finally:
            tg.cancel_scope.cancel()
