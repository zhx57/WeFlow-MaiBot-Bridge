from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class OutboxItem:
    message_id: str
    payload: dict[str, Any]
    attempts: int


class Storage:
    def __init__(self, path: Path, queue_limit: int) -> None:
        self.path = path
        self.queue_limit = queue_limit
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute(
                "CREATE TABLE IF NOT EXISTS seen (message_id TEXT PRIMARY KEY, created REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS inbound_events (message_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                "state TEXT NOT NULL CHECK(state IN ('pending','processing','buffered','done','dead')), "
                "attempts INTEGER NOT NULL, next_try REAL NOT NULL, created REAL NOT NULL, last_error TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS id_map (identifier TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "chat_type TEXT NOT NULL CHECK(chat_type IN ('private','group')), updated REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS outbox (message_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                "state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','dead')), attempts INTEGER NOT NULL, "
                "next_try REAL NOT NULL, created REAL NOT NULL, last_error TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS dead_letters (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "direction TEXT NOT NULL, message_id TEXT, payload TEXT NOT NULL, error TEXT NOT NULL, created REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending_mention_media (message_id TEXT PRIMARY KEY, "
                "session_key TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS outbound_messages (message_id TEXT PRIMARY KEY, "
                "payload TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','dead')), "
                "attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, "
                "created REAL NOT NULL, last_error TEXT)"
            )
            db.execute("UPDATE outbox SET state='pending' WHERE state='sending'")
            db.execute("UPDATE outbound_messages SET state='pending' WHERE state='sending'")
            db.execute("UPDATE inbound_events SET state='pending' WHERE state IN ('processing','buffered')")

    def mark_seen(self, message_id: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO seen(message_id,created) VALUES(?,?)", (message_id, time.time())
            )
            return cursor.rowcount == 1

    def remember(self, identifier: str, name: str, chat_type: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO id_map(identifier,name,chat_type,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(identifier) DO UPDATE SET name=excluded.name,chat_type=excluded.chat_type,updated=excluded.updated",
                (identifier, name, chat_type, time.time()),
            )

    def resolve(self, identifier: str) -> tuple[str, str] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT name,chat_type FROM id_map WHERE identifier=?", (identifier,)
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def enqueue(self, message_id: str, payload: dict[str, Any]) -> bool:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM outbox WHERE state IN ('pending','sending')"
            ).fetchone()[0]
            existing = db.execute("SELECT 1 FROM outbox WHERE message_id=?", (message_id,)).fetchone()
            if existing:
                return False
            if pending >= self.queue_limit:
                raise RuntimeError("持久化入站 outbox 已满")
            db.execute(
                "INSERT INTO outbox(message_id,payload,state,attempts,next_try,created) "
                "VALUES(?,?,'pending',0,0,?)",
                (message_id, encoded, time.time()),
            )
            return True

    def enqueue_event(self, message_id: str, payload: dict[str, Any]) -> bool:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM inbound_events WHERE state IN ('pending','processing','buffered')"
            ).fetchone()[0]
            existing = db.execute("SELECT 1 FROM inbound_events WHERE message_id=?", (message_id,)).fetchone()
            if existing:
                return False
            if pending >= self.queue_limit:
                raise RuntimeError("持久化 SSE 事件队列已满")
            db.execute(
                "INSERT INTO inbound_events(message_id,payload,state,attempts,next_try,created) "
                "VALUES(?,?,'pending',0,0,?)", (message_id, encoded, time.time())
            )
            return True

    def claim_event(self) -> OutboxItem | None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT message_id,payload,attempts FROM inbound_events "
                "WHERE state='pending' AND next_try<=? ORDER BY created LIMIT 1", (time.time(),)
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE inbound_events SET state='processing' WHERE message_id=?", (row[0],))
        return OutboxItem(str(row[0]), json.loads(row[1]), int(row[2]))

    def event_buffered(self, message_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE inbound_events SET state='buffered' WHERE message_id=?", (message_id,))

    def event_done(self, message_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE inbound_events SET state='done',last_error=NULL WHERE message_id=?", (message_id,))

    def event_failed(self, item: OutboxItem, error: BaseException, max_attempts: int) -> None:
        attempts = item.attempts + 1
        error_text = str(error)[:2000]
        with self._lock, self._connect() as db:
            if attempts >= max_attempts:
                db.execute(
                    "UPDATE inbound_events SET state='dead',attempts=?,last_error=? WHERE message_id=?",
                    (attempts, error_text, item.message_id),
                )
                db.execute(
                    "INSERT INTO dead_letters(direction,message_id,payload,error,created) VALUES('weflow',?,?,?,?)",
                    (item.message_id, json.dumps(item.payload, ensure_ascii=False), error_text, time.time()),
                )
            else:
                db.execute(
                    "UPDATE inbound_events SET state='pending',attempts=?,next_try=?,last_error=? WHERE message_id=?",
                    (attempts, time.time() + min(300.0, 2.0**attempts), error_text, item.message_id),
                )

    def pending_event_count(self) -> int:
        with self._lock, self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM inbound_events WHERE state IN ('pending','processing')"
            ).fetchone()[0])

    def save_pending_mention_media(
        self, message_id: str, session_key: str, payload: dict[str, Any]
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO pending_mention_media(message_id,session_key,payload,created) "
                "VALUES(?,?,?,?)",
                (message_id, session_key, encoded, time.time()),
            )
            db.execute(
                "UPDATE inbound_events SET state='buffered' WHERE message_id=?", (message_id,)
            )

    def get_pending_mention_media(
        self, session_key: str, max_age: float
    ) -> list[tuple[str, dict[str, Any]]]:
        cutoff = time.time() - max_age
        with self._lock, self._connect() as db:
            expired = db.execute(
                "SELECT message_id FROM pending_mention_media WHERE session_key=? AND created<?",
                (session_key, cutoff),
            ).fetchall()
            if expired:
                db.executemany(
                    "UPDATE inbound_events SET state='done' WHERE message_id=?",
                    ((row[0],) for row in expired),
                )
                db.execute(
                    "DELETE FROM pending_mention_media WHERE session_key=? AND created<?",
                    (session_key, cutoff),
                )
            rows = db.execute(
                "SELECT message_id,payload FROM pending_mention_media "
                "WHERE session_key=? ORDER BY created", (session_key,)
            ).fetchall()
        return [(str(row[0]), json.loads(row[1])) for row in rows]

    def expire_pending_mention_media(self, max_age: float) -> int:
        cutoff = time.time() - max_age
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT message_id FROM pending_mention_media WHERE created<?", (cutoff,)
            ).fetchall()
            if not rows:
                return 0
            db.executemany(
                "UPDATE inbound_events SET state='done' WHERE message_id=?",
                ((row[0],) for row in rows),
            )
            db.execute("DELETE FROM pending_mention_media WHERE created<?", (cutoff,))
            return len(rows)

    def remove_pending_mention_media(self, message_ids: list[str]) -> None:
        if not message_ids:
            return
        with self._lock, self._connect() as db:
            db.executemany(
                "DELETE FROM pending_mention_media WHERE message_id=?",
                ((message_id,) for message_id in message_ids),
            )

    def enqueue_outbound_message(self, message_id: str, payload: dict[str, Any]) -> bool:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock, self._connect() as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM outbound_messages WHERE state IN ('pending','sending')"
            ).fetchone()[0]
            existing = db.execute(
                "SELECT 1 FROM outbound_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if existing:
                return False
            if pending >= self.queue_limit:
                raise RuntimeError("持久化微信出站队列已满")
            db.execute(
                "INSERT INTO outbound_messages(message_id,payload,state,created) "
                "VALUES(?,?,'pending',?)", (message_id, encoded, time.time())
            )
            return True

    def claim_outbound_message(self) -> OutboxItem | None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT message_id,payload,attempts FROM outbound_messages "
                "WHERE state='pending' AND next_try<=? ORDER BY created LIMIT 1",
                (time.time(),),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE outbound_messages SET state='sending' WHERE message_id=?", (row[0],)
            )
        return OutboxItem(str(row[0]), json.loads(row[1]), int(row[2]))

    def finish_outbound_message(self, message_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE outbound_messages SET state='sent',last_error=NULL "
                "WHERE message_id=? AND state='sending'", (message_id,)
            )

    def advance_outbound_message(self, message_id: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE outbound_messages SET payload=? WHERE message_id=? AND state='sending'",
                (encoded, message_id),
            )

    def pending_outbound_count(self) -> int:
        with self._lock, self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM outbound_messages WHERE state IN ('pending','sending')"
            ).fetchone()[0])

    def fail_outbound_message(self, item: OutboxItem, error: BaseException) -> None:
        error_text = str(error)[:2000]
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE outbound_messages SET state='dead',last_error=? "
                "WHERE message_id=? AND state='sending'", (error_text, item.message_id)
            )
            db.execute(
                "INSERT INTO dead_letters(direction,message_id,payload,error,created) "
                "VALUES('outbound',?,?,?,?)",
                (item.message_id, json.dumps(item.payload, ensure_ascii=False), error_text, time.time()),
            )

    def retry_outbound_message(
        self, item: OutboxItem, error: BaseException, max_attempts: int
    ) -> bool:
        attempts = item.attempts + 1
        if attempts >= max_attempts:
            self.fail_outbound_message(item, error)
            return False
        error_text = str(error)[:2000]
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE outbound_messages SET state='pending',attempts=?,next_try=?,last_error=? "
                "WHERE message_id=? AND state='sending'",
                (attempts, time.time() + min(30.0, 2.0**attempts), error_text, item.message_id),
            )
        return True

    def enqueue_batch(self, message_id: str, payload: dict[str, Any], event_ids: list[str]) -> bool:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM outbox WHERE message_id=?", (message_id,)).fetchone()
            inserted = False
            if not existing:
                pending = db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE state IN ('pending','sending')"
                ).fetchone()[0]
                if pending >= self.queue_limit:
                    raise RuntimeError("持久化入站 outbox 已满")
                db.execute(
                    "INSERT INTO outbox(message_id,payload,state,attempts,next_try,created) "
                    "VALUES(?,?,'pending',0,0,?)", (message_id, encoded, time.time())
                )
                inserted = True
            db.executemany(
                "UPDATE inbound_events SET state='done',last_error=NULL WHERE message_id=?",
                ((event_id,) for event_id in event_ids),
            )
            return inserted

    def claim_next(self, now: float | None = None) -> OutboxItem | None:
        now = time.time() if now is None else now
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT message_id,payload,attempts FROM outbox "
                "WHERE state='pending' AND next_try<=? ORDER BY created LIMIT 1", (now,)
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE outbox SET state='sending' WHERE message_id=?", (row[0],))
        return OutboxItem(str(row[0]), json.loads(row[1]), int(row[2]))

    def sent(self, message_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE outbox SET state='sent',last_error=NULL WHERE message_id=?", (message_id,))

    def failed(self, item: OutboxItem, error: BaseException, max_attempts: int) -> None:
        attempts = item.attempts + 1
        error_text = str(error)[:2000]
        with self._lock, self._connect() as db:
            if attempts >= max_attempts:
                db.execute(
                    "UPDATE outbox SET state='dead',attempts=?,last_error=? WHERE message_id=?",
                    (attempts, error_text, item.message_id),
                )
                db.execute(
                    "INSERT INTO dead_letters(direction,message_id,payload,error,created) VALUES('inbound',?,?,?,?)",
                    (item.message_id, json.dumps(item.payload, ensure_ascii=False), error_text, time.time()),
                )
            else:
                db.execute(
                    "UPDATE outbox SET state='pending',attempts=?,next_try=?,last_error=? WHERE message_id=?",
                    (attempts, time.time() + min(300.0, 2.0**attempts), error_text, item.message_id),
                )

    def store_dead_letter(self, direction: str, message_id: str | None, payload: Any, error: BaseException) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO dead_letters(direction,message_id,payload,error,created) VALUES(?,?,?,?,?)",
                (direction, message_id, json.dumps(payload, ensure_ascii=False, default=str), str(error)[:2000], time.time()),
            )

    def outbox_state(self, message_id: str) -> str | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT state FROM outbox WHERE message_id=?", (message_id,)).fetchone()
        return str(row[0]) if row else None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level="DEFERRED")
