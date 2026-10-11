import httpx
import pytest

from capital_hook import CapitalClient
from capital_hook.testing import FakeCapital


@pytest.fixture
def fake():
    return FakeCapital()


@pytest.fixture
def client(fake):
    return CapitalClient("me@example.com", "pw", "api-key", http=fake.client())


@pytest.fixture
def make_client(fake):
    """Build a client whose HTTP layer is the fake, except where `override(request)` returns a response."""
    def build(override=None, **kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            if override is not None:
                response = override(request)
                if response is not None:
                    return response
            return fake.handler(request)

        return CapitalClient("me@example.com", "pw", "api-key", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)
    return build
