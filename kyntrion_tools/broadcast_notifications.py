"""Release announcements for known bot private-chat users, with durable opt-out.

Never enumerate members of channels, groups, or Telegram Business client chats.
A campaign freezes a snapshot of previously observed private bot contacts.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from kyntrion_tools.telegram_api import TelegramApi, TelegramApiError, TelegramFloodWait

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
MIN_SEND_INTERVAL = 1.1


def _positive(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Expected positive Telegram user ID")
    return value


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Campaign text is empty")
    if len(text.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("Telegram text exceeds 4096 UTF-16 units")
    return text


class NotificationStore:
    def __init__(self, path: str | Path):
        raw = str(path).strip()
        if not raw or raw == ":memory:" or raw.startswith("file:"):
            raise ValueError("Notifications require a persistent SQLite path")
        self.path = Path(raw)
        if (not self.path.is_absolute() or self.path.is_symlink()
                or (self.path.exists() and not self.path.is_file())):
            raise ValueError("Notifications database path must be an absolute regular file")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    user_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL CHECK(category='release'),
                    body TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved')),
                    not_before INTEGER NOT NULL DEFAULT 0,
                    dispatch_enabled INTEGER NOT NULL DEFAULT 0 CHECK(dispatch_enabled IN (0,1)),
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    user_id INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK(state IN ('pending','inflight','sent','failed','unknown','skipped')),
                    not_before INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER,
                    outcome TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (campaign_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS delivery_pending
                    ON deliveries(campaign_id,state,not_before,user_id);
            """)
            # Additive upgrade for the database created by PR #77.
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(campaigns)")}
            if "dispatch_enabled" not in columns:
                self.db.execute(
                    "ALTER TABLE campaigns ADD COLUMN dispatch_enabled INTEGER NOT NULL DEFAULT 0")
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def observe_private(self, user_id: int) -> bool:
        """Enroll a first-time private bot contact, never undo /news_off.

        This does not import marketing consent, group membership, external
        contacts or Business messages. A new /forget_me is handled before
        invoking this method in NotificationHandler.observe_update.
        """
        uid = _positive(user_id)
        with self._tx():
            return bool(self.db.execute(
                "INSERT OR IGNORE INTO subscribers(user_id,enabled,updated_at) "
                "VALUES (?,1,?)", (uid, int(time.time()))).rowcount)

    def backfill_confirmed_private_contacts(self, legacy_db: str | Path) -> int:
        """One-time migration from bot-private marketing feature records.

        Only user IDs already persisted by confirmed private bot workflows
        are eligible; read source DB in read-only mode. Do not import the
        marketing opt-in flag, business connection IDs, group members,
        channel subscribers, message bodies or other personal data.
        """
        source = Path(legacy_db)
        if (not source.is_absolute() or source.is_symlink()
                or not source.is_file() or source == self.path):
            raise ValueError("Expected a separate existing private source DB")
        allowed = ("footer_events", "preferences", "orders", "pending_orders")
        original_count = self.db.execute(
            "SELECT COUNT(*) FROM subscribers").fetchone()[0]
        legacy = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            tables = {row[0] for row in legacy.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not (tables & set(allowed)):
                raise ValueError("No known private bot interaction tables")
            with self._tx():
                for table in allowed:
                    if table not in tables:
                        continue
                    cols = {row[1] for row in legacy.execute(
                        f"PRAGMA table_info({table})")}
                    if "user_id" not in cols:
                        raise ValueError("Unexpected legacy table schema")
                    for (uid,) in legacy.execute(
                            f"SELECT DISTINCT user_id FROM {table} "
                            "WHERE typeof(user_id)='integer' AND user_id>0"):
                        self.db.execute(
                            "INSERT OR IGNORE INTO subscribers(user_id,enabled,updated_at) "
                            "VALUES(?,1,?)", (uid, int(time.time())))
            return (self.db.execute(
                "SELECT COUNT(*) FROM subscribers").fetchone()[0]
                    - original_count)
        finally:
            legacy.close()

    def consent(self, user_id: int, enabled: bool) -> None:
        user_id = _positive(user_id)
        if type(enabled) is not bool:
            raise ValueError("Expected explicit boolean")
        with self._tx():
            self.db.execute(
                "INSERT INTO subscribers VALUES (?,?,?) ON CONFLICT(user_id) "
                "DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                (user_id, int(enabled), int(time.time())),
            )
            if not enabled:
                self.db.execute(
                    "UPDATE deliveries SET state='skipped',outcome='unsubscribed' "
                    "WHERE user_id=? AND state='pending'", (user_id,))

    def status(self, user_id: int) -> bool:
        row = self.db.execute(
            "SELECT enabled FROM subscribers WHERE user_id=?",
            (_positive(user_id),)).fetchone()
        return bool(row and row["enabled"])

    def forget(self, user_id: int) -> None:
        user_id = _positive(user_id)
        with self._tx():
            self.db.execute("DELETE FROM deliveries WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM subscribers WHERE user_id=?", (user_id,))

    def create(self, campaign_id: str, category: str, body: str) -> int:
        if not isinstance(campaign_id, str) or not ID_PATTERN.fullmatch(campaign_id):
            raise ValueError("Invalid campaign ID")
        if category != "release":
            raise ValueError("Only opted-in release announcements are supported")
        body = _validate_text(body)
        with self._tx():
            self.db.execute(
                "INSERT INTO campaigns(id,category,body,digest,created_at) "
                "VALUES(?,?,?,?,?)",
                (campaign_id, category, body, _digest(body), int(time.time())))
            self.db.execute(
                "INSERT INTO deliveries(campaign_id,user_id,updated_at) "
                "SELECT ?,user_id,? FROM subscribers WHERE enabled=1",
                (campaign_id, int(time.time())))
            return self.db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE campaign_id=?",
                (campaign_id,)).fetchone()[0]

    def campaign(self, campaign_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise LookupError("Campaign not found")
        return dict(row)

    def approve(self, campaign_id: str, phrase: str) -> bool:
        campaign = self.campaign(campaign_id)
        expected = f"APPROVE:{campaign_id}:{campaign['digest'][:12]}"
        if phrase != expected:
            raise PermissionError("Approval does not match campaign digest")
        with self._tx():
            return bool(self.db.execute(
                "UPDATE campaigns SET state='approved' "
                "WHERE id=? AND state='draft'", (campaign_id,)).rowcount)

    def subscribers_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM subscribers WHERE enabled=1").fetchone()[0]

    def recent_campaigns(self, limit: int = 6) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Invalid history size")
        return [dict(row) for row in self.db.execute(
            "SELECT id,state,digest,dispatch_enabled,created_at "
            "FROM campaigns ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,))]

    def unsettled(self, campaign_id: str) -> bool:
        counts = self.counts(campaign_id)
        return counts["unknown"] > 0 or counts["inflight"] > 0

    def pause_all(self) -> None:
        """Bot start: require a fresh owner confirmation after every restart."""
        with self._tx():
            self.db.execute("UPDATE campaigns SET dispatch_enabled=0 "
                            "WHERE dispatch_enabled=1")

    def pause(self, campaign_id: str) -> None:
        with self._tx():
            changed = self.db.execute(
                "UPDATE campaigns SET dispatch_enabled=0 WHERE id=?", (campaign_id,))
            if changed.rowcount != 1:
                raise LookupError("Campaign not found")

    def activate(self, campaign_id: str) -> None:
        with self._tx():
            row = self.db.execute(
                "SELECT state FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if row is None or row["state"] != "approved":
                raise PermissionError("Approve campaign before activation")
            if self.db.execute(
                "SELECT 1 FROM campaigns WHERE dispatch_enabled=1"
            ).fetchone():
                raise PermissionError("A campaign is already active")
            if self.db.execute(
                "SELECT 1 FROM deliveries WHERE campaign_id=? AND state IN ('unknown','inflight')",
                (campaign_id,)).fetchone():
                raise PermissionError("Reconcile ambiguous deliveries before proceeding")
            if self.db.execute(
                "SELECT 1 FROM deliveries WHERE campaign_id=? AND outcome='api_rejected'",
                (campaign_id,)).fetchone():
                raise PermissionError("Campaign payload was rejected by Telegram")
            if not self.db.execute(
                "SELECT 1 FROM deliveries WHERE campaign_id=? AND state='pending'",
                (campaign_id,)).fetchone():
                raise PermissionError("No pending opt-in recipients")
            self.db.execute(
                "UPDATE campaigns SET dispatch_enabled=1 WHERE id=?", (campaign_id,))

    def has_invalid_payload(self, campaign_id: str) -> bool:
        """An explicit Telegram 400 likely affects the whole immutable message."""
        return self.db.execute(
            "SELECT 1 FROM deliveries WHERE campaign_id=? AND outcome='api_rejected' "
            "LIMIT 1", (campaign_id,)).fetchone() is not None

    def has_due(self, campaign_id: str, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        return self.db.execute(
            "SELECT 1 FROM campaigns c JOIN deliveries d ON d.campaign_id=c.id "
            "WHERE c.id=? AND c.dispatch_enabled=1 AND c.state='approved' "
            "AND c.not_before<=? AND d.state='pending' AND d.not_before<=? "
            "LIMIT 1", (campaign_id, now, now)).fetchone() is not None

    def claim(self, campaign_id: str, now: int | None = None) -> int | None:
        now = int(time.time()) if now is None else now
        with self._tx():
            row = self.db.execute(
                "SELECT state,not_before FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if row is None or row["state"] != "approved":
                raise PermissionError("Campaign is not approved")
            if row["not_before"] > now:
                return None  # Whole campaign respects an outstanding 429 cooldown.
            self.db.execute(
                "UPDATE deliveries SET state='skipped',outcome='unsubscribed',updated_at=? "
                "WHERE campaign_id=? AND state='pending' AND NOT EXISTS "
                "(SELECT 1 FROM subscribers s WHERE s.user_id=deliveries.user_id AND s.enabled=1)",
                (now, campaign_id))
            row = self.db.execute(
                "SELECT d.user_id FROM deliveries d JOIN subscribers s ON s.user_id=d.user_id "
                "WHERE d.campaign_id=? AND d.state='pending' AND s.enabled=1 "
                "AND d.not_before<=? ORDER BY d.user_id LIMIT 1",
                (campaign_id, now)).fetchone()
            if row is None:
                return None
            user_id = row["user_id"]
            changed = self.db.execute(
                "UPDATE deliveries SET state='inflight',updated_at=? "
                "WHERE campaign_id=? AND user_id=? AND state='pending'",
                (now, campaign_id, user_id))
            if changed.rowcount != 1:
                raise RuntimeError("Atomic claim failed")
            return user_id

    def finish(self, campaign_id: str, user_id: int, state: str, *,
               message_id: int | None = None, outcome: str = "") -> None:
        if state not in ("sent", "failed", "unknown", "skipped"):
            raise ValueError("Invalid result")
        if message_id is not None and (type(message_id) is not int or message_id <= 0):
            raise ValueError("Invalid Telegram message ID")
        with self._tx():
            changed = self.db.execute(
                "UPDATE deliveries SET state=?,message_id=?,outcome=?,updated_at=? "
                "WHERE campaign_id=? AND user_id=? AND state='inflight'",
                (state, message_id, outcome[:40], int(time.time()),
                 campaign_id, _positive(user_id)))
            if changed.rowcount != 1:
                raise RuntimeError("Already finalized or never claimed")
            if outcome == "blocked":
                self.db.execute(
                    "UPDATE subscribers SET enabled=0,updated_at=? WHERE user_id=?",
                    (int(time.time()), user_id))

    def defer_429(self, campaign_id: str, user_id: int, retry_after: int) -> None:
        if type(retry_after) is not int or not 1 <= retry_after <= 3600:
            raise ValueError("Invalid retry_after")
        with self._tx():
            changed = self.db.execute(
                "UPDATE deliveries SET state='pending',not_before=?, "
                "outcome='rate_limited',updated_at=? "
                "WHERE campaign_id=? AND user_id=? AND state='inflight'",
                (int(time.time()) + retry_after + 1, int(time.time()),
                 campaign_id, _positive(user_id)))
            if changed.rowcount != 1:
                raise RuntimeError("Rate limit was not claimed")
            self.db.execute("UPDATE campaigns SET not_before=? WHERE id=?",
                            (int(time.time()) + retry_after + 1, campaign_id))

    def counts(self, campaign_id: str) -> dict:
        self.campaign(campaign_id)
        values = {row["state"]: row["n"] for row in self.db.execute(
            "SELECT state,COUNT(*) n FROM deliveries WHERE campaign_id=? GROUP BY state",
            (campaign_id,))}
        return {key: values.get(key, 0) for key in
                ("pending", "inflight", "sent", "failed", "unknown", "skipped")}


class NotificationHandler:
    def __init__(self, api, store: NotificationStore):
        self.api, self.store = api, store

    def observe_update(self, update: dict) -> bool:
        """Enroll only real, authenticated private message senders.

        Call at the *start* of routing, even when another feature handles the
        incoming message. Synthetic menu actions, business updates, group
        messages and bots must never increase the audience.
        """
        if update.get("_menu_action"):
            return False
        msg = update.get("message")
        if not isinstance(msg, dict):
            return False
        chat, actor = msg.get("chat") or {}, msg.get("from") or {}
        uid = actor.get("id")
        if (chat.get("type") != "private" or type(uid) is not int or uid <= 0
                or type(chat.get("id")) is not int or chat["id"] != uid
                or actor.get("is_bot") is True):
            return False
        body = msg.get("text")
        if isinstance(body, str) and body.strip():
            command = body.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if not any(key in msg for key in (
                    "forward_origin", "forward_from", "forward_date")):
                if command == "/forget_me":
                    self.store.forget(uid)
                    return False
                if command == "/news_off":
                    self.store.consent(uid, False)
                    return False
        return self.store.observe_private(uid)

    def handle_update(self, update: dict) -> bool:
        msg = update.get("message")
        if not isinstance(msg, dict) or any(k in msg for k in
                ("forward_origin", "forward_from", "forward_date")):
            return False
        chat, actor = msg.get("chat") or {}, msg.get("from") or {}
        user_id, body = actor.get("id"), msg.get("text")
        if (chat.get("type") != "private" or type(user_id) is not int or user_id <= 0
                or chat.get("id") != user_id or not isinstance(body, str)):
            return False
        command = body.split(maxsplit=1)[0].split("@", 1)[0].lower() if body.strip() else ""
        if command == "/forget_me":
            self.store.forget(user_id)
            return False  # Other privacy handlers must continue.
        if command == "/news_on":
            self.store.consent(user_id, True)
            reply = "✅ Новости KYNTRION включены. Отписаться: /news_off"
        elif command == "/news_off":
            self.store.consent(user_id, False)
            reply = "🔕 Новости KYNTRION отключены. Включить: /news_on"
        elif command == "/news_status":
            reply = ("✅ Новости KYNTRION включены для личных чатов по умолчанию. "
                     "Отписаться: /news_off"
                     if self.store.status(user_id) else
                     "🔕 Новости отключены. Включить: /news_on")
        else:
            return False
        self.api.send_action({"method": "sendMessage", "chat_id": user_id, "text": reply})
        return True


def send_batch(store: NotificationStore, api, campaign_id: str,
               *, limit: int = 25, pause=time.sleep) -> dict:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Batch limit must be 1..500")
    campaign = store.campaign(campaign_id)
    if campaign["state"] != "approved":
        raise PermissionError("Campaign is not approved")
    processed = 0
    while processed < limit:
        uid = store.claim(campaign_id)
        if uid is None:
            break
        processed += 1
        if not store.status(uid):
            store.finish(campaign_id, uid, "skipped", outcome="unsubscribed")
            continue
        try:
            result = api.send_action({
                "method": "sendMessage", "chat_id": uid, "text": campaign["body"],
                "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})
        except TelegramFloodWait as exc:
            store.defer_429(campaign_id, uid, exc.retry_after)
            break  # Definitive 429 only. Next approved batch after cooldown.
        except TelegramApiError as exc:
            reason = str(exc)
            if reason.startswith("API HTTP 403:"):
                store.finish(campaign_id, uid, "failed", outcome="blocked")
            elif reason.startswith("API HTTP 400:"):
                store.finish(campaign_id, uid, "failed", outcome="api_rejected")
            else:
                store.finish(campaign_id, uid, "unknown", outcome="telegram_uncertain")
        except Exception:
            store.finish(campaign_id, uid, "unknown", outcome="unexpected_error")
        else:
            if (isinstance(result, dict) and type(result.get("message_id")) is int
                    and result["message_id"] > 0):
                store.finish(campaign_id, uid, "sent", message_id=result["message_id"])
            else:
                store.finish(campaign_id, uid, "unknown", outcome="missing_receipt")
        if processed < limit:
            pause(MIN_SEND_INTERVAL)
    return store.counts(campaign_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="KYNTRION opt-in release announcements (no automatic sending)")
    parser.add_argument("--db", default=os.environ.get(
        "KYN_NOTIFICATIONS_DB", "/var/lib/kyntrion-text-bot/notifications.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Snapshot opted-in users; no sends")
    prepare.add_argument("id")
    prepare.add_argument("--category", choices=("release",), default="release")
    prepare.add_argument("--text-file", required=True)
    backfill = commands.add_parser(
        "backfill-private", help="Import known private contacts from existing bot DB, no sends")
    backfill.add_argument("--source-db", required=True)
    show = commands.add_parser("preview")
    show.add_argument("id")
    approve = commands.add_parser("approve")
    approve.add_argument("id")
    approve.add_argument("--confirm", required=True)
    send = commands.add_parser("send")
    send.add_argument("id")
    send.add_argument("--confirm", required=True)
    send.add_argument("--limit", type=int, default=25)
    stats = commands.add_parser("stats")
    stats.add_argument("id")
    args = parser.parse_args(argv)
    store = NotificationStore(args.db)
    try:
        if args.command == "prepare":
            body = Path(args.text_file).read_text(encoding="utf-8")
            if len(body.encode("utf-8")) > 32_000:
                raise ValueError("Text file is too large")
            count = store.create(args.id, args.category, body)
            print(f"Prepared {args.id}: {count} opted-in users (no sends)")
        elif args.command == "backfill-private":
            count = store.backfill_confirmed_private_contacts(args.source_db)
            print(f"Added {count} previously contacted private users; no messages sent")
        elif args.command == "preview":
            data = store.campaign(args.id)
            print(f"Campaign: {data['id']} / {data['state']} / {data['category']}")
            print(f"SHA256: {data['digest']}\nRecipients: {store.counts(args.id)}")
            print(f"\n{data['body']}\n")
            print(f"Approval phrase: APPROVE:{args.id}:{data['digest'][:12]}")
        elif args.command == "approve":
            print("Approved" if store.approve(args.id, args.confirm) else "Already approved")
        elif args.command == "stats":
            print(f"Campaign {args.id}: {store.counts(args.id)}")
        elif args.command == "send":
            data = store.campaign(args.id)
            if args.confirm != f"SEND:{args.id}:{data['digest'][:12]}":
                raise PermissionError("Send confirmation does not match immutable campaign")
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                raise ValueError("TELEGRAM_BOT_TOKEN not configured")
            api = TelegramApi(token, gateway_url=os.environ.get("TELEGRAM_GATEWAY_URL"))
            print(f"Batch finished: {send_batch(store, api, args.id, limit=args.limit)}")
        return 0
    except (LookupError, PermissionError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"Operation denied: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
