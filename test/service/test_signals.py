from asyncdbus.service import ServiceInterface, signal, SignalDisabledError, dbus_property
from asyncdbus.aio import MessageBus, ValueEvent
from asyncdbus import Message, MessageType
from asyncdbus.constants import PropertyAccess
from asyncdbus.signature import Variant

import pytest
import anyio
from contextlib import asynccontextmanager


class ExampleInterface(ServiceInterface):
    def __init__(self, name):
        super().__init__(name)

    @signal()
    def signal_empty(self):
        assert type(self) is ExampleInterface

    @signal()
    def signal_simple(self) -> 's':
        assert type(self) is ExampleInterface
        return 'hello'

    @signal()
    def signal_multiple(self) -> 'ss':
        assert type(self) is ExampleInterface
        return ['hello', 'world']

    @signal(name='renamed')
    def original_name(self):
        assert type(self) is ExampleInterface

    @signal(disabled=True)
    def signal_disabled(self):
        assert type(self) is ExampleInterface

    @dbus_property(access=PropertyAccess.READ)
    def test_prop(self) -> 'i':
        return 42


class SecondExampleInterface(ServiceInterface):
    def __init__(self, name):
        super().__init__(name)

    @dbus_property(access=PropertyAccess.READ)
    def str_prop(self) -> 's':
        return "abc"

    @dbus_property(access=PropertyAccess.READ)
    def list_prop(self) -> 'ai':
        return [1, 2, 3]


class ExpectMessage:
    def __init__(self, bus1, bus2, interface_name, timeout=1):
        self.future = ValueEvent()
        self.bus1 = bus1
        self.bus2 = bus2
        self.interface_name = interface_name
        self.timeout = timeout
        self.timeout_task = None
        self._ctx_ = None

    def message_handler(self, msg):
        if msg.sender == self.bus1.unique_name and msg.interface == self.interface_name:
            self.timeout_task.cancel()
            self.future.set(msg)
            return True

    async def timeout_handler(self, *, task_status):
        with anyio.open_cancel_scope() as self.timeout_task:
            task_status.started()
            await anyio.sleep(self.timeout)
            self.future.set_error(TimeoutError)

    @asynccontextmanager
    async def _ctx(self):
        self.bus2.add_message_handler(self.message_handler)
        try:
            async with anyio.create_task_group() as tg:
                await tg.start(self.timeout_handler)
                yield self.future
                tg.cancel_scope.cancel()
        finally:
            self.bus2.remove_message_handler(self.message_handler)

    async def __aenter__(self):
        if self._ctx_ is not None:
            raise RuntimeError("A ScopeSet can only be used once")
        self._ctx_ = self._ctx()
        return await self._ctx_.__aenter__()

    async def __aexit__(self, *tb):
        ctx, self._ctx_ = self._ctx_, None
        return await ctx.__aexit__(*tb)  # pylint:disable=no-member  # YES IT HAS


def assert_signal_ok(signal, export_path, member, signature, body):
    assert signal.message_type == MessageType.SIGNAL
    assert signal.path == export_path
    assert signal.member == member
    assert signal.signature == signature
    assert signal.body == body


@pytest.mark.anyio
async def test_signals():
  async with MessageBus().connect() as bus1, \
          MessageBus().connect() as bus2:

    interface = ExampleInterface('test.interface')
    export_path = '/test/path'
    bus1.export(export_path, interface)

    await bus2.call(
        Message(destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='AddMatch',
                signature='s',
                body=[f'sender={bus1.unique_name}']))

    async with ExpectMessage(bus1, bus2, interface.name) as expected_signal:
        interface.signal_empty()
        assert_signal_ok(signal=await expected_signal,
                         export_path=export_path,
                         member='signal_empty',
                         signature='',
                         body=[])

    async with ExpectMessage(bus1, bus2, interface.name) as expected_signal:
        interface.original_name()
        assert_signal_ok(signal=await expected_signal,
                         export_path=export_path,
                         member='renamed',
                         signature='',
                         body=[])

    async with ExpectMessage(bus1, bus2, interface.name) as expected_signal:
        interface.signal_simple()
        assert_signal_ok(signal=await expected_signal,
                         export_path=export_path,
                         member='signal_simple',
                         signature='s',
                         body=['hello'])

    async with ExpectMessage(bus1, bus2, interface.name) as expected_signal:
        interface.signal_multiple()
        assert_signal_ok(signal=await expected_signal,
                         export_path=export_path,
                         member='signal_multiple',
                         signature='ss',
                         body=['hello', 'world'])

    with pytest.raises(SignalDisabledError):
        interface.signal_disabled()


@pytest.mark.anyio
async def test_interface_add_remove_signal():
  async with MessageBus().connect() as bus1, \
          MessageBus().connect() as bus2:

    await bus2.call(
        Message(destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='AddMatch',
                signature='s',
                body=[f'sender={bus1.unique_name}']))

    first_interface = ExampleInterface('test.interface.first')
    second_interface = SecondExampleInterface('test.interface.second')
    export_path = '/test/path'

    # add first interface
    async with ExpectMessage(bus1, bus2, 'org.freedesktop.DBus.ObjectManager') as expected_signal:
        bus1.export(export_path, first_interface)
        assert_signal_ok(
            signal=await expected_signal,
            export_path=export_path,
            member='InterfacesAdded',
            signature='oa{sa{sv}}',
            body=[export_path, {
                'test.interface.first': {
                    'test_prop': Variant('i', 42)
                }
            }])

    # add second interface
    async with ExpectMessage(bus1, bus2, 'org.freedesktop.DBus.ObjectManager') as expected_signal:
        bus1.export(export_path, second_interface)
        assert_signal_ok(signal=await expected_signal,
                         export_path=export_path,
                         member='InterfacesAdded',
                         signature='oa{sa{sv}}',
                         body=[
                             export_path, {
                                 'test.interface.second': {
                                     'str_prop': Variant('s', "abc"),
                                     'list_prop': Variant('ai', [1, 2, 3])
                                 }
                             }
                         ])

    # remove single interface
    async with ExpectMessage(bus1, bus2, 'org.freedesktop.DBus.ObjectManager') as expected_signal:
        bus1.unexport(export_path, second_interface)
        assert_signal_ok(signal=await expected_signal,
                         export_path=export_path,
                         member='InterfacesRemoved',
                         signature='oas',
                         body=[export_path, ['test.interface.second']])

    # add second interface again
    async with ExpectMessage(bus1, bus2, 'org.freedesktop.DBus.ObjectManager') as expected_signal:
        bus1.export(export_path, second_interface)
        await expected_signal

    # remove multiple interfaces
    async with ExpectMessage(bus1, bus2, 'org.freedesktop.DBus.ObjectManager') as expected_signal:
        bus1.unexport(export_path)
        assert_signal_ok(signal=await expected_signal,
                         export_path=export_path,
                         member='InterfacesRemoved',
                         signature='oas',
                         body=[export_path, ['test.interface.first', 'test.interface.second']])
