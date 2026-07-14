from types import SimpleNamespace

import pytest

import weflow_maibot_bridge.router as router_module
from weflow_maibot_bridge.router import RouterService


async def handler(_message):
    return None


def test_router_rejects_socketio_maim_message(monkeypatch) -> None:
    monkeypatch.setattr(router_module.maim_message, "__version__", "0.7.5")
    config = SimpleNamespace(platform="weflow", url="ws://127.0.0.1:8000/ws", token="")
    with pytest.raises(RuntimeError, match="HTTP 404"):
        RouterService(config, handler)
