import json
import unittest

import test_kyntrion


class StudioInputTests(unittest.TestCase):
    def setUp(self):
        self.host = test_kyntrion.PiarToolsTests()
        self.addCleanup(self.host.doCleanups)
        self.host.setUp()
        self.host.send("/kyntrion")
        self.host.send("/post")
        self.studio = self.host.ext.options["post_studio"]

    def click(self, prefix):
        message = next(params for method, params in reversed(self.host.calls)
                       if method == "messages.send" and "keyboard" in params)
        for row in json.loads(message["keyboard"])["buttons"]:
            for button in row:
                action = button["action"]
                payload = json.loads(action["payload"])
                if payload.get("cmd", "").startswith(prefix):
                    self.host.send(action["label"], payload=action["payload"])
                    return
        self.fail(f"No button for {prefix}")

    def test_captioned_album_reaches_preview_and_confirmed_wall_in_order(self):
        self.click("post:new")
        self.click("post:media:")
        media = [{"type": kind, kind: {"owner_id": 5, "id": number}}
                 for kind, number in (("photo", 4), ("photo", 9), ("video", 2))]
        self.host.send("На выставке", attachments=media)
        content = self.studio.store.list_drafts(5)[0]["content"]
        self.assertEqual(content.get("caption"), "На выставке")
        self.assertEqual([item["file_id"] for item in content["media"]],
                         ["photo5_4", "photo5_9", "video5_2"])
        self.click("post:publish:")
        self.host.wall.assert_not_called()
        self.assertTrue(any(params.get("message") == "На выставке"
                            and params.get("attachment") == "photo5_4,photo5_9,video5_2"
                            for method, params in self.host.calls if method == "messages.send"))
        self.click("post:confirm:")
        self.host.wall.assert_called_once_with(
            "wall.post", owner_id=-7, from_group=1,
            message="На выставке", attachments="photo5_4,photo5_9,video5_2")

    def test_attachment_on_explicit_studio_command_keeps_command_routing(self):
        self.click("post:new")
        self.host.send("/post text История", attachments=[
            {"type": "photo", "photo": {"owner_id": 5, "id": 4}}])
        self.assertEqual(self.studio.store.list_drafts(5)[0]["content"], {"text": "История"})

    def test_wall_edit_acknowledgment_for_both_vk_versions(self):
        edit = {"method": "wall.edit", "chat_id": -7, "message_id": 42,
                "message": "Правка", "attachments": ""}
        for response in (1, {"post_id": 42}):
            with self.subTest(response=response):
                self.host.wall.return_value = response
                self.assertEqual(self.studio.send_public_action(edit), {"message_id": 42})
        for response in (0, True, {"post_id": 43}, {}):
            with self.subTest(invalid=response):
                self.host.wall.return_value = response
                with self.assertRaises(ValueError):
                    self.studio.send_public_action(edit)
        self.host.wall.return_value = 1
        with self.assertRaises(ValueError):
            self.studio.send_public_action({**edit, "method": "wall.post"})


if __name__ == "__main__":
    unittest.main()
