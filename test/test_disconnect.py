from dbus_next.aio import MessageBus
from dbus_next import Message

import os
import pytest
import anyio


@pytest.mark.anyio
async def test_bus_disconnect_before_reply():
  '''In this test, the bus disconnects before the reply comes in. Make sure
  the caller receives a reply with the error instead of hanging.'''
  bus = MessageBus()
  assert not bus.connected
  async with bus.connect():
    assert bus.connected

    await bus.disconnect()
    # This actually cancels the current scope.

    assert False, "Not called"


@pytest.mark.anyio
async def test_unexpected_disconnect():
    bus = MessageBus()
    assert not bus.connected
    with pytest.raises(anyio.BrokenResourceError):
        async with bus.connect():
            assert bus.connected

            ping = bus.call(
                Message(destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus',
                        member='Ping'))

            os.close(bus._fd)

            # The actual async call will cancel this scope
            # and re-raise the error when leaving the context
            await ping
            assert False, "Not called"
