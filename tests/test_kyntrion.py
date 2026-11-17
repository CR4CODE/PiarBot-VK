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

    def test_removed_studio_entry_points_preserve_collect_and_show_manual_guidance(self):
        self.send("/kyntrion")
        for method, params in self.calls:
            if method == "messages.send" and "keyboard" in params:
                keyboard = json.loads(params["keyboard"])
                for row in keyboard["buttons"]:
                    for button in row:
                        action = button["action"]
                        self.assertNotIn("Студия", action.get("label", ""))
                        self.assertNotIn("post:", action.get("payload", ""))
        self.assertNotIn("post_studio", self.ext.options)
        self.send("/collect")
        self.send("мой текст")
        old_payload = json.dumps({"kyntrion": 1, "cmd": "post:confirm:old", "origin": "expired"})
        for text, extra in (("/post", {}), ("/post text Пост", {}),
                            ("🎨 Студия постов", {}), ("Опубликовать", {"payload": old_payload})):
            with self.subTest(text=text):
                self.assertTrue(self.send(text, **extra))
                self.assertEqual(self.calls[-1][0], "messages.send")
                self.assertIn("вручную", self.calls[-1][1]["message"])
        self.ext.tick()
        self.send("/done")
        self.assertEqual([data.decode() for _, _, data in self.docs], ["мой текст"])
        self.assertFalse((Path(self.ext.state_dir) / "7" / "studio.sqlite3").exists())

    def test_owner_transfer_revokes_old_panel_and_preserves_collect(self):
        self.ext.owner_lookup = lambda: "5"
        self.send("/kyntrion")
        self.send("/collect")
        self.send("до передачи")
        self.ext.owner_lookup = lambda: "6"
        self.send("/done")
        self.assertEqual(self.docs[-1][2].decode(), "до передачи")
        self.assertEqual(self.ext.options["notifications_panel"].owner_id, 6)
        self.assertEqual(self.ext.options["crm"].owner, 6)

    def test_addressed_group_command_returns_selected_text_to_its_sender(self):
        peer = 2_000_000_001
        event = SimpleNamespace(peer_id=peer, user_id=5, from_me=False, to_me=False,
                                message_id=17, text="[club99|Другой бот], .txt")
        self.assertFalse(self.ext.handle(event, self.back))
        event.text = "[club7|Наш бот], .txt"
        self.messages[17] = {"id": 17, "conversation_message_id": 117,
                             "peer_id": peer, "from_id": 5, "text": event.text,
                             "reply_message": {"peer_id": peer, "from_id": 8, "text": "текст"}}
        self.api.delete_message = Mock(return_value=True)
        self.assertTrue(self.ext.handle(event, self.back))
        self.api.delete_message.assert_called_once_with(peer, 117)
        self.assertEqual((self.docs[0][0], self.docs[0][2].decode()), (5, "текст"))

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

    def test_zip_reply_keeps_every_document_in_selected_vk_message(self):
        self.send("/kyntrion")
        self.api.download_file = lambda url, **kwargs: url.rsplit("/", 1)[-1].encode()
        attachments = [
            {"type": "doc", "doc": {"owner_id": 5, "id": i, "title": "текст.txt",
                                   "url": f"https://cdn.userapi.com/{i}"}}
            for i in (11, 12)
        ]
        self.send(".zip", reply_message={"peer_id": 5, "from_id": 8, "attachments": attachments})
        self.assertEqual(len(self.docs), 1)
        with zipfile.ZipFile(io.BytesIO(self.docs[0][2])) as archive:
            self.assertEqual(archive.read("текст.txt"), b"11")
            self.assertEqual(archive.read("текст (2).txt"), b"12")
