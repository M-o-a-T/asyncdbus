from asyncdbus import Message, DBusError, MessageBus
from asyncdbus.service import ServiceInterface, dbus_property, PropertyAccess
from asyncdbus.signature import Str

import pytest


class ExampleInterface(ServiceInterface):
    def __init__(self):
        super().__init__('test.interface')
        self._some_property = 'foo'
        self.error_name = 'test.error'
        self.error_text = 'i am bad'
        self._int64_property = -10000

    @dbus_property()
    def SomeProperty(self) -> Str:
        return self._some_property

    @SomeProperty.setter
    def SomeProperty(self, val: Str):
        self._some_property = val

    @dbus_property(access=PropertyAccess.READ)
    def Int64Property(self) -> 'x':
        return self._int64_property

    @dbus_property()
    def ErrorThrowingProperty(self) -> Str:
        raise DBusError(self.error_name, self.error_text)

    @ErrorThrowingProperty.setter
    def ErrorThrowingProperty(self, val: Str):
        raise DBusError(self.error_name, self.error_text)


@pytest.mark.anyio
async def test_aio_properties():
    async with MessageBus().connect() as service_bus:
        service_interface = ExampleInterface()
        await service_bus.export('/test/path', service_interface)

        async with MessageBus().connect() as bus:
            obj = await bus.get_proxy_object(service_bus.unique_name, '/test/path',
                                             service_bus._introspect_export_path('/test/path'))
            interface = await obj.get_interface(service_interface.name)

            prop = await interface.get_some_property()
            assert prop == service_interface._some_property

            prop = await interface.get_int64_property()
            assert prop == service_interface._int64_property

            await interface.set_some_property('different')
            assert service_interface._some_property == 'different'

            with pytest.raises(DBusError):
                try:
                    prop = await interface.get_error_throwing_property()
                    assert False, prop
                except DBusError as e:
                    assert e.type == service_interface.error_name
                    assert e.text == service_interface.error_text
                    assert type(e.reply) is Message
                    raise e

            with pytest.raises(DBusError):
                try:
                    await interface.set_error_throwing_property('different')
                except DBusError as e:
                    assert e.type == service_interface.error_name
                    assert e.text == service_interface.error_text
                    assert type(e.reply) is Message
                    raise e
