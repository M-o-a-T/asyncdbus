#!/usr/bin/env python3
import sys
import os
import anyio

sys.path.append(os.path.abspath(os.path.dirname(__file__) + '/..'))

from asyncdbus.validators import (is_bus_name_valid, is_member_name_valid, is_object_path_valid,
                                  is_interface_name_valid)
from asyncdbus import MessageBus, NameNotFoundError
from asyncdbus import BusType
from argparse import ArgumentParser

parser = ArgumentParser()

parser.add_argument('--system', help='Use the system bus', action='store_true')
parser.add_argument('--session', help='Use the session bus', action='store_true')
parser.add_argument('name', help='The name to look up')

args = parser.parse_args()


def exit_error(message):
    parser.print_usage()
    print()
    print(message)
    sys.exit(1)

bus_type = BusType.DETECT

if args.session:
    bus_type = BusType.SESSION
if args.system:
    bus_type = BusType.SYSTEM

name = args.name

async def main():
    ret = 0
    async with MessageBus(bus_type=bus_type).connect() as bus:
        try:
            bus_id = await bus.get_name_owner(name)
        except NameNotFoundError:
            print(f"{name !r} not found.", file=sys.stderr)
            ret = 1
        else:
            print(bus_id)
    sys.exit(ret)


anyio.run(main)
