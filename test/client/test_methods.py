from asyncdbus.message import MessageFlag
from asyncdbus.service import ServiceInterface, method
import asyncdbus.introspection as intr
from asyncdbus import MessageBus, DBusError, ProxyObject
from asyncdbus.signature import Str, Tuple

import pytest


class ExampleInterface(ServiceInterface):
    def __init__(self):
        super().__init__('test.interface')

    @method()
    def Ping(self):
        pass

    @method()
    def EchoInt64(self, what: 'x') -> 'x':
        return what

    @method()
    def EchoString(self, what: Str) -> Str:
        return what

    @method()
    def ConcatStrings(self, what1: Str, what2: Str) -> Str:
        return what1 + what2

    @method()
    def EchoThree(self, what1: Str, what2: Str, what3: Str) -> Tuple[Str, Str, Str]:
        return [what1, what2, what3]

    @method()
    def ThrowsError(self):
        raise DBusError('test.error', 'something went wrong')


@pytest.mark.anyio
async def test_aio_proxy_object():
    bus_name = 'client.test.methods'

    async with MessageBus().connect() as bus, \
            MessageBus().connect() as bus2:
        await bus.request_name(bus_name)
        service_interface = ExampleInterface()
        await bus.export('/test/path', service_interface)
        # add some more to test nodes
        await bus.export('/test/path/child1', ExampleInterface())
        await bus.export('/test/path/child2', ExampleInterface())

        introspection = await bus2.introspect(bus_name, '/test/path')
        assert type(introspection) is intr.Node
        obj = await bus2.get_proxy_object(bus_name, '/test/path', introspection)
        interface = await obj.get_interface(service_interface.name)

        children = obj.get_children()
        assert len(children) == 2
        for child in obj.get_children():
            assert type(child) is ProxyObject

        result = await interface.call_ping()
        assert result is None

        result = await interface.call_echo_string('hello')
        assert result == 'hello'

        result = await interface.call_concat_strings('hello ', 'world')
        assert result == 'hello world'

        result = await interface.call_echo_three('hello', 'there', 'world')
        assert result == ['hello', 'there', 'world']

        result = await interface.call_echo_int64(-10000)
        assert result == -10000

        result = await interface.call_echo_string('no reply', flags=MessageFlag.NO_REPLY_EXPECTED)
        assert result is None

        with pytest.raises(DBusError):
            try:
                await interface.call_throws_error()
            except DBusError as e:
                assert e.reply is not None
                assert e.type == 'test.error'
                assert e.text == 'something went wrong'
                raise e
