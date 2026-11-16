import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import zipfile

from kyntrion_tools.vk_api import VkApi
from kyntrion_tools.vk_piar import PiarExtension


class PiarToolsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.api = VkApi("offline", 7)
        self.calls = []
        self.docs = []
        self.mid = 0
        self.messages = {}
        self.api.call = self.call
        self.api.send_document = lambda peer, name, data, *a, **kw: self.docs.append((peer, name, data))
        self.back = Mock()
        self.ext = PiarExtension(self.api, 7, Path(tmp.name), {"VK_OWNER_ID": "5"})
        self.addCleanup(self.ext.close)

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "messages.getById":
            return {"items": [self.messages[params["message_ids"]]]}
        if method == "messages.send":
            return [{"peer_id": params["peer_ids"], "conversation_message_id": 500 + len(self.calls)}]
        if method == "messages.edit":
            return 1
        self.fail(method)

    def send(self, text, uid=5, **extras):
        self.mid += 1
        self.messages[self.mid] = {"id": self.mid, "conversation_message_id": self.mid + 100,
                                   "from_id": uid, "peer_id": uid, "text": text, **extras}
        event = SimpleNamespace(peer_id=uid, user_id=uid, to_me=True, from_me=False,
                                text=text, message_id=self.mid)
        self.last = event
        return self.ext.handle(event, self.back)

    def test_menu_entry_collect_duplicate_back_and_legacy_commands(self):
        self.assertFalse(self.send("/start ref"))
        self.assertFalse(self.send("👤 Профиль"))
        self.assertFalse(self.calls)
        self.assertTrue(self.send("🧰 KYNTRION"))
        self.assertTrue(self.send("/collect"))
        self.send("первая")
        self.ext.handle(self.last, self.back)
        self.send("вторая")
        self.send("/done")
        self.assertEqual(self.docs[0][2].decode(), "первая\n\nвторая")
        self.send("/piar")
        self.back.assert_called_once_with(5)
        self.assertFalse(self.send("👤 Профиль"))

    def test_help_keyboard_click_carries_original_message_and_user_scope(self):
        self.send("/kyntrion")
        self.send("/help")
        sent = self.calls[-1][1]
        keyboard = json.loads(sent["keyboard"])
        button = keyboard["buttons"][0][0]["action"]
        self.assertEqual(button["type"], "text")
        expected_id = 500 + len(self.calls)
        self.send(button["label"], payload=button["payload"])
        method, edit = self.calls[-1]
        self.assertEqual((method, edit["cmid"]), ("messages.edit", expected_id))
        self.assertFalse(self.send(button["label"], uid=6, payload=button["payload"]))

    def test_original_archive_bytes_and_voice_transcript(self):
        self.send("/kyntrion")
        self.api.download_file = lambda url, **kwargs: b"example"
        attachment = {"type": "doc", "doc": {"owner_id": 5, "id": 3, "size": 7,
                      "title": "пример.txt", "url": "https://cdn.userapi.com/data"}}
        self.send(".zip", reply_message={"peer_id": 5, "from_id": 8, "attachments": [attachment]})
        with zipfile.ZipFile(io.BytesIO(self.docs[-1][2])) as archive:
            self.assertEqual(archive.read("пример.txt"), b"example")
        self.send("", attachments=[{"type": "audio_message", "audio_message": {
            "owner_id": 5, "id": 7, "duration": 2, "transcript": "Готово",
            "link_ogg": "https://cdn.userapi.com/a.ogg"}}])
        self.assertTrue(any(b"\xd0\x93\xd0\xbe" in contents for _, _, contents in self.docs))

    def test_wall_requires_confirmation_and_owner(self):
        studio = self.ext.options["post_studio"]
        studio.wall_call = Mock(return_value={"post_id": 42})
        draft = studio.store.create_draft(5, -7)
        studio.store.patch_draft(5, draft, {"text": "Новая запись"}, 1)
        token = studio.request_publish(5, draft)
        studio.wall_call.assert_not_called()
        self.assertNotEqual(studio.confirm_publish(88, token), "published")
        self.assertEqual(studio.confirm_publish(5, token), "published")
        studio.confirm_publish(5, token)
        studio.wall_call.assert_called_once_with("wall.post", owner_id=-7, from_group=1,
                                                message="Новая запись", attachments="")
