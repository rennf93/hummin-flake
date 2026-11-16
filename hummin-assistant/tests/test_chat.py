import asyncio
import os
import sys
import unittest

from hummin_assistant.chat import Conversation, headlines
from hummin_assistant.rpc import HumminRpc

FAKE = os.path.join(os.path.dirname(__file__), "fake_hummin.py")


async def run(prompt, on_confirm=None):
    """Send prompt, return the UI events seen up to the final state busy=false."""
    rpc = HumminRpc([sys.executable, FAKE], dict(os.environ), "/tmp")
    out = []

    async def emit(e):
        out.append(e)

    conv = Conversation(rpc, emit)
    await rpc.start()
    try:
        await conv.new()
        await conv.ask(prompt, [])
        while True:
            ev = await asyncio.wait_for(rpc.events.get(), 5)
            await conv.handle(ev)
            if out and out[-1] == {"ev": "state", "busy": False}:
                return out
            if on_confirm and out and out[-1]["ev"] == "confirm":
                await rpc.respond_ui(out[-1]["id"], confirmed=on_confirm == "yes")
    finally:
        await rpc.stop()


class ChatTests(unittest.TestCase):
    def test_plain_prompt(self):
        evs = asyncio.run(run("hi"))
        self.assertEqual([e["ev"] for e in evs], ["state", "delta", "delta", "done", "state"])
        self.assertEqual([e.get("text") for e in evs[1:4]], ["Hel", "lo", "Hello"])
        self.assertEqual((evs[0]["busy"], evs[-1]["busy"]), (True, False))

    def test_memories(self):
        evs = asyncio.run(run("remember x"))
        self.assertIn({"ev": "memories", "items": ["menubar lost tray"]}, evs)

    def test_confirm(self):
        for answer, text in (("yes", "deleted"), ("no", "denied")):
            evs = asyncio.run(run("delete it", answer))
            c = next(e for e in evs if e["ev"] == "confirm")
            self.assertEqual(c["id"], "c1")
            self.assertEqual(next(e for e in evs if e["ev"] == "done")["text"], text)

    def test_session_file(self):
        async def go():
            rpc = HumminRpc([sys.executable, FAKE], dict(os.environ), "/tmp")
            await rpc.start()
            try:
                return await Conversation(rpc, None).session_file()
            finally:
                await rpc.stop()
        self.assertEqual(asyncio.run(go()), "/tmp/fake.jsonl")

    def test_ask_is_busy_before_the_ack(self):
        from hummin_assistant.rpc import RpcError
        seen = []

        class Rpc:
            async def request(self, cmd):  # summon() checks busy while this is in flight
                seen.append(conv.busy)

        class Failing:
            async def request(self, cmd):
                raise RpcError("down")
        conv = Conversation(Rpc(), None)
        asyncio.run(conv.ask("hi", []))
        self.assertEqual(seen, [True])
        conv = Conversation(Failing(), None)
        with self.assertRaises(RpcError):
            asyncio.run(conv.ask("hi", []))
        self.assertFalse(conv.busy)

    def test_stop_fails_a_request_in_flight_and_reports_the_exit(self):
        from hummin_assistant.rpc import RpcError

        async def go():
            rpc = HumminRpc(["sleep", "30"], dict(os.environ), os.getcwd())  # never answers
            await rpc.start()
            req = asyncio.create_task(rpc.request({"type": "get_state"}, timeout=10))
            await asyncio.sleep(0.2)
            await rpc.stop()
            with self.assertRaises(RpcError):
                await asyncio.wait_for(req, 2)  # not the 10 s timeout
            self.assertEqual(rpc.events.get_nowait(), {"type": "_exit"})
        for _ in range(5):  # stop() races the reader seeing EOF
            asyncio.run(go())

    def test_an_oversized_line_stops_the_child_so_it_can_restart(self):
        async def go():  # a line over the 16 MiB limit (e.g. a huge screenshot echoed back) kills the reader
            code = "import sys,time; sys.stdout.write('x' * (17 << 20) + '\\n'); sys.stdout.flush(); time.sleep(30)"
            rpc = HumminRpc([sys.executable, "-c", code], dict(os.environ), os.getcwd())
            await rpc.start()
            self.assertEqual(await asyncio.wait_for(rpc.events.get(), 5), {"type": "_exit"})
            self.assertFalse(rpc.alive)  # else ensure() never restarts it and every request times out
            await rpc.stop()
        asyncio.run(go())

    def test_headlines(self):
        self.assertEqual(headlines("Problem: a\nApproach: b\n\nProblem: " + "x" * 100), ["a", "x" * 79 + "…"])


if __name__ == "__main__":
    unittest.main()
