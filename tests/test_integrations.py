import pytest
from gateway.integrations import EchoHandler, HandlerRequest


@pytest.mark.asyncio
async def test_echo_handler_returns_input():
    request = HandlerRequest("b", "c", "personal", "u", "m", "hello")
    result = await EchoHandler().handle(request)
    assert result.status == "completed"
    assert result.text == "hello"
