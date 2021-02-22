#!/usr/bin/env python3
import sys
import os
sys.path.append(os.path.abspath(os.path.dirname(__file__) + '/..'))

from asyncdbus import Message, MessageType
from asyncdbus import MessageBus

import anyio
import json


async def main():
    async with MessageBus().connect() as bus:

        reply = await bus.call(
            Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='ListNames'))

        if reply.message_type == MessageType.ERROR:
            raise Exception(reply.body[0])

        print(json.dumps(reply.body[0], indent=2))


anyio.run(main)
