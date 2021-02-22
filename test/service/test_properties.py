from asyncdbus.service import ServiceInterface, dbus_property, method
from asyncdbus import Message, MessageBus, MessageType, PropertyAccess, ErrorType, Variant, DBusError
from asyncdbus.signature import Tuple, Str

import pytest
import anyio

from asyncdbus.signature import Str, Array, Struct, Dict, Int64


class ExampleInterface(ServiceInterface):
    def __init__(self, name):
        super().__init__(name)
        self._string_prop = 'hi'
        self._readonly_prop = 100
        self._disabled_prop = '1234'
        self._container_prop = [['hello', 'world']]
        self._renamed_prop = '65'

    @dbus_property()
    def string_prop(self) -> Str:
        return self._string_prop

    @string_prop.setter
    def string_prop_setter(self, val: Str):
        self._string_prop = val

    @dbus_property(PropertyAccess.READ)
    def readonly_prop(self) -> Int64:
        return self._readonly_prop

    @dbus_property()
    def container_prop(self) -> Array[Struct[Str, Str]]:
        return self._container_prop

    @container_prop.setter
    def container_prop(self, val: Array[Struct[Str, Str]]):
        self._container_prop = val

    @dbus_property(name='renamed_prop')
    def original_name(self) -> Str:
        return self._renamed_prop

    @original_name.setter
    def original_name_setter(self, val: Str):
        self._renamed_prop = val

    @dbus_property(disabled=True)
    def disabled_prop(self) -> Str:
        return self._disabled_prop

    @disabled_prop.setter
    def disabled_prop(self, val: Str):
        self._disabled_prop = val

    @dbus_property(disabled=True)
    def throws_error(self) -> Str:
        raise DBusError('test.error', 'told you so')

    @throws_error.setter
    def throws_error(self, val: Str):
        raise DBusError('test.error', 'told you so')

    @method()
    async def do_emit_properties_changed(self):
        changed = {'string_prop': 'asdf'}
        invalidated = ['container_prop']
        await self.emit_properties_changed(changed, invalidated)


@pytest.mark.anyio
async def test_property_methods():
    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:

        interface = ExampleInterface('test.interface')
        export_path = '/test/path'
        await bus1.export(export_path, interface)

        async def call_properties(member, signature, body):
            return await bus2.call(
                Message(
                    destination=bus1.unique_name,
                    path=export_path,
                    interface='org.freedesktop.DBus.Properties',
                    member=member,
                    signature=signature,
                    body=body))

        result = await call_properties('GetAll', Str, [interface.name])
        assert result.message_type == MessageType.METHOD_RETURN, result.body[0]
        assert result.signature == 'a{sv}'
        assert result.body == [{
            'string_prop': Variant(Str, interface._string_prop),
            'readonly_prop': Variant(Int64, interface._readonly_prop),
            'container_prop': Variant('a(ss)', interface._container_prop),
            'renamed_prop': Variant(Str, interface._renamed_prop)
        }]

        result = await call_properties('Get', Tuple[Str, Str], [interface.name, 'string_prop'])
        assert result.message_type == MessageType.METHOD_RETURN, result.body[0]
        assert result.signature == 'v'
        assert result.body == [Variant(Str, 'hi')]

        result = await call_properties(
            'Set', 'ssv',
            [interface.name, 'string_prop', Variant(Str, 'ho')])
        assert result.message_type == MessageType.METHOD_RETURN, result.body[0]
        assert interface.string_prop == 'ho'

        with pytest.raises(DBusError) as err:
            await call_properties('Set', 'ssv',
                                  [interface.name, 'readonly_prop',
                                   Variant(Int64, 100)])
        result = err.value.reply
        assert result.message_type == MessageType.ERROR, result.body[0]
        assert result.error_name == ErrorType.PROPERTY_READ_ONLY.value, result.body[0]

        with pytest.raises(DBusError) as err:
            result = await call_properties('Set', 'ssv',
                                           [interface.name, 'disabled_prop',
                                            Variant(Str, 'asdf')])
        result = err.value.reply
        assert result.message_type == MessageType.ERROR, result.body[0]
        assert result.error_name == ErrorType.UNKNOWN_PROPERTY.value

        with pytest.raises(DBusError) as err:
            await call_properties(
                'Set', 'ssv',
                [interface.name, 'not_a_prop', Variant(Str, 'asdf')])
        result = err.value.reply
        assert result.message_type == MessageType.ERROR, result.body[0]
        assert result.error_name == ErrorType.UNKNOWN_PROPERTY.value

        # wrong type
        with pytest.raises(DBusError) as err:
            await call_properties(
                'Set', 'ssv',
                [interface.name, 'string_prop', Variant(Int64, 100)])
        result = err.value.reply
        assert result.message_type == MessageType.ERROR
        assert result.error_name == ErrorType.INVALID_SIGNATURE.value

        # enable the erroring property so we can test it
        for prop in ServiceInterface._get_properties(interface):
            if prop.name == 'throws_error':
                prop.disabled = False

        with pytest.raises(DBusError) as err:
            await call_properties(
                'Set', 'ssv',
                [interface.name, 'throws_error', Variant(Str, 'ho')])
        result = err.value.reply
        assert result.message_type == MessageType.ERROR, result.body[0]
        assert result.error_name == 'test.error'
        assert result.body == ['told you so']

        with pytest.raises(DBusError) as err:
            await call_properties('Get', Tuple[Str, Str], [interface.name, 'throws_error'])
        result = err.value.reply
        assert result.message_type == MessageType.ERROR, result.body[0]
        assert result.error_name == 'test.error'
        assert result.body == ['told you so']

        with pytest.raises(DBusError) as err:
            await call_properties('GetAll', Str, [interface.name])
        result = err.value.reply
        assert result.message_type == MessageType.ERROR, result.body[0]
        assert result.error_name == 'test.error'
        assert result.body == ['told you so']


@pytest.mark.anyio
async def test_property_changed_signal():
    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:

        await bus2.call(
            Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='AddMatch',
                signature=Str,
                body=[f'sender={bus1.unique_name}']))

        interface = ExampleInterface('test.interface')
        export_path = '/test/path'
        await bus1.export(export_path, interface)

        async def wait_for_message():
            # TODO timeout
            evt = anyio.create_event()
            sig = None

            async def message_handler(signal):
                nonlocal sig
                if signal.interface == 'org.freedesktop.DBus.Properties':
                    bus2.remove_message_handler(message_handler)
                    sig = signal
                    evt.set()

            bus2.add_message_handler(message_handler)
            await evt.wait()
            return sig

        await bus2.send(
            Message(
                destination=bus1.unique_name,
                interface=interface.name,
                path=export_path,
                member='do_emit_properties_changed'))

        signal = await wait_for_message()
        assert signal.interface == 'org.freedesktop.DBus.Properties'
        assert signal.member == 'PropertiesChanged'
        assert signal.signature == 'sa{sv}as'
        assert signal.body == [
            interface.name, {
                'string_prop': Variant(Str, 'asdf')
            }, ['container_prop']
        ]
