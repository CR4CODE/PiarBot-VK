import unittest
from unittest.mock import Mock

from kyntrion_tools.app import dispatch_update
from kyntrion_tools.bot import BotController
from kyntrion_tools.command_catalog import help_text


class VKHelpTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock(platform="vk")
        self.controller = BotController(self.api)

    def message(self, text):
        dispatch_update(self.api, self.controller, None, {"message": {
            "chat": {"id": 13, "type": "private"}, "from": {"id": 13},
            "message_id": 5, "text": text,
        }})

    def test_automation_buttons_show_vk_instructions(self):
        self.message("/automation")
        entry = self.api.send_message.call_args
        self.assertIn("права удаления", entry.args[1])
        buttons = [button for row in entry.kwargs["reply_markup"]["inline_keyboard"] for button in row]
        expected = {"text": "личный диалог с сообществом", "txt": ".zip",
                    "collect": "/done", "quote": "ID автора",
                    "type": "своём сообщении", "love": "своём сообщении"}
        seen = set()
        for button in buttons:
            data = button["callback_data"]
            dispatch_update(self.api, self.controller, None, {"callback_query": {
                "id": "test", "from": {"id": 13}, "data": data, "message": {
                    "chat": {"id": 13, "type": "private"}, "message_id": 14},
            }})
            action = self.api.send_action.call_args.args[0]
            self.assertEqual((action["method"], action["message_id"]), ("editMessageText", 14))
            for unrelated in ("@KyntrionBot", "Telegram", "пока не реализован", "tg://"):
                self.assertNotIn(unrelated, action["text"])
            topic = data.removeprefix("help:")
            if topic in expected:
                self.assertIn(expected[topic], action["text"])
                seen.add(topic)
        self.assertEqual(seen, set(expected))

    def test_typed_help_and_incomplete_commands_use_the_same_guide(self):
        for command, topic in ((".хелп .текст", "text"), (".текст", "text"),
                               (".тхт", "txt"), (".help /myid", "myid")):
            with self.subTest(command=command):
                self.message(command)
                self.assertEqual(self.api.send_message.call_args.args[1], help_text(topic, api=self.api))
        self.assertIn("@KyntrionBot", help_text("text"))
