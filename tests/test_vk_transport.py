"""Wire-format tests for VK Bot API calls. No credentials or network required."""
from io import BytesIO
import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qsl, urlsplit

from kyntrion_tools.vk_api import VkApi, VkApiError


def client(*responses):
    replies = (value if isinstance(value, bytes) else json.dumps(value).encode()
               for value in responses)
    opener = Mock(side_effect=lambda req, timeout: BytesIO(next(replies)))
    return VkApi("offline-example", 7, opener=opener), opener


def form(opener, index):
    request = opener.call_args_list[index].args[0]
    return dict(parse_qsl(request.data.decode(), keep_blank_values=True))


class VKTransportTests(unittest.TestCase):
    def test_reply_and_edit_use_conversation_id(self):
        api, opener = client({"response": [{"peer_id": 8, "message_id": 999,
                                           "conversation_message_id": 11}]}, {"response": 1})
        markup = {"inline_keyboard": [[{"text": "Услуги", "callback_data": "ui:services"}]]}
        sent = api.send_message(8, "Выберите", 6, reply_markup=markup)
        api.send_action({"method": "editMessageText", "chat_id": 8,
                         "message_id": sent["message_id"], "text": "Ответ"})
        send = form(opener, 0)
        self.assertEqual((send["peer_ids"], send["v"]), ("8", "5.199"))
        self.assertGreater(int(send["random_id"]), 0)
        self.assertEqual(json.loads(send["forward"]), {"is_reply": True, "peer_id": 8,
                                                       "conversation_message_ids": [6]})
        action = json.loads(send["keyboard"])["buttons"][0][0]["action"]
        self.assertEqual((action["type"], json.loads(action["payload"])["cmd"]),
                         ("callback", "ui:services"))
        self.assertEqual(form(opener, 1)["cmid"], "11")
        self.assertNotIn("offline-example", opener.call_args_list[0].args[0].full_url)

    def test_error_in_recipient_or_api_response_is_not_success(self):
        bad = {"peer_id": 8, "conversation_message_id": 0,
               "error": {"code": 901, "description": "private reason"}}
        api, opener = client({"response": [bad]},
                             {"error": {"error_code": 6, "error_msg": "offline-example"}})
        with self.assertRaises(VkApiError):
            api.send_message(8, "private text")
        with self.assertRaisesRegex(VkApiError, "429") as error:
            api.call("messages.getById", message_ids=1)
        self.assertNotIn("offline-example", str(error.exception))
        self.assertEqual(opener.call_count, 2)

    def test_deletion_requires_positive_ack_for_requested_peer_and_cmid(self):
        success = {"peer_id": 8, "message_id": 999, "conversation_message_id": 11, "response": 1}
        for ack, expected in ((success, True), ({**success, "response": 0}, False),
                              ({**success, "peer_id": 9}, False),
                              ({**success, "conversation_message_id": 99}, False)):
            with self.subTest(ack=ack):
                api, opener = client({"response": [ack]})
                self.assertEqual(api.delete_message(8, 11), expected)
                self.assertEqual(form(opener, 0)["cmids"], "11")

    def test_document_upload_transmits_original_bytes_and_filename(self):
        name = "пример.txt"
        data = "Один\nДва".encode()
        api, opener = client({"response": {"upload_url": "https://pu.vk.com/upload"}},
                             {"file": "upload-handle"},
                             {"response": {"doc": {"owner_id": -7, "id": 23, "access_key": "ab"}}},
                             {"response": [{"peer_id": 8, "conversation_message_id": 18}]})
        api.send_document(8, name, data, "Текст")
        upload = opener.call_args_list[1].args[0]
        boundary = upload.get_header("Content-type").split("boundary=")[1].encode()
        self.assertIn(('name="file"; filename="' + name + '"').encode(), upload.data)
        self.assertIn(b"\r\n\r\n" + data + b"\r\n--" + boundary + b"--", upload.data)
        self.assertNotIn(b"offline-example", upload.data)
        self.assertEqual(form(opener, 2)["title"], name)
        self.assertEqual(form(opener, 3)["attachment"], "doc-7_23_ab")

    def test_download_enforces_limit_independent_of_metadata(self):
        api, opener = client(b"12345", b"1234")
        api.register_media("doc7_2", "https://cdn.userapi.com/d/23", size=1)
        path = api.get_file("doc7_2")["file_path"]
        with self.assertRaises(VkApiError):
            api.download_file(path, max_bytes=4)
        self.assertEqual(api.download_file(path, max_bytes=4), b"1234")
        for invalid in ("https://vk.com.example.org/x", "http://vk.com/x", "https://user@vk.com/x"):
            with self.assertRaises(VkApiError):
                api.download_file(invalid)
        self.assertEqual(opener.call_count, 2)

    def test_large_keyboard_all_commands_reachable_on_original_message(self):
        api, opener = client({"response": [{"peer_id": 8, "conversation_message_id": 14}]},
                             {"response": 1}, {"response": 1})
        markup = {"inline_keyboard": [
            [{"text": str(i), "callback_data": "cmd-" + str(i)}] for i in range(9)
        ]}
        api.send_message(8, "Меню", reply_markup=markup)
        first = json.loads(form(opener, 0)["keyboard"])
        control = json.loads(first["buttons"][-1][0]["action"]["payload"])
        api.show_keyboard_page("event-1", 8, 8, 14, control["kyn_page"])
        edit = form(opener, 2)
        self.assertEqual((form(opener, 1)["event_id"], edit["cmid"]), ("event-1", "14"))
        actions = set()
        for page in (first, json.loads(edit["keyboard"])):
            self.assertIs(page["one_time"], False)
            self.assertLessEqual(len(page["buttons"]), 6)
            self.assertLessEqual(sum(map(len, page["buttons"])), 10)
            for row in page["buttons"]:
                for button in row:
                    cmd = json.loads(button["action"]["payload"]).get("cmd")
                    if cmd:
                        actions.add(cmd)
        self.assertEqual(actions, {"cmd-" + str(i) for i in range(9)})

    def test_long_poll_recovers_cursor_and_key(self):
        def endpoint(key, ts):
            return {"response": {"server": "https://lp.vk.com/wh7", "key": key, "ts": ts}}
        api, opener = client(endpoint("first", "11"), {"failed": 2}, endpoint("second", "20"),
                             {"failed": 1, "ts": "30"}, {"failed": 3}, endpoint("third", "50"),
                             {"ts": "51", "updates": [{"type": "message_new"}]})
        api.connect(resume_ts="8")
        for expected in ("8", "30", "50"):
            self.assertEqual(api.poll(wait=1), [])
            self.assertEqual(api.ts, expected)
        self.assertEqual(api.poll(wait=1), [{"type": "message_new"}])
        self.assertEqual(api.ts, "51")
        url = opener.call_args_list[3].args[0].full_url
        self.assertEqual(dict(parse_qsl(urlsplit(url).query)), {
            "act": "a_check", "key": "second", "ts": "8", "wait": "1"
        })


if __name__ == "__main__":
    unittest.main()
