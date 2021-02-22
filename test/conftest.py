import pytest

# XXX TODO we can't (yet) skip this, because mixing asyncio and trio
# causes test/test_fd_passing.py::test_sending_file_descriptor_with_proxy
# to hang with Trio. To be investigated.


@pytest.fixture
def anyio_backend():
    return "trio"
