"""KYNTRION section of PiarBot's existing user Long Poll dispatcher."""

from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from kyntrion_tools.command_catalog import parse_dot
from kyntrion_tools.post_studio_scheduler import run_due
from kyntrion_tools.vk_app import PollState, consume, features


class PiarExtension:
    def __init__(self, api, group_id, state_dir, env):
        self.api = api
        self.group_id = group_id
        api.text_buttons = True
        self.resources = ExitStack()
        try:
            self.controller, self.marketing, self.options = self.resources.enter_context(
                features(api, group_id, state_dir, env))
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

    def tick(self):
        if time.monotonic() < self.next_tick:
            return
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
        enter = private and text.casefold() in ("/kyntrion", "🧰 kyntrion")
        active = private and self.db.execute(
            "SELECT 1 FROM tools_dialog WHERE peer=?", (peer,)).fetchone()
        if active and (text in ("/piar", "⬅️ В меню PiarBot", "⬅️ В меню")
                       or text.split(maxsplit=1)[:1] == ["/start"]):
            self._leave(peer)
            if text.startswith("/start"):
                return False
            back_to_piar(peer)
            return True
        group_command = peer > 2_000_000_000 and parse_dot(text) is not None
        if not (enter or active or group_command):
            return False

        # User Long Poll only includes attachment flags and abbreviated text.
        # VK's message object supplies reply, forwards, signed media links and CMID.
        response = self.api.call("messages.getById", message_ids=event.message_id)
        items = response.get("items", []) if isinstance(response, dict) else []
        if len(items) != 1:
            return True
        msg = items[0]
        if (msg.get("out") or msg.get("peer_id") != peer or msg.get("from_id") != user
                or msg.get("id") != event.message_id or not msg.get("conversation_message_id")):
            return True
        cmid = msg["conversation_message_id"]
        if enter:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO tools_dialog VALUES(?)", (peer,))
            msg = {**msg, "text": "/start"}
        envelope = {"type": "message_new", "group_id": self.group_id, "object": {"message": msg}}
        payload = msg.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        if private and isinstance(payload, dict) and payload.get("kyntrion") == 1 and "origin" in payload:
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
