The Message Bus
===============

.. toctree::
   :maxdepth: 2

   message-bus.rst

The message bus manages a connection to the DBus daemon. It's capable of sending and receiving messages and wiring up the classes of the high level interfaces.

The async bus implementation uses `anyio <https://anyio.readthedocs.io>`_ under the hood, thus it is compatible with both `asyncio <https://docs.python.org/3/library/asyncio.html>`_ and `trio <https://trio.readthedocs.io>`_. Use :class:`MessageBus <asyncdbus.MessageBus>` to connect to the bus.

