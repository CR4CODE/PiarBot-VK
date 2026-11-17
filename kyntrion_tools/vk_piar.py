"""KYNTRION section of PiarBot's existing user Long Poll dispatcher."""

from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time

from kyntrion_tools.command_catalog import parse_dot
from kyntrion_tools.post_studio_scheduler import run_due
from kyntrion_tools.vk_api import VkApiError
from kyntrion_tools.vk_app import PollState, consume, features
from kyntrion_tools.vk_events import group_command_text


def _button_payload(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _partner_source(ref):
    if isinstance(ref, str):
        match = re.fullmatch(r"kyn_([A-Za-z0-9_-]{1,64})", ref)
        if match:
            return match.group(1)
    return ""


class PiarExtension:
    def __init__(self, api, group_id, state_dir, env, owner_lookup=None, community_wall=None):
        self.api = api
        self.group_id = group_id
        self.owner_lookup = owner_lookup
        self.community_wall = community_wall
        self.state_dir = state_dir
        self.env = dict(env)
        if owner_lookup is not None:
            self.env["VK_OWNER_ID"] = self._live_owner()
        api.text_buttons = True
        self.resources = ExitStack()
        try:
            self.feature_resources = self.resources.enter_context(ExitStack())
            self.controller, self.marketing, self.options = self.feature_resources.enter_context(
                features(api, group_id, state_dir, self.env, community_wall))
            path = Path(state_dir) / str(group_id) / "poll.sqlite3"
            self.db = self.resources.enter_context(closing(sqlite3.connect(path)))
            self.state = PollState(self.db)
            self.db.execute("CREATE TABLE IF NOT EXISTS tools_dialog (peer INTEGER PRIMARY KEY)")
        except BaseException:
            self.close()
            raise
        self.next_tick = 0.

    def close(self):
        self.resources.close()

    def _live_owner(self):
        value = str(self.owner_lookup())
        return value if value.isascii() and value.isdecimal() and int(value) > 0 else ""

    def _sync_owner(self):
        if self.owner_lookup is None:
            return
        owner = self._live_owner()
        if owner == self.env["VK_OWNER_ID"]:
            return
        # The same polling thread handles PiarBot /setowner and tools events.
        # Rebind CRM, notifications and scheduled wall jobs before the next action.
        env = {**self.env, "VK_OWNER_ID": owner}
        env.pop("VK_WALL_TOKEN", None)
        with ExitStack() as pending:
            controller, marketing, options = pending.enter_context(
                features(self.api, self.group_id, self.state_dir, env, self.community_wall))
            controller.sessions = self.controller.sessions
            controller.archive_sessions = self.controller.archive_sessions
            self.feature_resources.close()
            self.feature_resources.enter_context(pending.pop_all())
            self.controller, self.marketing, self.options, self.env = controller, marketing, options, env

    def tick(self):
        if time.monotonic() < self.next_tick:
            return
        self._sync_owner()
        self.next_tick = time.monotonic() + 5
        self.options["voice_router"].flush_completed()
        studio = self.options.get("post_studio")
        if studio:
            run_due(studio, studio.store, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        panel = self.options.get("notifications_panel")
        if panel:
            panel.tick()
        with self.db:
            self.db.execute("DELETE FROM seen WHERE seen_at < ?", (int(time.time()) - 7 * 86400,))

    def _leave(self, peer):
        with self.db:
            self.db.execute("DELETE FROM tools_dialog WHERE peer=?", (peer,))
        self.controller.sessions.pop((peer, peer), None)
        self.controller.archive_sessions.active.pop(peer, None)

    def handle(self, event, back_to_piar):
        """Return False to continue through PiarBot's original dispatch."""
        peer, user = event.peer_id, event.user_id
        if type(user) is not int or user <= 0 or getattr(event, "from_me", False):
            return False
        text = (event.text or "").strip()
        private = peer == user and event.to_me
        words = text.split(maxsplit=1)
        command = words[0].casefold() if words else ""
        enter = private and (command == "/kyntrion" or text.casefold() == "🧰 kyntrion")
        ref_hint = getattr(event, "ref", None)
        client_start = private and (bool(_partner_source(ref_hint))
                                    or text.casefold() in ("начать", "start")
                                    or _button_payload(getattr(event, "payload", None)).get("command") == "start")
        active = private and self.db.execute(
            "SELECT 1 FROM tools_dialog WHERE peer=?", (peer,)).fetchone()
        if private and command == "/start" and (len(words) > 1 or not client_start):
            if active:
                self._leave(peer)
            return False
        if active and text in ("/piar", "⬅️ В меню PiarBot", "⬅️ В меню"):
            self._leave(peer)
            back_to_piar(peer)
            return True
        group_command = (peer > 2_000_000_000
                         and parse_dot(group_command_text(text, self.group_id)) is not None)
        managed = bool(enter or active or group_command)
        if not (managed or client_start):
            return False
        self._sync_owner()

        # User Long Poll only includes attachment flags and abbreviated text.
        # VK's message object supplies reply, forwards, signed media links and CMID.
        try:
            response = self.api.call("messages.getById", message_ids=event.message_id)
        except VkApiError:
            if managed:
                raise
            return False
        items = response.get("items", []) if isinstance(response, dict) else []
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            return managed
        msg = items[0]
        if (msg.get("out") or msg.get("peer_id") != peer or msg.get("from_id") != user
                or msg.get("id") != event.message_id or not msg.get("conversation_message_id")):
            return managed
        cmid = msg["conversation_message_id"]
        payload = _button_payload(msg.get("payload"))
        source = _partner_source(msg.get("ref", ref_hint)) if private else ""
        if enter or source:
            if command == "/kyntrion" and len(words) > 1:
                source = words[1]
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO tools_dialog VALUES(?)", (peer,))
            # The shared dispatcher validates the source and counts this CMID once.
            # Remove VK's start payload so normalization keeps the decoded source.
            payload = {}
            msg = {**msg, "text": "/start " + source if source else "/start", "payload": payload}
        elif client_start and (not active or payload.get("command") == "start"):
            if active:
                self._leave(peer)
            return False
        envelope = {"type": "message_new", "group_id": self.group_id, "object": {"message": msg}}
        if private and payload.get("kyntrion") == 1 and "origin" in payload:
            origin = self.api.button_origin(peer, payload["origin"])
            if not origin:
                if self.state.claim(envelope):
                    self.api.send_message(peer, "Эта клавиатура устарела. Открой /kyntrion.")
                return True
            envelope = {"type": "message_event", "group_id": self.group_id, "object": {
                "event_id": f"piar-{peer}-{cmid}", "user_id": user, "peer_id": peer,
                "conversation_message_id": origin, "payload": payload}}
        consume(self.api, self.group_id, self.state, envelope,
                self.controller, self.marketing, self.options)
        if private and text == "/forget_me":
            self._leave(peer)
        return True
