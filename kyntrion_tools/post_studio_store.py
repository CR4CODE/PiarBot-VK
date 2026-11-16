"""Private, tenant-scoped SQLite records for KYNTRION Post Studio.

No chat history, downloaded media, Telegram tokens or credentials are stored.
Every public lookup uses the numeric Telegram actor ID as its tenant boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_DRAFT_BYTES = 64 * 1024
MAX_DRAFTS_PER_USER = 50
MAX_CHANNELS_PER_USER = 50
CONFIRMATION_TTL = 15 * 60


def _owner(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Telegram owner_id must be a positive integer")
    return value


def _channel(value: int) -> int:
    if type(value) is not int or value >= 0:
        raise ValueError("Telegram channel_id must be negative")
    return value


def _date_utc(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Date must be an ISO timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Date must be an ISO timestamp with timezone") from None
    if parsed.tzinfo is None:
        raise ValueError("Date must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_blob(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ValueError("Draft content must be JSON-compatible") from None
    if len(encoded.encode("utf-8")) > MAX_DRAFT_BYTES:
        raise ValueError("Draft exceeds size limit")
    return encoded


class PostStudioStore:
    def __init__(self, path: str | Path):
        raw = str(path).strip()
        if not raw or raw == ":memory:" or raw.startswith("file:"):
            raise ValueError("Post Studio requires a persistent SQLite path")
        self.path = Path(raw)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError("Invalid Post Studio database path")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=5000")
            self._migrate()
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _migrate(self) -> None:
        # Version 1 is intentionally additive: an existing database stays usable.
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                owner_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner_id, channel_id)
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                content_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(owner_id, channel_id)
                    REFERENCES channels(owner_id, channel_id)
            );
            CREATE INDEX IF NOT EXISTS drafts_by_owner ON drafts(owner_id, id);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                draft_id INTEGER NOT NULL,
                draft_version INTEGER NOT NULL,
                due_utc TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN
                    ('queued', 'inflight', 'published', 'failed', 'uncertain', 'cancelled')),
                attempt_id TEXT UNIQUE,
                receipt_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(draft_id) REFERENCES drafts(id)
            );
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state, due_utc);
            CREATE INDEX IF NOT EXISTS jobs_by_owner ON jobs(owner_id, id);
            CREATE TABLE IF NOT EXISTS publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                draft_id INTEGER NOT NULL,
                draft_version INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                state TEXT NOT NULL,
                attempt_id TEXT NOT NULL UNIQUE,
                message_ids_json TEXT NOT NULL DEFAULT '[]',
                receipt_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(draft_id, draft_version)
            );
            CREATE INDEX IF NOT EXISTS publications_by_owner ON publications(owner_id, id);
            CREATE TABLE IF NOT EXISTS confirmations (
                token TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                draft_id INTEGER NOT NULL,
                draft_version INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS confirmations_by_owner ON confirmations(owner_id);
            CREATE TABLE IF NOT EXISTS user_settings (
                owner_id INTEGER PRIMARY KEY,
                timezone TEXT NOT NULL DEFAULT 'UTC'
            );
            CREATE TABLE IF NOT EXISTS editor_sessions (
                owner_id INTEGER PRIMARY KEY,
                draft_id INTEGER NOT NULL,
                step TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(draft_id) REFERENCES drafts(id)
            );
            CREATE TABLE IF NOT EXISTS management_confirmations (
                token TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                publication_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedule_confirmations (
                token TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                draft_id INTEGER NOT NULL,
                draft_version INTEGER NOT NULL,
                due_utc TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS management_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                publication_id INTEGER NOT NULL,
                token TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                acked_ids_json TEXT NOT NULL DEFAULT '[]',
                receipt_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS management_by_owner
                ON management_ops(owner_id, publication_id);
            PRAGMA user_version=1;
        """)

    def connect_channel(self, owner_id: int, chat_id: int, title: str) -> None:
        _owner(owner_id)
        _channel(chat_id)
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Channel needs a title")
        with self._transaction():
            count = self.db.execute(
                "SELECT COUNT(*) FROM channels WHERE owner_id=?", (owner_id,)).fetchone()[0]
            existing = self.db.execute(
                "SELECT 1 FROM channels WHERE owner_id=? AND channel_id=?",
                (owner_id, chat_id)).fetchone()
            if count >= MAX_CHANNELS_PER_USER and not existing:
                raise ValueError("Channel quota exceeded")
            self.db.execute(
                "INSERT INTO channels(owner_id,channel_id,title,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(owner_id,channel_id) DO UPDATE SET "
                "title=excluded.title,updated_at=excluded.updated_at",
                (owner_id, chat_id, title.strip()[:200], _stamp()))

    def list_channels(self, owner_id: int) -> list[dict]:
        _owner(owner_id)
        rows = self.db.execute(
            "SELECT channel_id,title FROM channels WHERE owner_id=? ORDER BY title,channel_id",
            (owner_id,)).fetchall()
        return [dict(row) for row in rows]

    def create_draft(self, owner_id: int, channel_id: int) -> int:
        _owner(owner_id)
        _channel(channel_id)
        with self._transaction():
            linked = self.db.execute(
                "SELECT 1 FROM channels WHERE owner_id=? AND channel_id=?",
                (owner_id, channel_id)).fetchone()
            if not linked:
                raise LookupError("Channel is not connected for this user")
            count = self.db.execute(
                "SELECT COUNT(*) FROM drafts WHERE owner_id=?", (owner_id,)).fetchone()[0]
            if count >= MAX_DRAFTS_PER_USER:
                raise ValueError("Draft quota exceeded")
            stamp = _stamp()
            cursor = self.db.execute(
                "INSERT INTO drafts(owner_id,channel_id,content_json,created_at,updated_at) "
                "VALUES(?,?,'{}',?,?)", (owner_id, channel_id, stamp, stamp))
            return cursor.lastrowid

    @staticmethod
    def _draft_dict(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["content"] = json.loads(value.pop("content_json"))
        return value

    def get_draft(self, owner_id: int, draft_id: int) -> dict | None:
        _owner(owner_id)
        row = self.db.execute(
            "SELECT * FROM drafts WHERE owner_id=? AND id=?",
            (owner_id, draft_id)).fetchone()
        return self._draft_dict(row) if row is not None else None

    def list_drafts(self, owner_id: int) -> list[dict]:
        _owner(owner_id)
        rows = self.db.execute(
            "SELECT * FROM drafts WHERE owner_id=? ORDER BY id DESC",
            (owner_id,)).fetchall()
        return [self._draft_dict(row) for row in rows]

    def get_session(self, owner_id: int) -> dict | None:
        _owner(owner_id)
        row = self.db.execute(
            "SELECT owner_id,draft_id,step,updated_at FROM editor_sessions "
            "WHERE owner_id=?", (owner_id,)).fetchone()
        return dict(row) if row is not None else None

    def set_session(self, owner_id: int, draft_id: int, step: str = "editing") -> None:
        _owner(owner_id)
        if step not in ("editing", "text", "caption", "media", "button_add", "schedule"):
            raise ValueError("Unknown editor step")
        with self._transaction():
            row = self.db.execute(
                "SELECT 1 FROM drafts WHERE owner_id=? AND id=? AND status='draft'",
                (owner_id, draft_id)).fetchone()
            if row is None:
                raise LookupError("User cannot edit this draft")
            self.db.execute(
                "INSERT INTO editor_sessions(owner_id,draft_id,step,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET "
                "draft_id=excluded.draft_id,step=excluded.step,"
                "updated_at=excluded.updated_at",
                (owner_id, draft_id, step, _stamp()))

    def clear_session(self, owner_id: int) -> None:
        _owner(owner_id)
        self.db.execute("DELETE FROM editor_sessions WHERE owner_id=?", (owner_id,))

    def patch_draft(self, owner_id: int, draft_id: int, patch: dict,
                    expected_version: int) -> dict:
        _owner(owner_id)
        if not isinstance(patch, dict):
            raise ValueError("Draft patch must be an object")
        with self._transaction():
            row = self.db.execute(
                "SELECT version,content_json,status FROM drafts WHERE owner_id=? AND id=?",
                (owner_id, draft_id)).fetchone()
            if row is None:
                raise LookupError("Draft not found")
            if row["version"] != expected_version:
                raise ValueError("Draft version changed; reopen preview")
            if row["status"] != "draft":
                raise ValueError("Published draft cannot be overwritten")
            content = json.loads(row["content_json"])
            content.update(patch)
            data = _json_blob(content)
            self.db.execute(
                "UPDATE drafts SET content_json=?,version=version+1,updated_at=? "
                "WHERE owner_id=? AND id=? AND version=?",
                (data, _stamp(), owner_id, draft_id, expected_version))
        result = self.get_draft(owner_id, draft_id)
        assert result is not None
        return result

    def create_job(self, owner_id: int, draft_id: int, due_utc: str,
                   expected_version: int | None = None) -> int:
        _owner(owner_id)
        due = _date_utc(due_utc)
        with self._transaction():
            row = self.db.execute(
                "SELECT version,status FROM drafts WHERE owner_id=? AND id=?",
                (owner_id, draft_id)).fetchone()
            if row is None:
                raise LookupError("Draft not found")
            if row["status"] != "draft":
                raise ValueError("Only drafts can be scheduled")
            if expected_version is not None and row["version"] != expected_version:
                raise LookupError("Scheduled draft version is stale")
            stamp = _stamp()
            cursor = self.db.execute(
                "INSERT INTO jobs(owner_id,draft_id,draft_version,due_utc,state,"
                "created_at,updated_at) VALUES(?,?,?,?,'queued',?,?)",
                (owner_id, draft_id, row["version"], due, stamp, stamp))
            return cursor.lastrowid

    def create_schedule_confirmation(self, owner_id: int, draft_id: int,
                                     version: int, due_utc: str) -> str:
        _owner(owner_id)
        due = _date_utc(due_utc)
        with self._transaction():
            row = self.db.execute(
                "SELECT version,status FROM drafts WHERE owner_id=? AND id=?",
                (owner_id, draft_id)).fetchone()
            if row is None or row["version"] != version or row["status"] != "draft":
                raise LookupError("Draft changed before scheduling")
            token = secrets.token_urlsafe(24)
            self.db.execute(
                "INSERT INTO schedule_confirmations "
                "(token,owner_id,draft_id,draft_version,due_utc,expires_at) "
                "VALUES(?,?,?,?,?,?)",
                (token, owner_id, draft_id, version, due,
                 int(time.time()) + CONFIRMATION_TTL))
            return token

    def consume_schedule_confirmation(self, owner_id: int, token: str) -> dict | None:
        _owner(owner_id)
        if not isinstance(token, str) or len(token) > 128:
            return None
        with self._transaction():
            row = self.db.execute(
                "SELECT * FROM schedule_confirmations WHERE owner_id=? AND token=?",
                (owner_id, token)).fetchone()
            if row is None:
                return None
            self.db.execute("DELETE FROM schedule_confirmations WHERE token=?", (token,))
            if row["expires_at"] < int(time.time()):
                return None
            return dict(row)

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict:
        job = dict(row)
        job["receipt"] = json.loads(job.pop("receipt_json") or "null")
        return job

    def get_job(self, owner_id: int, job_id: int) -> dict | None:
        _owner(owner_id)
        row = self.db.execute(
            "SELECT * FROM jobs WHERE owner_id=? AND id=?", (owner_id, job_id)).fetchone()
        return self._job_dict(row) if row is not None else None

    def list_jobs(self, owner_id: int) -> list[dict]:
        _owner(owner_id)
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE owner_id=? ORDER BY due_utc,id",
            (owner_id,)).fetchall()
        return [self._job_dict(row) for row in rows]

    def claim_due(self, now_utc: str) -> list[dict]:
        now = _date_utc(now_utc)
        with self._transaction():
            rows = self.db.execute(
                "SELECT id FROM jobs WHERE state='queued' AND due_utc<=? "
                "ORDER BY due_utc,id LIMIT 20", (now,)).fetchall()
            claimed = []
            for row in rows:
                attempt_id = uuid.uuid4().hex
                self.db.execute(
                    "UPDATE jobs SET state='inflight',attempt_id=?,updated_at=? "
                    "WHERE id=? AND state='queued'", (attempt_id, _stamp(), row["id"]))
                current = self.db.execute(
                    "SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
                claimed.append(self._job_dict(current))
            return claimed

    def finish_job(self, job_id: int, state: str, receipt: dict | None) -> None:
        if type(job_id) is not int or job_id <= 0:
            raise ValueError("Invalid job ID")
        if state not in ("published", "failed", "uncertain"):
            raise ValueError("Invalid final job state")
        with self._transaction():
            result = self.db.execute(
                "UPDATE jobs SET state=?,receipt_json=?,updated_at=? "
                "WHERE id=? AND state='inflight'",
                (state, _json_blob(receipt), _stamp(), job_id))
            if result.rowcount != 1:
                raise LookupError("Job was not claimed or is already finished")

    def cancel_job(self, owner_id: int, job_id: int) -> bool:
        _owner(owner_id)
        with self._transaction():
            result = self.db.execute(
                "UPDATE jobs SET state='cancelled',updated_at=? "
                "WHERE owner_id=? AND id=? AND state='queued'",
                (_stamp(), owner_id, job_id))
            return result.rowcount == 1

    def create_confirmation(self, owner_id: int, draft_id: int, version: int) -> str:
        _owner(owner_id)
        with self._transaction():
            row = self.db.execute(
                "SELECT version,status FROM drafts WHERE owner_id=? AND id=?",
                (owner_id, draft_id)).fetchone()
            if not row or row["version"] != version or row["status"] != "draft":
                raise LookupError("Draft or version is unavailable")
            token = secrets.token_urlsafe(16)
            self.db.execute(
                "INSERT INTO confirmations(token,owner_id,draft_id,draft_version,expires_at) "
                "VALUES(?,?,?,?,?)",
                (token, owner_id, draft_id, version, int(time.time()) + CONFIRMATION_TTL))
            return token

    def consume_confirmation(self, owner_id: int, token: str) -> dict | None:
        _owner(owner_id)
        if not isinstance(token, str) or not token:
            return None
        with self._transaction():
            row = self.db.execute(
                "SELECT draft_id,draft_version,expires_at FROM confirmations "
                "WHERE owner_id=? AND token=?", (owner_id, token)).fetchone()
            if row is None:
                return None
            self.db.execute(
                "DELETE FROM confirmations WHERE owner_id=? AND token=?", (owner_id, token))
            if row["expires_at"] <= int(time.time()):
                return None
            return {"draft_id": row["draft_id"], "version": row["draft_version"]}

    @staticmethod
    def _publication_dict(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["message_ids"] = json.loads(value.pop("message_ids_json"))
        value["receipt"] = json.loads(value.pop("receipt_json") or "null")
        return value

    def get_publication(self, owner_id: int, publication_id: int) -> dict | None:
        _owner(owner_id)
        row = self.db.execute(
            "SELECT * FROM publications WHERE owner_id=? AND id=?",
            (owner_id, publication_id)).fetchone()
        return self._publication_dict(row) if row is not None else None

    def list_publications(self, owner_id: int) -> list[dict]:
        _owner(owner_id)
        rows = self.db.execute(
            "SELECT * FROM publications WHERE owner_id=? ORDER BY id DESC",
            (owner_id,)).fetchall()
        return [self._publication_dict(row) for row in rows]

    def latest_edit_receipt(self, owner_id: int, publication_id: int) -> dict | None:
        _owner(owner_id)
        row = self.db.execute(
            "SELECT receipt_json FROM management_ops WHERE owner_id=? AND publication_id=? "
            "AND state='edited' ORDER BY id DESC LIMIT 1", (owner_id, publication_id)).fetchone()
        return json.loads(row["receipt_json"]) if row and row["receipt_json"] else None

    def begin_publication(self, owner_id: int, draft_id: int, version: int) -> int | None:
        """Durably claim one revision before any Telegram send; never unclaim it."""
        _owner(owner_id)
        with self._transaction():
            row = self.db.execute(
                "SELECT channel_id,version,status FROM drafts "
                "WHERE owner_id=? AND id=?", (owner_id, draft_id)).fetchone()
            if row is None or row["version"] != version or row["status"] != "draft":
                return None
            existing = self.db.execute(
                "SELECT 1 FROM publications WHERE draft_id=? AND draft_version=?",
                (draft_id, version)).fetchone()
            if existing:
                return None
            stamp = _stamp()
            cursor = self.db.execute(
                "INSERT INTO publications(owner_id,draft_id,draft_version,"
                "channel_id,state,attempt_id,created_at,updated_at) "
                "VALUES(?,?,?,?,'inflight',?,?,?)",
                (owner_id, draft_id, version, row["channel_id"],
                 uuid.uuid4().hex, stamp, stamp))
            return cursor.lastrowid

    def record_sent_messages(self, owner_id: int, publication_id: int,
                             message_ids: list[int]) -> None:
        _owner(owner_id)
        if (not isinstance(message_ids, list) or not message_ids
                or any(type(value) is not int or value <= 0 for value in message_ids)):
            raise ValueError("Only real positive Telegram message IDs can be recorded")
        with self._transaction():
            row = self.db.execute(
                "SELECT message_ids_json FROM publications "
                "WHERE owner_id=? AND id=? AND state='inflight'",
                (owner_id, publication_id)).fetchone()
            if row is None:
                raise LookupError("Publication is not inflight")
            recorded = json.loads(row["message_ids_json"])
            recorded.extend(message_ids)
            self.db.execute(
                "UPDATE publications SET message_ids_json=?,updated_at=? "
                "WHERE owner_id=? AND id=? AND state='inflight'",
                (_json_blob(recorded), _stamp(), owner_id, publication_id))

    def finish_publication(self, owner_id: int, publication_id: int,
                           state: str, receipt: dict | None = None) -> None:
        _owner(owner_id)
        if state not in ("published", "failed", "uncertain"):
            raise ValueError("Invalid publication state")
        with self._transaction():
            row = self.db.execute(
                "SELECT draft_id,draft_version FROM publications "
                "WHERE owner_id=? AND id=? AND state='inflight'",
                (owner_id, publication_id)).fetchone()
            if row is None:
                raise LookupError("Publication has already ended")
            self.db.execute(
                "UPDATE publications SET state=?,receipt_json=?,updated_at=? "
                "WHERE owner_id=? AND id=? AND state='inflight'",
                (state, _json_blob(receipt), _stamp(), owner_id, publication_id))
            if state in ("published", "uncertain"):
                self.db.execute(
                    "UPDATE drafts SET status=?,updated_at=? "
                    "WHERE owner_id=? AND id=? AND version=?",
                    (state, _stamp(), owner_id, row["draft_id"], row["draft_version"]))

    def create_management_confirmation(self, owner_id: int, publication_id: int,
                                       kind: str, payload: object) -> str:
        _owner(owner_id)
        with self._transaction():
            published = self.db.execute(
                "SELECT 1 FROM publications WHERE owner_id=? AND id=? AND state='published'",
                (owner_id, publication_id)).fetchone()
            if published is None:
                raise LookupError("Publication not available for management")
            blocked = self.db.execute(
                "SELECT 1 FROM management_ops WHERE owner_id=? AND publication_id=? "
                "AND state IN ('inflight','uncertain')",
                (owner_id, publication_id)).fetchone()
            if blocked is not None:
                raise LookupError("Previous management outcome needs reconciliation")
            token = secrets.token_urlsafe(24)
            self.db.execute(
                "INSERT INTO management_confirmations "
                "(token,owner_id,publication_id,kind,payload_json,expires_at) "
                "VALUES(?,?,?,?,?,?)",
                (token, owner_id, publication_id, kind, _json_blob(payload),
                 int(time.time()) + CONFIRMATION_TTL))
        return token

    def consume_management_confirmation(self, owner_id: int, token: str) -> dict | None:
        _owner(owner_id)
        if not isinstance(token, str) or len(token) > 128:
            return None
        with self._transaction():
            row = self.db.execute(
                "SELECT * FROM management_confirmations WHERE owner_id=? AND token=?",
                (owner_id, token)).fetchone()
            if row is None:
                return None
            self.db.execute("DELETE FROM management_confirmations WHERE token=?", (token,))
            if row["expires_at"] < int(time.time()):
                return None
            return {"token": token, "publication_id": row["publication_id"],
                    "kind": row["kind"], "payload": json.loads(row["payload_json"])}

    def begin_management(self, owner_id: int, publication_id: int,
                         token: str, kind: str) -> int | None:
        _owner(owner_id)
        with self._transaction():
            row = self.db.execute(
                "SELECT state FROM publications WHERE owner_id=? AND id=?",
                (owner_id, publication_id)).fetchone()
            if row is None or row["state"] != "published":
                return None
            blocked = self.db.execute(
                "SELECT 1 FROM management_ops WHERE owner_id=? AND publication_id=? "
                "AND state IN ('inflight','uncertain')",
                (owner_id, publication_id)).fetchone()
            if blocked is not None:
                return None
            stamp = _stamp()
            cursor = self.db.execute(
                "INSERT INTO management_ops "
                "(owner_id,publication_id,token,kind,state,created_at,updated_at) "
                "VALUES(?,?,?,?,'inflight',?,?)",
                (owner_id, publication_id, token, kind, stamp, stamp))
            return cursor.lastrowid

    def record_management_ack(self, owner_id: int, operation_id: int,
                              message_id: int) -> None:
        _owner(owner_id)
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("Expected positive Telegram message ID")
        with self._transaction():
            row = self.db.execute(
                "SELECT acked_ids_json FROM management_ops "
                "WHERE owner_id=? AND id=? AND state='inflight'",
                (owner_id, operation_id)).fetchone()
            if row is None:
                raise LookupError("Management operation not inflight")
            ids = json.loads(row["acked_ids_json"])
            ids.append(message_id)
            self.db.execute(
                "UPDATE management_ops SET acked_ids_json=?,updated_at=? "
                "WHERE owner_id=? AND id=?",
                (_json_blob(ids), _stamp(), owner_id, operation_id))

    def finish_management(self, owner_id: int, operation_id: int, state: str,
                          receipt: dict | None = None) -> None:
        _owner(owner_id)
        if state not in ("edited", "deleted", "failed", "uncertain"):
            raise ValueError("Invalid management result")
        with self._transaction():
            row = self.db.execute(
                "SELECT publication_id FROM management_ops "
                "WHERE owner_id=? AND id=? AND state='inflight'",
                (owner_id, operation_id)).fetchone()
            if row is None:
                raise LookupError("Management operation is not inflight")
            self.db.execute(
                "UPDATE management_ops SET state=?,receipt_json=?,updated_at=? "
                "WHERE owner_id=? AND id=?",
                (state, _json_blob(receipt), _stamp(), owner_id, operation_id))
            if state == "deleted":
                self.db.execute(
                    "UPDATE publications SET state='deleted',updated_at=? "
                    "WHERE owner_id=? AND id=? AND state='published'",
                    (_stamp(), owner_id, row["publication_id"]))

    def get_timezone(self, owner_id: int) -> str:
        _owner(owner_id)
        row = self.db.execute(
            "SELECT timezone FROM user_settings WHERE owner_id=?", (owner_id,)).fetchone()
        return row["timezone"] if row else "UTC"

    def get_metrics(self, owner_id: int) -> dict[str, int]:
        """Minimal private counters; Telegram does not expose post views here."""
        _owner(owner_id)
        def count(table: str, condition: str) -> int:
            return self.db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE owner_id=? AND {condition}",
                (owner_id,)).fetchone()["n"]
        return {
            "drafts": count("drafts", "status='draft'"),
            "scheduled": count("jobs", "state IN ('queued','inflight')"),
            "published": count("publications", "state='published'"),
            "failed": (count("jobs", "state='failed'") +
                       count("publications", "state='failed'")),
            "uncertain": (count("jobs", "state IN ('inflight','uncertain')") +
                          count("publications", "state IN ('inflight','uncertain')")),
        }

    def set_timezone(self, owner_id: int, name: str) -> None:
        _owner(owner_id)
        if not isinstance(name, str) or not 1 <= len(name) <= 64:
            raise ValueError("Invalid timezone")
        try:
            ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError, TypeError):
            raise ValueError("Timezone must be a valid IANA name") from None
        self.db.execute(
            "INSERT INTO user_settings(owner_id,timezone) VALUES(?,?) "
            "ON CONFLICT(owner_id) DO UPDATE SET timezone=excluded.timezone",
            (owner_id, name))

    def forget_user(self, owner_id: int) -> None:
        _owner(owner_id)
        with self._transaction():
            for table in ("editor_sessions", "confirmations", "management_confirmations",
                          "schedule_confirmations",
                          "management_ops", "publications", "jobs", "drafts",
                          "channels", "user_settings"):
                self.db.execute(f"DELETE FROM {table} WHERE owner_id=?", (owner_id,))
