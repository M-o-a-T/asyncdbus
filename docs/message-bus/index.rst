The Message Bus
===============

.. toctree::
   :maxdepth: 2

   base-message-bus.rst
   aio-message-bus.rst
   glib-message-bus.rst

The message bus manages a connection to the DBus daemon. It's capable of sending and receiving messages and wiring up the classes of the high level interfaces.

There are currently two implementations of the message bus depending on what main loop implementation you want to use. Use :class:`aio.MessageBus <asyncdbus.aio.MessageBus>` if you are using an async main loop. Use :class:`glib.MessageBus <asyncdbus.glib.MessageBus>` if you are using a GLib main loop.

For standalone applications, the async message bus is preferable because it has a nice async/await api in place of the callback/synchronous interface of the GLib message bus. If your application is using other libraries that use the GLib main loop, such as a GTK application, the GLib implementation will be needed. However neither library is a requirement.

The async bus implementation uses `anyio <https://anyio.readthedocs.io>`_ under the hood, thus it is compatible with both `asyncio <https://docs.python.org/3/library/asyncio.html>`_ and `trio <https://trio.readthedocs.io>`_.

For more information on how to use the message bus, see the documentation for the specific interfaces you plan to use.
