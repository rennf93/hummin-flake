import json
import os
import tempfile
import unittest

from hummin_assistant import protocol, settings
from hummin_assistant.keys import KeyTracker


class ProtocolTest(unittest.TestCase):
    def test_roundtrip(self):
        msg = {"ev": "delta", "text": "é"}
        self.assertEqual(protocol.decode(protocol.encode(msg)), msg)

    def test_encode_keeps_unicode_and_newline(self):
        self.assertEqual(protocol.encode({"t": "é"}), '{"t": "é"}\n'.encode())

    def test_invalid(self):
        self.assertIsNone(protocol.decode(b"nope"))
        self.assertIsNone(protocol.decode(b"[1]"))


class SettingsTest(unittest.TestCase):
    def _load(self, data):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.json")
            with open(p, "w") as f:
                json.dump(data, f)
            return settings.load(p)

    def test_overlay(self):
        s = self._load({"assistant": {"look": "side"}})
        self.assertEqual(s["look"], "side")
        self.assertEqual(s["model"], "openrouter/z-ai/glm-5.3-flash")

    def test_missing(self):
        self.assertEqual(settings.load("/nonexistent/x.json"), settings.DEFAULTS)

    def test_bogus_look_and_voice(self):
        s = self._load({"assistant": {"look": "bogus", "voice": "nope"}})
        self.assertEqual(s["look"], "popup")
        self.assertEqual(s["voice"], "typing")


class KeyTrackerTest(unittest.TestCase):
    def test_tap(self):
        k = KeyTracker()
        k.down(0)
        self.assertEqual(k.up(0.1), "tap")

    def test_hold(self):
        k = KeyTracker()
        k.down(0)
        self.assertEqual(k.tick(0.31), "hold_start")
        self.assertIsNone(k.tick(0.5))
        self.assertEqual(k.up(1.0), "hold_end")
        self.assertIsNone(k.up(1.1))


if __name__ == "__main__":
    unittest.main()
