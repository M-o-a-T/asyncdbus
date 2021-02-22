from asyncdbus import Message, MessageBus

import os
import pytest
import anyio
import errno


@pytest.mark.anyio
async def test_bus_disconnect_before_reply():
    '''In this test, the bus disconnects before the reply comes in. Make sure
  the caller receives a reply with the error instead of hanging.'''
    bus = MessageBus()
    assert not bus.connected
    async with bus.connect():
        assert bus.connected
    assert not bus.connected


@pytest.mark.anyio
async def test_unexpected_disconnect():
    bus = MessageBus()
    assert not bus.connected
    with pytest.raises((anyio.BrokenResourceError, OSError, anyio.ExceptionGroup)):
        async with bus.connect():
            assert bus.connected

            ping = bus.call(
                Message(
                    destination='org.freedesktop.DBus',
                    path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus',
                    member='Ping'))

            os.close(bus._fd)

            # The actual async call will ecancel this scope
            # and re-raise the error when leaving the context
            await ping
