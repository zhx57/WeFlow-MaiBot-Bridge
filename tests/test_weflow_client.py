import asyncio
import json
from types import SimpleNamespace

from weflow_maibot_bridge.weflow import WeFlowClient


class FakeResponse:
    def __init__(self, lines):
        self.lines = lines
        self.status_code = 200

    def raise_for_status(self):
        return None

    def iter_lines(self, *, chunk_size, decode_unicode):
        assert chunk_size == 1
        assert decode_unicode is True
        yield from self.lines

    def close(self):
        return None

    def json(self):
        return {"status": "ok"}


def test_weflow_health_check_uses_documented_endpoint_without_talker_or_token(monkeypatch) -> None:
    config = SimpleNamespace(
        base_url="http://127.0.0.1:5031", access_token="token",
        connect_timeout=1, read_timeout=1,
        retry_min_seconds=0.01, retry_max_seconds=0.01,
    )
    client = WeFlowClient(config, SimpleNamespace())
    request = MockRequest(FakeResponse([]))
    monkeypatch.setattr("weflow_maibot_bridge.weflow.requests.get", request)

    client.check_api()

    assert request.args[0] == "http://127.0.0.1:5031/health"
    assert "params" not in request.kwargs


class MockRequest:
    def __init__(self, response):
        self.response = response
        self.args = ()
        self.kwargs = {}

    def __call__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return self.response


async def test_weflow_single_data_line_is_delivered_without_blank_line(monkeypatch) -> None:
    config = SimpleNamespace(
        base_url="http://127.0.0.1:5031",
        access_token="token",
        connect_timeout=1,
        read_timeout=1,
        retry_min_seconds=0.01,
        retry_max_seconds=0.01,
    )
    client = WeFlowClient(config, SimpleNamespace())
    payload = {"rawid": "one", "sourceName": "Alice", "content": "hello"}
    monkeypatch.setattr(
        "weflow_maibot_bridge.weflow.requests.get",
        lambda *args, **kwargs: FakeResponse([f"data: {json.dumps(payload)}"]),
    )
    received = []

    async def handler(event):
        received.append(event)
        client.stop()

    await asyncio.wait_for(client.run(handler), timeout=2)
    assert received[0]["rawid"] == "one"
    assert received[0]["content"] == "hello"


async def test_weflow_single_data_line_preserves_sse_id(monkeypatch) -> None:
    config = SimpleNamespace(
        base_url="http://127.0.0.1:5031", access_token="token",
        connect_timeout=1, read_timeout=1,
        retry_min_seconds=0.01, retry_max_seconds=0.01,
    )
    client = WeFlowClient(config, SimpleNamespace())
    payload = {"sourceName": "Alice", "content": "hello"}
    monkeypatch.setattr(
        "weflow_maibot_bridge.weflow.requests.get",
        lambda *args, **kwargs: FakeResponse(["id: evt-1", f"data: {json.dumps(payload)}"]),
    )
    received = []

    async def handler(event):
        received.append(event)
        client.stop()

    await asyncio.wait_for(client.run(handler), timeout=2)
    assert received[0]["_sse_event_id"] == "evt-1"
