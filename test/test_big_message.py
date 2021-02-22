from asyncdbus import MessageBus, Message, MessageType
from asyncdbus.service import ServiceInterface, method

import pytest


class ExampleInterface(ServiceInterface):
    def __init__(self):
        super().__init__('example.interface')

    @method()
    def echo_bytes(self, what: 'ay') -> 'ay':
        return what


@pytest.mark.anyio
async def test_aio_big_message():
    'this tests that nonblocking reads and writes actually work'
    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:
        interface = ExampleInterface()
        await bus1.export('/test/path', interface)

        # two megabytes
        big_body = [bytes(1000000) * 2]
        result = await bus2.call(
            Message(
                destination=bus1.unique_name,
                path='/test/path',
                interface=interface.name,
                member='echo_bytes',
                signature='ay',
                body=big_body))
        assert result.message_type == MessageType.METHOD_RETURN, result.body[0]
        assert result.body[0] == big_body[0]
