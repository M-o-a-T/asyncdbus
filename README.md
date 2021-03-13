# asyncdbus

This is a fork of python-dbus-next. It uses anyio under the hood, thus is
compatible with Trio as well as asyncio.

Support for GLib has been removed. If you need that, use Trio in guest
mode.

This library is available on PyPi as [asyncdbus](https://pypi.org/project/asyncdbus/).

## Installation

You need the upcoming (3.0) version of AnyIO, which is not yet in PyPi.

If you are not familiar how to install a python package directly from github, try `pipenv`:

1. Install pipenv
   ```bash
   sudo apt install pipenv         # on debian/ubuntu
   sudo dnf install pipenv         # on fedora
   pkg install py36-pipenv         # FreeBSD
   pip install pipenv              # others
   ```

2. Create virtual environment and install packages
   ```bash
   cd /your/project/directory
   pipenv install
   # the following calls might take a while
   # especially on a raspberry pi or similar platforms
   pipenv install -e git+https://github.com/agronholm/anyio@master#egg=anyio
   pipenv install asyncdbus
   ```
   You might want to install additional packages for your project in this environment using `pipenv` instead of `pip`.

3. Enter the environment **(you have to do this everytime you want to run or develop)**
   ```bash
   pipenv shell
   ```

The rest of this document is from dbus-next and has not been modified.

# python-dbus-next

The next great DBus library for Python.

[Documentation](https://python-dbus-next.readthedocs.io/en/latest/)

[Chat](https://discord.gg/UdbXHVX)

python-dbus-next is a Python library for DBus that aims to be a fully featured high level library primarily geared towards integration of applications into Linux desktop and mobile environments.

Desktop application developers can use this library for integrating their applications into desktop environments by implementing common DBus standard interfaces or creating custom plugin interfaces.

Desktop users can use this library to create their own scripts and utilities to interact with those interfaces for customization of their desktop environment.

python-dbus-next plans to improve over other DBus libraries for Python in the following ways:

* Zero dependencies and pure Python 3.
* Support for multiple IO backends including anyio and the GLib main loop.
* Nonblocking IO suitable for GUI development.
* Target the latest language features of Python for beautiful services and clients.
* Complete implementation of the DBus type system without ever guessing types.
* Integration tests for all features of the library.
* Completely documented public API.

## Installing

This library is available on PyPi as [dbus-next](https://pypi.org/project/dbus-next/).

```
pip3 install dbus-next
```

## The Client Interface

To use a service on the bus, the library constructs a proxy object you can use to call methods, get and set properties, and listen to signals.

For more information, see the [overview for the high-level client](https://python-dbus-next.readthedocs.io/en/latest/high-level-client/index.html).

This example connects to a media player and controls it with the [MPRIS](https://specifications.freedesktop.org/mpris-spec/latest/) DBus interface.

```python
from asyncdbus import MessageBus

import anyio


async def main():
    bus = await MessageBus().connect()
    # the introspection xml would normally be included in your project, but
    # this is convenient for development
    introspection = await bus.introspect('org.mpris.MediaPlayer2.vlc', '/org/mpris/MediaPlayer2')

    obj = await bus.get_proxy_object('org.mpris.MediaPlayer2.vlc', '/org/mpris/MediaPlayer2', introspection)
    player = await obj.get_interface('org.mpris.MediaPlayer2.Player')
    properties = await obj.get_interface('org.freedesktop.DBus.Properties')

    # call methods on the interface (this causes the media player to play)
    await player.call_play()

    volume = await player.get_volume()
    print(f'current volume: {volume}, setting to 0.5')

    await player.set_volume(0.5)

    # listen to signals
    def on_properties_changed(interface_name, changed_properties, invalidated_properties):
        for changed, variant in changed_properties.items():
            print(f'property changed: {changed} - {variant.value}')

    properties.on_properties_changed(on_properties_changed)

    await anyio.sleep(99999)

anyio.run(main)
```

## The Service Interface

To define a service on the bus, use the `ServiceInterface` class and decorate class methods to specify DBus methods, properties, and signals with their type signatures.

For more information, see the [overview for the high-level service](https://python-dbus-next.readthedocs.io/en/latest/high-level-service/index.html).

```python
from asyncdbus.service import ServiceInterface, method, dbus_property, signal, Variant
from asyncdbus import MessageBus

import anyio

class ExampleInterface(ServiceInterface):
    def __init__(self, name):
        super().__init__(name)
        self._string_prop = 'kevin'

    @method()
    def Echo(self, what: 's') -> 's':
        return what

    @method()
    def GetVariantDict() -> 'a{sv}':
        return {
            'foo': Variant('s', 'bar'),
            'bat': Variant('x', -55),
            'a_list': Variant('as', ['hello', 'world'])
        }

    @dbus_property()
    def string_prop(self) -> 's':
        return self._string_prop

    @string_prop.setter
    def string_prop_setter(self, val: 's'):
        self._string_prop = val

    @signal()
    def signal_simple(self) -> 's':
        return 'hello'

async def main():
    bus = await MessageBus().connect()
    interface = ExampleInterface('test.interface')
    await bus.export('/test/path', interface)
    # now that we are ready to handle requests, we can request name from D-Bus
    await bus.request_name('test.name')
    # wait indefinitely
    await anyio.sleep(99999)

anyio.run(main)
```

## The Low-Level Interface

The low-level interface works with DBus messages directly.

For more information, see the [overview for the low-level interface](https://python-dbus-next.readthedocs.io/en/latest/low-level-interface/index.html).

```python
from asyncdbus.message import Message, MessageType
from asyncdbus import MessageBus

import anyio
import json


async def main():
    bus = await MessageBus().connect()

    reply = await bus.call(
        Message(destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='ListNames'))

    if reply.message_type == MessageType.ERROR:
        raise Exception(reply.body[0])

    print(json.dumps(reply.body[0], indent=2))


anyio.run(main)
```

## Projects that use python-dbus-next

* The [Playerctl](https://github.com/altdesktop/playerctl) test suite
* [i3-dstatus](https://github.com/altdesktop/i3-dstatus)

## Contributing

Contributions are welcome. Development happens on [Github](https://github.com/altdesktop/python-dbus-next).

Before you commit, run `make` to run the linter, code formatter, and the test suite.

# Copyright

You can use this code under an MIT license (see LICENSE).

© 2019, Tony Crisci
