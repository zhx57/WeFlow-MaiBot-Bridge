import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from maim_message import BaseMessageInfo, MessageBase, ReceiverInfo, Seg, UserInfo

from weflow_maibot_bridge.app import BridgeApp
from weflow_maibot_bridge.storage import OutboxItem
from weflow_maibot_bridge.storage import Storage


async def test_outbox_worker_sends_claimed_message_and_marks_sent() -> None:
    app = BridgeApp.__new__(BridgeApp)
    app._stopping = asyncio.Event()
    app.config = SimpleNamespace(bridge=SimpleNamespace(max_attempts=3))
    message = MessageBase(BaseMessageInfo(platform="weflow", message_id="m1"), Seg("text", "hi"))
    item = OutboxItem("m1", message.to_dict(), 0)
    app.storage = SimpleNamespace(
        claim_next=Mock(side_effect=[item]),
        sent=Mock(),
        failed=Mock(),
    )

    async def send(_message):
        app._stopping.set()

    app.router = SimpleNamespace(send=AsyncMock(side_effect=send))
    await app._outbox_worker()

    app.router.send.assert_awaited_once()
    app.storage.sent.assert_called_once_with("m1")
    app.storage.failed.assert_not_called()


async def test_missing_message_id_does_not_merge_identical_replies(tmp_path: Path) -> None:
    app = BridgeApp.__new__(BridgeApp)
    app.config = SimpleNamespace(maibot=SimpleNamespace(platform="weflow"))
    app.storage = Storage(tmp_path / "bridge.db", 10)
    app.storage.initialize()
    app.storage.remember("alice-id", "Alice", "private")
    message = MessageBase(
        BaseMessageInfo(
            platform="weflow",
            receiver_info=ReceiverInfo(user_info=UserInfo(platform="weflow", user_id="alice-id")),
        ),
        Seg("text", "好的"),
    )
    await app._handle_maibot(message)
    await app._handle_maibot(message)
    with app.storage._connect() as db:
        count = db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0]
    assert count == 2
