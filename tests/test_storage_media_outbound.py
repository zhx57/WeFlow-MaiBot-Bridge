import base64
import time
from pathlib import Path

import pytest

from weflow_maibot_bridge.media import decode_base64_image, read_local_image, validate_image, validate_image_url
from weflow_maibot_bridge.outbound import parse_outbound_segments
from weflow_maibot_bridge.storage import Storage


PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def test_dedup_and_outbox_retry_dead_letter(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bridge.db", 2)
    storage.initialize()
    assert storage.mark_seen("m1")
    assert not storage.mark_seen("m1")
    assert storage.enqueue("m1", {"message_info": {}, "message_segment": {}})
    assert not storage.enqueue("m1", {})
    item = storage.claim_next(now=1)
    assert item and item.message_id == "m1"
    storage.failed(item, RuntimeError("offline"), max_attempts=1)
    assert storage.outbox_state("m1") == "dead"


def test_persisted_event_recovers_and_batch_commit_marks_done(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bridge.db", 10)
    storage.initialize()
    assert storage.enqueue_event("raw-event", {"rawid": "raw-event"})
    claimed = storage.claim_event()
    assert claimed and claimed.message_id == "raw-event"
    storage.event_buffered("raw-event")
    # Startup recovers both in-flight and in-memory buffered events.
    storage.initialize()
    recovered = storage.claim_event()
    assert recovered and recovered.payload["rawid"] == "raw-event"
    storage.event_buffered("raw-event")
    assert storage.enqueue_batch("batch-1", {"message_info": {}, "message_segment": {}}, ["raw-event"])
    assert storage.pending_event_count() == 0
    assert storage.outbox_state("batch-1") == "pending"


def test_pending_mention_media_persists_and_keeps_all_images(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bridge.db", 10)
    storage.initialize()
    for message_id in ("image-1", "image-2"):
        storage.enqueue_event(message_id, {"rawid": message_id})
        storage.claim_event()
        storage.save_pending_mention_media(message_id, "room", {"rawid": message_id})
    pending = storage.get_pending_mention_media("room", 60)
    assert [message_id for message_id, _ in pending] == ["image-1", "image-2"]
    with storage._connect() as db:
        db.execute("UPDATE pending_mention_media SET created=?", (time.time() - 120,))
    assert storage.expire_pending_mention_media(60) == 2
    assert storage.get_pending_mention_media("room", 60) == []


def test_outbound_message_is_deduplicated_and_tracks_part_progress(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bridge.db", 10)
    storage.initialize()
    payload = {"contact": "Alice", "parts": [{"kind": "text", "data": "a"}], "next_part": 0}
    assert storage.enqueue_outbound_message("reply-1", payload)
    assert not storage.enqueue_outbound_message("reply-1", payload)
    item = storage.claim_outbound_message()
    assert item and item.payload["next_part"] == 0
    item.payload["next_part"] = 1
    storage.advance_outbound_message(item.message_id, item.payload)
    storage.finish_outbound_message(item.message_id)
    assert storage.pending_outbound_count() == 0


def test_retry_safe_outbound_failure_returns_message_to_pending(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bridge.db", 10)
    storage.initialize()
    storage.enqueue_outbound_message("reply-1", {"parts": [], "next_part": 0})
    item = storage.claim_outbound_message()
    assert item
    assert storage.retry_outbound_message(item, RuntimeError("window unavailable"), 3)
    with storage._connect() as db:
        state, attempts = db.execute(
            "SELECT state,attempts FROM outbound_messages WHERE message_id='reply-1'"
        ).fetchone()
    assert (state, attempts) == ("pending", 1)


def test_media_magic_mime_size_base64_and_local_root(tmp_path: Path) -> None:
    image = validate_image(PNG, 1024, "image/png")
    assert image.mime == "image/png"
    assert decode_base64_image(base64.b64encode(PNG).decode(), 1024).raw == PNG
    with pytest.raises(ValueError, match="MIME"):
        validate_image(PNG, 1024, "image/jpeg")
    with pytest.raises(ValueError, match="尺寸"):
        validate_image(PNG, 2)
    path = tmp_path / "image.png"
    path.write_bytes(PNG)
    assert read_local_image(str(path), 1024, ()).raw == PNG
    assert read_local_image(str(path), 1024, (tmp_path,)).raw == PNG
    with pytest.raises(ValueError, match="允许目录"):
        read_local_image(str(path), 1024, (tmp_path / "other",))


def test_local_image_url_is_allowed() -> None:
    validate_image_url("http://127.0.0.1:8000/image.png")


def test_outbound_segment_order_reply_notify_and_emoji() -> None:
    parts = parse_outbound_segments({"type": "seglist", "data": [
        {"type": "reply", "data": "x"},
        {"type": "text", "data": " first "},
        {"type": "image", "data": ["a", "b"]},
        {"type": "notify", "data": {}},
        {"type": "emoji", "data": "[笑]"},
    ]})
    assert [(part.kind, part.data) for part in parts] == [
        ("text", "first"), ("image", "a"), ("image", "b"), ("text", "[笑]")
    ]
    with pytest.raises(ValueError, match="不支持"):
        parse_outbound_segments({"type": "file", "data": "x"})
