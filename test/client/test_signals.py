from asyncdbus.service import ServiceInterface, signal
from asyncdbus import Message, MessageBus
from asyncdbus.introspection import Node
from asyncdbus.constants import RequestNameReply
from asyncdbus.signature import Str, Tuple

import pytest


class ExampleInterface(ServiceInterface):
    def __init__(self):
        super().__init__('test.interface')

    @signal()
    def SomeSignal(self) -> Str:
        return 'hello'

    @signal()
    def SignalMultiple(self) -> Tuple[Str, Str]:
        return ['hello', 'world']


@pytest.mark.anyio
async def test_signals():
    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:

        bus_intr = await bus1.introspect('org.freedesktop.DBus', '/org/freedesktop/DBus')
        bus_obj = await bus1.get_proxy_object('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                              bus_intr)
        stats = await bus_obj.get_interface('org.freedesktop.DBus.Debug.Stats')

        await bus1.request_name('test.signals.name')
        service_interface = ExampleInterface()
        await bus1.export('/test/path', service_interface)

        obj = await bus2.get_proxy_object('test.signals.name', '/test/path',
                                          bus1._introspect_export_path('/test/path'))
        interface = await obj.get_interface(service_interface.name)

        async def ping():
            await bus2.call(
                Message(
                    destination=bus1.unique_name,
                    interface='org.freedesktop.DBus.Peer',
                    path='/test/path',
                    member='Ping'))

        err = None

        single_counter = 0

        def single_handler(value):
            try:
                nonlocal single_counter
                nonlocal err
                assert value == 'hello'
                single_counter += 1
            except Exception as e:
                err = e

        multiple_counter = 0

        def multiple_handler(value1, value2):
            nonlocal multiple_counter
            nonlocal err
            try:
                assert value1 == 'hello'
                assert value2 == 'world'
                multiple_counter += 1
            except Exception as e:
                err = e

        await ping()
        match_rules = await stats.call_get_all_match_rules()
        assert bus2.unique_name in match_rules
        bus_match_rules = match_rules[bus2.unique_name]
        # the bus connection itself takes a rule on NameOwnerChange after the high
        # level client is initialized
        assert len(bus_match_rules) == 1
        assert len(bus2._user_message_handlers) == 0

        await interface.on_some_signal(single_handler)
        await interface.on_signal_multiple(multiple_handler)

        # Interlude: adding a signal handler with `on_[signal]` should add a match rule and
        # message handler. Removing a signal handler with `off_[signal]` should
        # remove the match rule and message handler to avoid memory leaks.
        await ping()
        match_rules = await stats.call_get_all_match_rules()
        assert bus2.unique_name in match_rules
        bus_match_rules = match_rules[bus2.unique_name]
        # test the match rule and user handler has been added
        assert len(bus_match_rules) == 2
        assert "type='signal',interface='test.interface',path='/test/path',sender='test.signals.name'" in bus_match_rules
        assert len(bus2._user_message_handlers) == 1

        await service_interface.SomeSignal()
        await ping()
        assert err is None
        assert single_counter == 1

        await service_interface.SignalMultiple()
        await ping()
        assert err is None
        assert multiple_counter == 1

        # special case: another bus with the same path and interface but on a
        # different name and connection will trigger the match rule of the first
        # (happens with mpris)
        async with MessageBus().connect() as bus3:
            await bus3.request_name('test.signals.name2')
            service_interface2 = ExampleInterface()
            await bus3.export('/test/path', service_interface2)

            obj = await bus2.get_proxy_object('test.signals.name2', '/test/path',
                                              bus3._introspect_export_path('/test/path'))
            # we have to add a dummy handler to add the match rule
            iface2 = await obj.get_interface(service_interface2.name)

            def dummy_signal_handler(what):
                pass

            await iface2.on_some_signal(dummy_signal_handler)
            await ping()

            await service_interface2.SomeSignal()
            await ping()
            # single_counter is not incremented for signals of the second interface
            assert single_counter == 1

            await interface.off_some_signal(single_handler)
            await interface.off_signal_multiple(multiple_handler)
            await iface2.off_some_signal(dummy_signal_handler)

            # After `off_[signal]`, the match rule and user handler should be removed
            await ping()
            match_rules = await stats.call_get_all_match_rules()
            assert bus2.unique_name in match_rules
            bus_match_rules = match_rules[bus2.unique_name]
            assert len(bus_match_rules) == 1
            assert "type='signal',interface='test.interface',path='/test/path',sender='test.signals.name'" not in bus_match_rules
            assert len(bus2._user_message_handlers) == 0


@pytest.mark.anyio
async def test_signals_with_changing_owners():
    well_known_name = 'test.signals.changing.name'

    async with MessageBus().connect() as bus1, MessageBus().connect() as bus3:
        async with MessageBus().connect() as bus2:

            async def ping():
                await bus1.call(
                    Message(
                        destination=bus1.unique_name,
                        interface='org.freedesktop.DBus.Peer',
                        path='/test/path',
                        member='Ping'))

            service_interface = ExampleInterface()
            introspection = Node.default()
            introspection.interfaces.append(service_interface.introspect())

            # get the interface before export
            obj = await bus1.get_proxy_object(well_known_name, '/test/path', introspection)
            iface = await obj.get_interface('test.interface')
            counter = 0

            def handler(what):
                nonlocal counter
                counter += 1

            await iface.on_some_signal(handler)
            await ping()

            # now export and get the name
            await bus2.export('/test/path', service_interface)
            result = await bus2.request_name(well_known_name)
            assert result is RequestNameReply.PRIMARY_OWNER

            # the signal should work
            await service_interface.SomeSignal()
            await ping()
            assert counter == 1
            counter = 0

            # now queue up a transfer of the name
            service_interface2 = ExampleInterface()
            await bus3.export('/test/path', service_interface2)
            result = await bus3.request_name(well_known_name)
            assert result is RequestNameReply.IN_QUEUE

            # if it doesn't own the name, the signal shouldn't work here
            await service_interface2.SomeSignal()
            await ping()
            assert counter == 0

        # here bus 2 is disconnected
        # now transfer over the name and it should work
        await ping()

        await service_interface2.SomeSignal()
        await ping()
        assert counter == 1
        counter = 0
