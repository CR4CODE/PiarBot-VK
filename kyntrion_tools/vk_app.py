"""Run KYNTRION in VK community messages with Bots Long Poll."""

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time

from kyntrion_tools.app import dispatch_update
from kyntrion_tools.bot import BotController
from kyntrion_tools.broadcast_notifications import NotificationHandler, NotificationStore
from kyntrion_tools.kyntrion_ai_client import AIClient
from kyntrion_tools.kyntrion_catalog import CatalogAddon
from kyntrion_tools.kyntrion_consult import ConsultationAddon
from kyntrion_tools.kyntrion_crm import CRMAddon
from kyntrion_tools.kyntrion_marketing import MarketingAddon, MarketingConfig
from kyntrion_tools.kyntrion_portfolio import PortfolioAddon
from kyntrion_tools.notification_panel import OwnerNotificationPanel
from kyntrion_tools.post_studio_scheduler import run_due
from kyntrion_tools.post_studio_store import PostStudioStore
from kyntrion_tools.telegram_api import TelegramApi
from kyntrion_tools.vk_api import VkApi, VkApiError
from kyntrion_tools.vk_events import normalize_events
from kyntrion_tools.voice_command_journal import VoiceCommandJournal
from kyntrion_tools.voice_text_commands import VoiceTextRouter


LOG = logging.getLogger("kyntrion.vk")


def positive_id(env, name):
    value = env.get(name, "").strip()
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ValueError(f"Задай положительный числовой {name}.")
    return int(value)


def make_api(env):
    group_id = positive_id(env, "VK_GROUP_ID")
    token = env.get("VK_GROUP_TOKEN", "").strip()
    if not token:
        raise ValueError("Задай VK_GROUP_TOKEN в конфигурации службы.")
    asr = None
    if env.get("VK_ASR_URL"):
        credential = env.get("VK_WORKER_SECRET", "")
        asr = TelegramApi(credential, voice_asr_url=env["VK_ASR_URL"]).transcribe_audio
    return VkApi(token, group_id, transcriber=asr)


def owner_wall_api(token, group_id, owner):
    if not token:
        return None
    api = VkApi(token, group_id)

    def call(method, **params):
        users = api.call("users.get")
        if not users or users[0].get("id") != owner:
            raise VkApiError("Ключ стены принадлежит другому владельцу")
        return api.call(method, **params)

    return call


@contextmanager
def features(api, group_id, state_dir, env, community_wall=None):
    root = Path(state_dir) / str(group_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner = positive_id(env, "VK_OWNER_ID") if env.get("VK_OWNER_ID") else None
    controller = BotController(api)
    with ExitStack() as stack:
        def manage(resource):
            return stack.enter_context(closing(resource))

        news = manage(NotificationStore(root / "notifications.sqlite3"))
        options = {"notifications": NotificationHandler(api, news)}
        marketing = None
        if owner:
            marketing = manage(MarketingAddon(MarketingConfig(
                root / "marketing.sqlite3", owner, platform_name="VK")))
            ai = (AIClient(env["VK_CONSULT_URL"], env.get("VK_WORKER_SECRET", ""))
                  if env.get("VK_CONSULT_URL") else None)
            from kyntrion_tools.vk_studio import VKPostStudio
            wall = owner_wall_api(env.get("VK_WALL_TOKEN"), group_id, owner) or community_wall
            studio = VKPostStudio(api, manage(PostStudioStore(root / "studio.sqlite3")),
                                  group_id, owner, wall_call=wall)
            options.update(portfolio=PortfolioAddon(marketing), catalog=CatalogAddon(marketing),
                           crm=CRMAddon(marketing), consultation=ConsultationAddon(marketing, ai),
                           post_studio=studio, notifications_panel=OwnerNotificationPanel(
                               api, news, owner, delivery_enabled=(
                                   env.get("VK_NEWS_DELIVERY", "").lower() == "true")))
        # Group commands use the actual event sender. The shared router's owner
        # parameter is only relevant to the Telegram Business transport.
        journal = manage(VoiceCommandJournal(root / "commands.sqlite3"))
        options["voice_router"] = VoiceTextRouter(api, controller, owner or group_id, journal)
        yield controller, marketing, options


class PollState:
    """Store a batch cursor and claim individual events before observable actions."""

    def __init__(self, db):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY, ts TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, seen_at INTEGER)")

    def cursor(self):
        row = self.db.execute("SELECT ts FROM cursor WHERE id=1").fetchone()
        return row[0] if row else None

    def advance(self, ts):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cursor VALUES(1, ?)", (ts,))
            self.db.execute("DELETE FROM seen WHERE seen_at < ?", (int(time.time()) - 86400 * 7,))

    def claim(self, event):
        obj = event.get("object")
        if not isinstance(obj, dict):
            return False
        kind = event.get("type")
        if kind == "message_event":
            identity = obj.get("event_id")
        elif kind == "message_new":
            msg = obj.get("message")
            if not isinstance(msg, dict):
                return False
            mid = msg.get("conversation_message_id") or msg.get("id")
            identity = [msg.get("peer_id"), mid] if mid else event.get("event_id")
        else:
            return False
        if not identity:
            return False
        key = sha256(json.dumps([kind, identity]).encode()).hexdigest()
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO seen VALUES(?, ?)",
                                       (key, int(time.time())))
        return inserted.rowcount == 1


