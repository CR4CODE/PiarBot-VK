"""Isolated, opt-in, short-lived records for connected Telegram chat monitoring."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time


TTL_OPTIONS = (3600, 86400, 604800)
DEFAULT_PREFS = {
    "enabled": False,
    "notify_edit": True,
    "notify_delete": True,
    "capture_media": True,
    "ttl": 86400,
}
TOGGLE_KEYS = frozenset(("enabled", "notify_edit", "notify_delete", "capture_media"))


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


class MonitorStore:
    def __init__(self, db_path: str | Path, *, media_root: str | Path | None = None):
        path = str(db_path).strip()
        if not path or path.casefold() == ":memory:" or path.casefold().startswith("file:"):
            raise ValueError("MonitorStore requires a persistent local SQLite file")
        self.db_path = Path(path)
        if self.db_path.is_symlink():
            raise ValueError("A symbolic link is not allowed for the monitor database")
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.db_path.exists() and not self.db_path.is_file():
            raise ValueError("Monitor database must be a regular file")
        first_run = not self.db_path.exists()
        self.db = sqlite3.connect(self.db_path, isolation_level=None)
        if first_run:
            os.chmod(self.db_path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.media_root = Path(media_root).resolve() if media_root is not None else None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                user_chat_id INTEGER NOT NULL,
                is_enabled INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preferences (
                owner_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                notify_edit INTEGER NOT NULL DEFAULT 1,
                notify_delete INTEGER NOT NULL DEFAULT 1,
                capture_media INTEGER NOT NULL DEFAULT 1,
                ttl INTEGER NOT NULL DEFAULT 86400
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_label TEXT NOT NULL DEFAULT '',
                original_body TEXT NOT NULL,
                current_body TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                media_path TEXT NOT NULL DEFAULT '',
                captured_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY(connection_id, chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS snapshots_expiration ON snapshots(expires_at);
            CREATE INDEX IF NOT EXISTS snapshots_owner ON snapshots(owner_id);
            CREATE TABLE IF NOT EXISTS event_claims (
                event_key TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
        """)

    def close(self) -> None:
        self.db.close()

    def record_connection(self, data: dict) -> bool:
        if not isinstance(data, dict):
            return False
        cid = data.get("id")
        user = data.get("user")
        uid = user.get("id") if isinstance(user, dict) else None
        owner_chat = data.get("user_chat_id")
        enabled = data.get("is_enabled")
        if (not isinstance(cid, str) or not 1 <= len(cid) <= 256
                or not _positive(uid) or not _positive(owner_chat)
                or type(enabled) is not bool):
            return False
        existing = self.get_connection(cid)
        if existing is not None and existing["owner_id"] != uid:
            self.purge_owner(existing["owner_id"])
        if not enabled:
            self.purge_owner(uid)
            self.db.execute("DELETE FROM connections WHERE id = ?", (cid,))
            return True
        self.db.execute(
            "INSERT INTO connections(id,owner_id,user_chat_id,is_enabled) VALUES (?,?,?,1) "
            "ON CONFLICT(id) DO UPDATE SET owner_id=excluded.owner_id,"
            "user_chat_id=excluded.user_chat_id,is_enabled=1",
            (cid, uid, owner_chat),
        )
        # Connecting the bot in Telegram enables its default monitoring
        # capability. A previously chosen pause is preserved on refresh.
        self.db.execute(
            "INSERT OR IGNORE INTO preferences(owner_id,enabled) VALUES (?,1)", (uid,),
        )
        return True

    def get_connection(self, cid: str) -> dict | None:
        if not isinstance(cid, str):
            return None
        row = self.db.execute(
            "SELECT id,owner_id,user_chat_id,is_enabled FROM connections WHERE id=?",
            (cid,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_connections_for_owner(self, owner_id: int) -> list[dict]:
        if not _positive(owner_id):
            return []
        rows = self.db.execute(
            "SELECT id,owner_id,user_chat_id,is_enabled FROM connections "
            "WHERE owner_id=? AND is_enabled=1 ORDER BY id", (owner_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_prefs(self, owner_id: int) -> dict:
        if not _positive(owner_id):
            return dict(DEFAULT_PREFS)
        row = self.db.execute(
            "SELECT enabled,notify_edit,notify_delete,capture_media,ttl "
            "FROM preferences WHERE owner_id=?", (owner_id,),
        ).fetchone()
        if row is None:
            return dict(DEFAULT_PREFS)
        return {k: bool(row[k]) if k in TOGGLE_KEYS else row[k] for k in DEFAULT_PREFS}

    def update_pref(self, owner_id: int, key: str, value: object) -> bool:
        if not _positive(owner_id):
            return False
        if key == "ttl":
            if type(value) is not int or value not in TTL_OPTIONS:
                return False
        elif key in TOGGLE_KEYS:
            if type(value) is not bool:
                return False
            if key == "enabled" and value and not self.get_connections_for_owner(owner_id):
                return False
        else:
            return False
        self.db.execute(
            "INSERT OR IGNORE INTO preferences(owner_id) VALUES (?)", (owner_id,),
        )
        # Key strictly allowlisted above; user input is never used as SQL.
        self.db.execute(f"UPDATE preferences SET {key}=? WHERE owner_id=?",
                        (int(value), owner_id))
        if key == "enabled" and value is False:
            self.clear_snapshots(owner_id)
        return True

    def save_snapshot(self, owner_id: int, cid: str, chat_id: int, message_id: int,
                      sender_id: int, body: str, *, sender_label: str = "", kind: str = "",
                      media_path: str = "", now: int | None = None) -> bool:
        conn = self.get_connection(cid)
        prefs = self.get_prefs(owner_id)
        if (conn is None or conn["owner_id"] != owner_id or not conn["is_enabled"]
                or not prefs["enabled"] or not _positive(chat_id)
                or not _positive(message_id) or not _positive(sender_id)
                or not isinstance(body, str) or not isinstance(sender_label, str)
                or not isinstance(kind, str)
                or not isinstance(media_path, str)):
            return False
        stamp = int(time.time()) if now is None else int(now)
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO snapshots "
            "(connection_id,chat_id,message_id,owner_id,sender_id,sender_label,original_body,"
            "current_body,kind,media_path,captured_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, chat_id, message_id, owner_id, sender_id, sender_label[:200],
             body[:4096], body[:4096],
             kind[:32], media_path, stamp, stamp + prefs["ttl"]),
        )
        return cursor.rowcount == 1

    def get_snapshot(self, owner_id: int, cid: str, chat_id: int, message_id: int,
                     *, now: int | None = None) -> dict | None:
        if not _positive(owner_id):
            return None
        stamp = int(time.time()) if now is None else int(now)
        row = self.db.execute(
            "SELECT * FROM snapshots WHERE owner_id=? AND connection_id=? "
            "AND chat_id=? AND message_id=? AND expires_at>?",
            (owner_id, cid, chat_id, message_id, stamp),
        ).fetchone()
        return dict(row) if row is not None else None

    def update_current(self, owner_id: int, cid: str, chat_id: int,
                       message_id: int, body: str) -> None:
        if isinstance(body, str):
            self.db.execute(
                "UPDATE snapshots SET current_body=? WHERE owner_id=? AND "
                "connection_id=? AND chat_id=? AND message_id=?",
                (body[:4096], owner_id, cid, chat_id, message_id),
            )

    def claim_event(self, key: str, owner_id: int) -> bool:
        if not isinstance(key, str) or not 1 <= len(key) <= 255 or not _positive(owner_id):
            return False
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO event_claims(event_key,owner_id,created_at) "
            "VALUES(?,?,?)", (key, owner_id, int(time.time())),
        )
        return cursor.rowcount == 1

    def _unlink(self, raw_path: str) -> None:
        if not raw_path:
            return
        path = Path(raw_path)
        # For production, refuse to remove paths outside a pinned media root.
        if self.media_root is not None:
            try:
                # systemd StateDirectory may expose /var/lib/<name> as a link
                # into /var/lib/private/<name>; compare canonical locations.
                path.resolve().relative_to(self.media_root)
            except (ValueError, OSError, RuntimeError):
                return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def clear_snapshots(self, owner_id: int) -> None:
        paths = self.db.execute(
            "SELECT media_path FROM snapshots WHERE owner_id=?", (owner_id,),
        ).fetchall()
        self.db.execute("DELETE FROM snapshots WHERE owner_id=?", (owner_id,))
        for row in paths:
            self._unlink(row["media_path"])

    def purge_owner(self, owner_id: int) -> None:
        if not _positive(owner_id):
            return
        self.clear_snapshots(owner_id)
        self.db.execute("DELETE FROM connections WHERE owner_id=?", (owner_id,))
        self.db.execute("DELETE FROM preferences WHERE owner_id=?", (owner_id,))
        self.db.execute("DELETE FROM event_claims WHERE owner_id=?", (owner_id,))

    def expire(self, *, now: int | None = None) -> int:
        stamp = int(time.time()) if now is None else int(now)
        rows = self.db.execute(
            "SELECT media_path FROM snapshots WHERE expires_at<=?", (stamp,),
        ).fetchall()
        self.db.execute("DELETE FROM snapshots WHERE expires_at<=?", (stamp,))
        for row in rows:
            self._unlink(row["media_path"])
        self.db.execute("DELETE FROM event_claims WHERE created_at<?",
                        (stamp - 7 * 86400,))
        return len(rows)
