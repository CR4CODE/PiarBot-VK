import tempfile
import unittest
from unittest.mock import Mock

from vk_api.longpoll import Event

from kyntrion_tools.vk_api import VkApi, VkApiError
from kyntrion_tools.vk_piar import PiarExtension


class ReferralTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.api = VkApi("offline", 7)
        self.api.send_message = Mock()
        self.api.call = Mock()
        self.ext = PiarExtension(self.api, 7, root.name, {"VK_OWNER_ID": "11"})
        self.addCleanup(self.ext.close)
        self.back = Mock()
        self.mid = 0

    def send(self, text, extra=None, **fields):
        self.mid += 1
        self.event = Event([4, self.mid, 0, 42, 1, text, extra or {}, {}, 0])
        self.api.call.return_value = {"items": [{
            "peer_id": 42, "from_id": 42, "conversation_message_id": self.mid + 70,
            "id": self.mid, "text": text, **fields,
        }]}
        return self.ext.handle(self.event, self.back)

    def counts(self):
        return dict(self.ext.marketing.db.execute("SELECT source, starts FROM referral_counts"))

    def test_typed_source_counted_once_per_event(self):
        self.assertTrue(self.send("/kyntrion Demo_01-x"))
        self.ext.handle(self.event, self.back)
        self.assertEqual(self.counts(), {"Demo_01-x": 1})
        self.api.send_message.assert_called_once()
        self.assertIn("KYNTRION", self.api.send_message.call_args.args[1])

    def test_client_start_ref_from_hydrated_message(self):
        self.assertTrue(self.send("Начать", ref="kyn_Expo", payload='{"command":"start"}'))
        self.assertEqual(self.counts(), {"Expo": 1})
        self.assertTrue(self.send("/piar"))
        self.back.assert_called_once_with(42)

    def test_ref_in_user_longpoll_extras(self):
        self.assertTrue(self.send("Добрый вечер", {"ref": "kyn_Expo"}))
        self.assertEqual(self.counts(), {"Expo": 1})

    def test_lookup_failure_preserves_native_entry_and_managed_errors(self):
        error = VkApiError("lookup failed")
        self.api.call.side_effect = error
        self.assertFalse(self.send("Начать"))
        with self.assertRaises(VkApiError):
            self.send("/kyntrion")
        self.api.call.side_effect = None
        self.assertTrue(self.send("/kyntrion"))
        self.api.call.side_effect = error
        with self.assertRaises(VkApiError):
            self.send("Начать")
        self.assertEqual(self.counts(), {})

    def test_piar_and_invalid_sources_stay_out_of_partner_counts(self):
        self.assertFalse(self.send("/start 12345ABCD"))
        self.api.call.assert_not_called()
        self.assertFalse(self.send("Начать", ref="12345ABCD", payload='{"command":"start"}'))
        self.assertFalse(self.send("Начать", ref="kyn_bad/source", payload='{"command":"start"}'))
        self.assertTrue(self.send("/kyntrion a b"))
        self.assertFalse(self.send("/start 12345ABCD"))
        self.assertEqual(self.counts(), {})