def consume(api, group_id, state, event, controller, marketing, options):
    if not isinstance(event, dict) or event.get("group_id") != group_id:
        return
    if not state.claim(event):
        return
    updates = normalize_events(event, group_id, api)
    for update in updates:
        msg = update.get("message", {})
        chat = msg.get("chat", {})
        peer = chat.get("id")
        forwarded = "forward_origin" in msg or "forward_date" in msg or "forward_from" in msg
        if forwarded and chat.get("type") != "private":
            continue
        if not forwarded and chat.get("type") == "private" and peer == msg.get("from", {}).get("id"):
            words = (msg.get("text") or "").split(maxsplit=1)
            cmd = words[0].lower() if words else ""
            if cmd == "/forget_me":
                controller.sessions.pop((peer, peer), None)
                controller.archive_sessions.active.pop(peer, None)
                if not marketing:
                    options["notifications"].store.forget(peer)
                    api.send_message(peer, "Настройки уведомлений и текущий сбор удалены.")
                    continue
            elif cmd == "/consult" and not marketing:
                api.send_message(peer, "Для консультаций владельцу сообщества нужно настроить VK_OWNER_ID.")
                continue
        dispatch_update(api, controller, marketing, update, **options)


def run(api, env):
    group_id = positive_id(env, "VK_GROUP_ID")
    state_dir = env.get("VK_STATE_DIR") or "vk-state"
    with features(api, group_id, state_dir, env) as (controller, marketing, options):
        db_path = Path(state_dir) / str(group_id) / "poll.sqlite3"
        with closing(sqlite3.connect(db_path)) as db:
            state = PollState(db)
            api.connect(resume_ts=state.cursor())
            state.advance(api.ts)
            failures = 0
            while True:
                panel = options.get("notifications_panel")
                try:
                    events = api.poll(wait=1 if panel and panel.wants_short_poll() else 5)
                    for event in events:
                        try:
                            consume(api, group_id, state, event, controller, marketing, options)
                        except (VkApiError, ValueError, TypeError, KeyError) as exc:
                            LOG.warning("Событие ВК пропущено (%s)", type(exc).__name__)
                    state.advance(api.ts)
                    failures = 0
                except VkApiError as exc:
                    failures += 1
                    LOG.warning("Long Poll недоступен (%s)", type(exc).__name__)
                    time.sleep(min(2 ** min(failures, 5), 30))
                options["voice_router"].flush_completed()
                studio = options.get("post_studio")
                if studio:
                    run_due(studio, studio.store, datetime.now(timezone.utc).isoformat(timespec="seconds"))
                if panel:
                    panel.tick()


def main(argv=None):
    parser = argparse.ArgumentParser(description="KYNTRION · VK Bots Long Poll")
    parser.add_argument("--check", action="store_true",
                        help="Проверить Long Poll и вывести JSON-квитанцию для NexShell")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        api = make_api(os.environ)
        if args.check:
            api.connect()
            print(json.dumps({"service": "kyntrion-vk-bot", "group_id": positive_id(os.environ, "VK_GROUP_ID"),
                              "long_poll": "connected"}, ensure_ascii=True))
        else:
            run(api, os.environ)
    except (ValueError, VkApiError, OSError) as exc:
        print(f"Ошибка запуска ВК: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
