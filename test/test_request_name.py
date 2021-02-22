from asyncdbus import Message, MessageBus, MessageType, NameFlag, RequestNameReply, ReleaseNameReply
from asyncdbus.signature import Str

import pytest


@pytest.mark.anyio
async def test_name_requests():
    test_name = 'test.request.name'

    async with MessageBus().connect() as bus1, \
            MessageBus().connect() as bus2:

        async def get_name_owner(name):
            reply = await bus1.call(
                Message(
                    destination='org.freedesktop.DBus',
                    path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus',
                    member='GetNameOwner',
                    signature=Str,
                    body=[name]))

            assert reply.message_type == MessageType.METHOD_RETURN
            return reply.body[0]

        reply = await bus1.request_name(test_name)
        assert reply == RequestNameReply.PRIMARY_OWNER
        reply = await bus1.request_name(test_name)
        assert reply == RequestNameReply.ALREADY_OWNER

        reply = await bus2.request_name(test_name, NameFlag.ALLOW_REPLACEMENT)
        assert reply == RequestNameReply.IN_QUEUE

        reply = await bus1.release_name(test_name)
        assert reply == ReleaseNameReply.RELEASED

        reply = await bus1.release_name('name.doesnt.exist')
        assert reply == ReleaseNameReply.NON_EXISTENT

        reply = await bus1.release_name(test_name)
        assert reply == ReleaseNameReply.NOT_OWNER

        new_owner = await get_name_owner(test_name)
        assert new_owner == bus2.unique_name

        reply = await bus1.request_name(test_name, NameFlag.DO_NOT_QUEUE)
        assert reply == RequestNameReply.EXISTS

        reply = await bus1.request_name(test_name,
                                        NameFlag.DO_NOT_QUEUE | NameFlag.REPLACE_EXISTING)
        assert reply == RequestNameReply.PRIMARY_OWNER
