import asyncio
import base64
import os
import unittest

from hummin_assistant import context, notify

STUBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stubs")


class StubEnv(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("PATH", "STUB_MODE", "STUB_OUT")}
        os.environ["PATH"] = STUBS + os.pathsep + os.environ["PATH"]
        os.environ.pop("STUB_MODE", None)
        os.environ.pop("STUB_OUT", None)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def stub(self, mode="", out=""):
        os.environ["STUB_MODE"] = mode
        os.environ["STUB_OUT"] = out


class ContextTests(StubEnv):
    def test_selection(self):
        self.stub(out="  sel \n")
        self.assertEqual(asyncio.run(context.selection()), "sel")

    def test_selection_truncated(self):
        self.stub(out="x" * 5000)
        self.assertEqual(len(asyncio.run(context.selection())), 4000)

    def test_selection_fail(self):
        self.stub("fail")
        self.assertEqual(asyncio.run(context.selection()), "")

    def test_screenshot(self):
        self.stub(out="PNGBYTES")
        r = asyncio.run(context.screenshot())
        self.assertEqual(r["type"], "image")
        self.assertEqual(r["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(r["data"]), b"PNGBYTES")

    def test_screenshot_fail(self):
        self.stub("fail")
        self.assertIsNone(asyncio.run(context.screenshot()))


class NotifyTests(StubEnv):
    def test_ask(self):
        for out, want in (("allow", True), ("deny", False), ("", False)):
            self.stub(out=out)
            self.assertIs(asyncio.run(notify.ask("t", "m")), want, out)
        self.stub("fail")
        self.assertIs(asyncio.run(notify.ask("t", "m")), False)
