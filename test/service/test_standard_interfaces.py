from asyncdbus.service import ServiceInterface, dbus_property, PropertyAccess
from asyncdbus.signature import Variant, Str, Var, Array, Empty, Byte, Tuple
from asyncdbus.errors import DBusError
from asyncdbus import Message, MessageBus, MessageType, introspection as intr
from asyncdbus.constants import ErrorType

import pytest

standard_interfaces_count = len(intr.Node.default().interfaces)


class ExampleInterface(ServiceInterface):
    def __init__(self, name):
        super().__init__(name)


class ExampleComplexInterface(ServiceInterface):
    def __init__(self, name):
        self._foo = 42
        self._bar = 'str'
        super().__init__(name)

    @dbus_property(access=PropertyAccess.READ)
    def Foo(self) -> 'y':
        return self._foo

    @dbus_property(access=PropertyAccess.READ)
    def Bar(self) -> Str:
        return self._bar


@pytest.mark.anyio
async def test_introspectable_interface():
    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:

        interface = ExampleInterface('test.interface')
        interface2 = ExampleInterface('test.interface2')

        export_path = '/test/path'
        await bus1.export(export_path, interface)
        await bus1.export(export_path, interface2)

        reply = await bus2.call(
            Message(
                destination=bus1.unique_name,
                path=export_path,
                interface='org.freedesktop.DBus.Introspectable',
                member='Introspect'))

        assert reply.message_type == MessageType.METHOD_RETURN, reply.body[0]
        assert reply.signature == Str.tree.signature
        node = intr.Node.parse(reply.body[0])
        assert len(node.interfaces) == standard_interfaces_count + 2
        assert node.interfaces[-1].name == 'test.interface2'
        assert node.interfaces[-2].name == 'test.interface'
        assert not node.nodes

        # introspect works on every path
        reply = await bus2.call(
            Message(
                destination=bus1.unique_name,
                path='/path/doesnt/exist',
                interface='org.freedesktop.DBus.Introspectable',
                member='Introspect'))
        assert reply.message_type == MessageType.METHOD_RETURN, reply.body[0]
        assert reply.signature == Str.tree.signature
        node = intr.Node.parse(reply.body[0])
        assert not node.interfaces
        assert not node.nodes


@pytest.mark.anyio
async def test_peer_interface():
    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:

        reply = await bus2.call(
            Message(
                destination=bus1.unique_name,
                path='/path/doesnt/exist',
                interface='org.freedesktop.DBus.Peer',
                member='Ping'))

        assert reply.message_type == MessageType.METHOD_RETURN, reply.body[0]
        assert reply.signature == Empty.tree.signature

        reply = await bus2.call(
            Message(
                destination=bus1.unique_name,
                path='/path/doesnt/exist',
                interface='org.freedesktop.DBus.Peer',
                member='GetMachineId',
                signature=''))

        assert reply.message_type == MessageType.METHOD_RETURN, reply.body[0]
        assert reply.signature == Str.tree.signature


@pytest.mark.anyio
async def test_object_manager():
    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:
        expected_reply = {
            '/test/path/deeper': {
                'test.interface2': {
                    'Bar': Variant(Str, 'str'),
                    'Foo': Variant(Byte, 42)
                }
            }
        }
        reply_ext = {
            '/test/path': {
                'test.interface1': {},
                'test.interface2': {
                    'Bar': Variant(Str, 'str'),
                    'Foo': Variant(Byte, 42)
                }
            }
        }

        interface = ExampleInterface('test.interface1')
        interface2 = ExampleComplexInterface('test.interface2')

        export_path = '/test/path'
        await bus1.export(export_path, interface)
        await bus1.export(export_path, interface2)
        await bus1.export(export_path + '/deeper', interface2)

        reply_root = await bus2.call(
            Message(
                destination=bus1.unique_name,
                path='/',
                interface='org.freedesktop.DBus.ObjectManager',
                member='GetManagedObjects'))

        reply_level1 = await bus2.call(
            Message(
                destination=bus1.unique_name,
                path=export_path,
                interface='org.freedesktop.DBus.ObjectManager',
                member='GetManagedObjects'))

        reply_level2 = await bus2.call(
            Message(
                destination=bus1.unique_name,
                path=export_path + '/deeper',
                interface='org.freedesktop.DBus.ObjectManager',
                member='GetManagedObjects'))

        assert reply_root.signature == 'a{oa{sa{sv}}}'
        assert reply_level1.signature == 'a{oa{sa{sv}}}'
        assert reply_level2.signature == 'a{oa{sa{sv}}}'

        assert reply_level2.body == [{}]
        assert reply_level1.body == [expected_reply]
        expected_reply.update(reply_ext)
        assert reply_root.body == [expected_reply]


@pytest.mark.anyio
async def test_standard_interface_properties():
    # standard interfaces have no properties, but should still behave correctly
    # when you try to call the methods anyway (#49)
    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:

        interface = ExampleInterface('test.interface1')
        export_path = '/test/path'
        await bus1.export(export_path, interface)

        for iface in [
                'org.freedesktop.DBus.Properties', 'org.freedesktop.DBus.Introspectable',
                'org.freedesktop.DBus.Peer', 'org.freedesktop.DBus.ObjectManager'
        ]:

            with pytest.raises(DBusError) as err:
                result = await bus2.call(
                    Message(
                        destination=bus1.unique_name,
                        path=export_path,
                        interface='org.freedesktop.DBus.Properties',
                        member='Get',
                        signature=Tuple[Str, Str],
                        body=[iface, 'anything']))
            result = err.value.reply
            assert result.message_type is MessageType.ERROR
            assert result.error_name == ErrorType.UNKNOWN_PROPERTY.value

            with pytest.raises(DBusError) as err:
                result = await bus2.call(
                    Message(
                        destination=bus1.unique_name,
                        path=export_path,
                        interface='org.freedesktop.DBus.Properties',
                        member='Set',
                        signature=Tuple[Str, Str, Var],
                        body=[iface, 'anything', Variant(Str, 'new thing')]))
            result = err.value.reply
            assert result.message_type is MessageType.ERROR
            assert result.error_name == ErrorType.UNKNOWN_PROPERTY.value

            result = await bus2.call(
                Message(
                    destination=bus1.unique_name,
                    path=export_path,
                    interface='org.freedesktop.DBus.Properties',
                    member='GetAll',
                    signature=Str,
                    body=[iface]))
            assert result.message_type is MessageType.METHOD_RETURN
            assert result.body == [{}]
