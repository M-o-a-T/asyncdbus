from asyncdbus.service import ServiceInterface, method
from asyncdbus import Message, MessageBus, MessageType, ErrorType, Variant, SignatureTree, DBusError, MessageFlag
from asyncdbus.signature import Str, Tuple, Array, Var, Struct, Dict, UInt64

import pytest


class ExampleInterface(ServiceInterface):
    def __init__(self, name):
        super().__init__(name)

    @method()
    def echo(self, what: Str) -> Str:
        assert type(self) is ExampleInterface
        return what

    @method()
    def echo_multiple(self, what1: Str, what2: Str) -> Tuple[Str, Str]:
        assert type(self) is ExampleInterface
        return [what1, what2]

    @method()
    def echo_containers(
            self, array: Array[Str], variant: Var, dict_entries: Array[Dict[Str, Var]],
            struct: Struct[Str, Struct[Str, Struct[Var]]]
    ) -> Tuple[Array[Str], Var, Array[Dict[Str, Var]], Struct[Str, Struct[Str, Struct[Var]]]]:
        assert type(self) is ExampleInterface
        return [array, variant, dict_entries, struct]

    @method()
    def ping(self):
        assert type(self) is ExampleInterface
        pass

    @method(name='renamed')
    def original_name(self):
        assert type(self) is ExampleInterface
        pass

    @method(disabled=True)
    def not_here(self):
        assert type(self) is ExampleInterface
        pass

    @method()
    def throws_unexpected_error(self):
        assert type(self) is ExampleInterface
        raise Exception('oops')

    @method()
    def throws_dbus_error(self):
        assert type(self) is ExampleInterface
        raise DBusError('test.error', 'an error ocurred')


class AsyncInterface(ServiceInterface):
    def __init__(self, name):
        super().__init__(name)

    @method()
    async def echo(self, what: Str) -> Str:
        assert type(self) is AsyncInterface
        return what

    @method()
    async def echo_multiple(self, what1: Str, what2: Str) -> Tuple[Str, Str]:
        assert type(self) is AsyncInterface
        return [what1, what2]

    @method()
    async def echo_containers(self, array: 'as', variant: 'v', dict_entries: 'a{sv}',
                              struct: '(s(s(v)))') -> 'asva{sv}(s(s(v)))':
        assert type(self) is AsyncInterface
        return [array, variant, dict_entries, struct]

    @method()
    async def ping(self):
        assert type(self) is AsyncInterface
        pass

    @method(name='renamed')
    async def original_name(self):
        assert type(self) is AsyncInterface
        pass

    @method(disabled=True)
    async def not_here(self):
        assert type(self) is AsyncInterface
        pass

    @method()
    async def throws_unexpected_error(self):
        assert type(self) is AsyncInterface
        raise Exception('oops')

    @method()
    def throws_dbus_error(self):
        assert type(self) is AsyncInterface
        raise DBusError('test.error', 'an error ocurred')


@pytest.mark.parametrize('interface_class', [ExampleInterface, AsyncInterface])
@pytest.mark.anyio
async def test_methods(interface_class):
    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:

        interface = interface_class('test.interface')
        export_path = '/test/path'

        async def call(member, signature='', body=[], flags=MessageFlag.NONE):
            return await bus2.call(
                Message(
                    destination=bus1.unique_name,
                    path=export_path,
                    interface=interface.name,
                    member=member,
                    signature=signature,
                    body=body,
                    flags=flags))

        await bus1.export(export_path, interface)

        body = ['hello world']
        reply = await call('echo', Str, body)

        assert reply.message_type == MessageType.METHOD_RETURN, reply.body[0]
        assert reply.signature == Str.tree.signature
        assert reply.body == body

        body = ['hello', 'world']
        reply = await call('echo_multiple', Tuple[Str, Str], body)
        assert reply.message_type == MessageType.METHOD_RETURN, reply.body[0]
        assert reply.signature == Tuple[Str, Str].tree.signature
        assert reply.body == body

        body = [['hello', 'world'],
                Variant('v', Variant(Struct[Str, Str], ['hello', 'world'])),
                {
                    'foo': Variant(UInt64, 100)
                }, ['one', ['two', [Variant(Str, 'three')]]]]
        signature = 'asva{sv}(s(s(v)))'
        SignatureTree(signature).verify(body)
        reply = await call('echo_containers', signature, body)
        assert reply.message_type == MessageType.METHOD_RETURN, reply.body[0]
        assert reply.signature == signature
        assert reply.body == body

        reply = await call('ping')
        assert reply.message_type == MessageType.METHOD_RETURN, reply.body[0]
        assert reply.signature == ''
        assert reply.body == []

        with pytest.raises(DBusError) as err:
            await call('throws_unexpected_error')
        reply = err.value.reply
        assert reply.message_type == MessageType.ERROR, reply.body[0]
        assert reply.error_name == ErrorType.SERVICE_ERROR.value, reply.body[0]

        with pytest.raises(DBusError) as err:
            await call('throws_dbus_error')
        reply = err.value.reply
        assert reply.message_type == MessageType.ERROR, reply.body[0]
        assert reply.error_name == 'test.error', reply.body[0]
        assert reply.body == ['an error ocurred']

        reply = await call('ping', flags=MessageFlag.NO_REPLY_EXPECTED)
        assert reply is None

        reply = await call('throws_unexpected_error', flags=MessageFlag.NO_REPLY_EXPECTED)
        assert reply is None

        reply = await call('throws_dbus_error', flags=MessageFlag.NO_REPLY_EXPECTED)
        assert reply is None
