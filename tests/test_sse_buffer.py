import asyncio

from weflow_maibot_bridge.buffer import DebounceBuffer
from weflow_maibot_bridge.models import InboundMessage, InboundPart
from weflow_maibot_bridge.sse import parse_sse


def message(sequence: int) -> InboundMessage:
    return InboundMessage(str(sequence), sequence, sequence, "private", "chat", "Alice", "alice", "Alice", InboundPart("text", str(sequence)))


def test_sse_multiline_event_id_and_retry() -> None:
    events = list(parse_sse([
        ": heartbeat\n", "id: evt-7\n", "event: wechat-message\n", "retry: 1500\n",
        "data: {\"text\":\n", "data: \"hello\"}\n", "\n",
    ]))
    assert len(events) == 1
    assert events[0].id == "evt-7"
    assert events[0].event == "wechat-message"
    assert events[0].retry == 1500
    assert events[0].data == '{"text":\n"hello"}'


async def test_debounce_keeps_arrival_during_processing() -> None:
    batches: list[list[int]] = []
    processing = asyncio.Event()
    release = asyncio.Event()

    async def handler(items):
        batches.append([item.sequence for item in items])
        if len(batches) == 1:
            processing.set()
            await release.wait()

    buffer = DebounceBuffer(0.01, handler)
    await buffer.add("chat", message(1))
    await processing.wait()
    await buffer.add("chat", message(2))
    release.set()
    await buffer.close(drain=True)
    assert batches == [[1], [2]]


async def test_debounce_orders_batch_by_sequence() -> None:
    batches = []
    buffer = DebounceBuffer(0.01, lambda items: _append(batches, items))
    await buffer.add("chat", message(2))
    await buffer.add("chat", message(1))
    await buffer.close(drain=True)
    assert [[item.sequence for item in batch] for batch in batches] == [[1, 2]]


async def test_debounce_recovers_after_handler_failure() -> None:
    attempts = 0
    delivered = []

    async def handler(items):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary storage failure")
        delivered.extend(item.sequence for item in items)

    buffer = DebounceBuffer(0.01, handler)
    await buffer.add("chat", message(1))
    await asyncio.sleep(0.05)
    await buffer.close(drain=True)
    assert attempts >= 2
    assert delivered == [1]


async def _append(target, value):
    target.append(value)
