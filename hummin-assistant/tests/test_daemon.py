import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

from hummin_assistant import protocol
from hummin_assistant.daemon import Daemon

HERE = os.path.dirname(__file__)
FAKE = os.path.join(HERE, "fake_hummin.py")


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.log, self.hypr, self.kitty = (os.path.join(t, n) for n in ("log", "hypr", "kitty"))
        self.env = {"XDG_RUNTIME_DIR": t, "FAKE_LOG": self.log, "STUB_HYPR": self.hypr, "STUB_ARGS": self.kitty,
                    "STUB_OUT": "sel", "PATH": os.path.join(HERE, "stubs") + ":" + os.environ["PATH"],
                    "QS_SETTINGS": os.path.join(t, "settings.json"), "HUMMIN_ASSISTANT_WAKE": "",
                    "HUMMIN_ASSISTANT_HUMMIN_SETTINGS": os.path.join(t, "hummin-settings.json")}
        self.old = dict(os.environ)
        os.environ.update(self.env)
        self.settings = {}
        self.daemon_task = None

    async def asyncTearDown(self):
        if self.daemon_task:
            self.daemon_task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(self.daemon_task, return_exceptions=True), 10)
            except asyncio.TimeoutError:
                # Python 3.12: cancellation can leave the rpc child's pipes holding the
                # daemon task open. Kill the child — its stdout EOF lets every reader finish.
                p = getattr(getattr(self.daemon, "rpc", None), "_proc", None)
                if p and p.returncode is None:
                    p.kill()
                await asyncio.gather(self.daemon_task, return_exceptions=True)
        os.environ.clear()
        os.environ.update(self.old)

    async def start(self, settings=None):
        if settings is not None:
            with open(self.env["QS_SETTINGS"], "w") as f:
                json.dump({"assistant": settings}, f)
        self.daemon = Daemon([sys.executable, FAKE])
        self.daemon_task = asyncio.create_task(self.daemon.serve())
        for _ in range(100):
            if os.path.exists(protocol.socket_path()):
                break
            await asyncio.sleep(0.02)
        r, w = await asyncio.open_unix_connection(protocol.socket_path(), limit=1 << 24)  # echoes of big pastes
        self.addCleanup(w.close)
        return r, w

    async def send(self, w, **cmd):
        w.write(protocol.encode(cmd))
        await w.drain()

    async def expect(self, r, ev, timeout=3, **match):
        async def loop():
            while True:
                m = protocol.decode(await r.readline())
                self.assertIsNotNone(m, "socket closed")
                if m["ev"] == ev and all(m.get(k) == v for k, v in match.items()):
                    return m
        return await asyncio.wait_for(loop(), timeout)

    def logged(self):
        return self.read(self.log).split()

    def read(self, path):
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()

    async def test_hello_full_state(self):
        r, w = await self.start()
        await self.send(w, cmd="hello")
        st = await self.expect(r, "state")
        for k in ("visible", "look", "busy", "mic", "listening", "speaking", "model", "models"):
            self.assertIn(k, st)
        self.assertEqual(st["model"], "openrouter/z-ai/glm-5.3-flash")
        self.assertEqual([m["id"] for m in st["models"]], self.ALL)  # no enabledModels: full list
        self.assertEqual([m["images"] for m in st["models"]], [True, False, False, False])

    ALL = ["openrouter/z-ai/glm-5.3-flash", "openrouter/other/big-model",
           "llamacpp-nas-9991/north-mini-code", "zai/glm-4.7"]

    def enable(self, globs):
        with open(self.env["HUMMIN_ASSISTANT_HUMMIN_SETTINGS"], "w") as f:
            json.dump({"enabledModels": globs}, f)

    async def models_after_hello(self, r, w):
        await self.send(w, cmd="hello")
        return [m["id"] for m in (await self.expect(r, "state"))["models"]]

    async def test_models_filtered_by_enabled_globs(self):
        self.enable(["zai/*"])
        r, w = await self.start()
        # current model first, llamacpp-* always, then enabled globs, Hummin's order otherwise
        self.assertEqual(await self.models_after_hello(r, w),
                         ["openrouter/z-ai/glm-5.3-flash", "llamacpp-nas-9991/north-mini-code", "zai/glm-4.7"])

    async def test_current_model_survives_filter_and_set_model_accepts_any(self):
        self.enable(["zai/*"])
        r, w = await self.start()
        await self.models_after_hello(r, w)
        await self.send(w, cmd="set_model", model="openrouter/other/big-model")
        st = await self.expect(r, "state", model="openrouter/other/big-model")
        self.assertEqual([m["id"] for m in st["models"]],
                         ["openrouter/other/big-model", "llamacpp-nas-9991/north-mini-code", "zai/glm-4.7"])

    async def test_malformed_enabled_models_fall_back_to_full_list(self):
        r, w = await self.start()
        path = self.env["HUMMIN_ASSISTANT_HUMMIN_SETTINGS"]
        for raw in ("{not json", '{"enabledModels": "zai/*"}', '{"enabledModels": {"a": 1}}'):
            with open(path, "w") as f:
                f.write(raw)
            self.assertEqual(await self.models_after_hello(r, w), self.ALL, raw)

    async def test_settings_edit_shows_on_summon(self):
        r, w = await self.start()
        self.assertEqual(await self.models_after_hello(r, w), self.ALL)
        self.enable(["zai/*"])
        await self.send(w, cmd="toggle")
        st = await self.expect(r, "state", models=[m for m in await self.wanted()])
        self.assertEqual(len(st["models"]), 3)

    async def wanted(self):
        return [{"id": i, "name": n, "images": im} for i, n, im in [
            ("openrouter/z-ai/glm-5.3-flash", "Z.ai: GLM 5.3 Flash", True),
            ("llamacpp-nas-9991/north-mini-code", "North Mini Code", False),
            ("zai/glm-4.7", "GLM 4.7", False)]]

    async def test_empty_enabled_models_falls_back_to_full_list(self):
        self.enable([])
        r, w = await self.start()
        self.assertEqual(await self.models_after_hello(r, w), self.ALL)

    async def test_set_model_splits_on_first_slash(self):
        r, w = await self.start()
        await self.send(w, cmd="set_model", model="openrouter/z-ai/glm-5.3-flash")
        await self.expect(r, "state", model="openrouter/z-ai/glm-5.3-flash")
        self.assertIn("set_model openrouter z-ai/glm-5.3-flash", self.read(self.log).split("\n"))

    async def test_settings_page_model_change_is_applied_on_summon(self):
        r, w = await self.start({"model": "openrouter/a/one"})
        with open(self.env["QS_SETTINGS"], "w") as f:  # the settings page only writes the file
            json.dump({"assistant": {"model": "llamacpp-laptop-9996/bonsai-2-27b"}}, f)
        await self.send(w, cmd="show", look="popup")
        await self.expect(r, "state", model="llamacpp-laptop-9996/bonsai-2-27b")
        self.assertIn("set_model llamacpp-laptop-9996 bonsai-2-27b", self.read(self.log).split(chr(10)))

    async def test_unchanged_settings_do_not_revert_a_popup_pick(self):
        r, w = await self.start({"model": "openrouter/a/one"})
        await self.send(w, cmd="set_model", model="openrouter/b/two")
        await self.expect(r, "state", model="openrouter/b/two")
        await self.send(w, cmd="show", look="popup")
        await self.expect(r, "show")
        self.assertNotIn("set_model openrouter a/one", self.read(self.log).split(chr(10)))

    async def test_instant_disconnect_logs_no_exception(self):
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx))
        r, w = await self.start()
        for _ in range(5):
            r2, w2 = await asyncio.open_unix_connection(protocol.socket_path())
            await self.send(w2, cmd="hello")
            await self.send(w2, cmd="toggle")
            w2.close()
        await asyncio.sleep(0.5)
        self.assertEqual(errors, [])
        await self.send(w, cmd="hello")
        await self.expect(r, "state")  # daemon still serving

    async def test_a_big_paste_keeps_the_ui_connected(self):
        r, w = await self.start()
        text = "log line\n" * 20000  # ~180 KB, over asyncio's 64 KB default line limit
        await self.send(w, cmd="prompt", text=text, screenshot=False, voice=False)
        self.assertEqual((await self.expect(r, "user"))["text"], text)

    async def test_a_malformed_command_errors_without_dropping_the_ui(self):
        r, w = await self.start()
        await self.send(w, cmd="set_model", model=5)
        await self.expect(r, "error")
        await self.send(w, cmd="hello")
        await self.expect(r, "state")  # same connection still served

    async def test_screenshot_reshow_keeps_the_chat(self):
        r, w = await self.start()
        await self.send(w, cmd="prompt", text="hi", screenshot=True, voice=False)
        await self.expect(r, "capture")
        self.assertTrue((await self.expect(r, "show")).get("keep"))  # a summon's show clears; this one must not

    async def test_toggle_show_context_hide(self):
        r, w = await self.start()
        await self.send(w, cmd="toggle")
        self.assertEqual((await self.expect(r, "show"))["look"], "popup")
        self.assertEqual((await self.expect(r, "context"))["selection"], "sel")
        await self.send(w, cmd="toggle")
        await self.expect(r, "hide")

    async def test_show_is_fast_with_slow_new_session(self):
        os.environ["FAKE_SLOW_NEW"] = "1"
        r, w = await self.start()
        await self.send(w, cmd="toggle")
        await self.expect(r, "show", timeout=0.2)

    async def test_prompt_streams_typed_user_only(self):
        r, w = await self.start()
        await self.send(w, cmd="prompt", text="hi", screenshot=False, voice=False)
        self.assertEqual((await self.expect(r, "user"))["text"], "hi")
        await self.expect(r, "delta", text="Hel")
        await self.expect(r, "done", text="Hello")

    async def test_background_confirm_uses_notification(self):
        os.environ["STUB_OUT"] = "allow"
        r, w = await self.start()
        await self.send(w, cmd="prompt", text="delete it", screenshot=False, voice=False)
        await self.expect(r, "done", text="deleted")

    async def test_background_confirm_deny(self):
        os.environ["STUB_OUT"] = "deny"
        r, w = await self.start()
        await self.send(w, cmd="prompt", text="delete it", screenshot=False, voice=False)
        await self.expect(r, "done", text="denied")

    async def test_visible_confirm_waits_for_ui(self):
        os.environ["STUB_MODE"] = "fail"  # a notification would deny
        r, w = await self.start()
        await self.send(w, cmd="toggle")
        await self.send(w, cmd="prompt", text="delete it", screenshot=False, voice=False)
        c = await self.expect(r, "confirm")
        await self.send(w, cmd="confirm", id=c["id"], allow=True)
        await self.expect(r, "done", text="deleted")

    async def test_busy_toggle_does_not_start_new_session(self):
        r, w = await self.start()
        await self.send(w, cmd="toggle")
        await self.expect(r, "context")
        await self.send(w, cmd="prompt", text="hang", screenshot=False, voice=False)
        await self.expect(r, "state", busy=True)
        await self.send(w, cmd="toggle")
        await self.expect(r, "hide")
        await self.send(w, cmd="toggle")
        await self.expect(r, "show")
        await self.expect(r, "context")
        self.assertEqual(self.logged().count("new_session"), 1)

    async def test_tap_key_toggles(self):
        r, w = await self.start()
        await self.send(w, cmd="key", state="down")
        await asyncio.sleep(0.05)
        await self.send(w, cmd="key", state="up")
        await self.expect(r, "show")

    async def test_hold_key_records_and_sends_voice_prompt(self):
        os.environ["STUB_OUT"] = "hi there"
        r, w = await self.start()
        await self.send(w, cmd="key", state="down")
        await self.expect(r, "state", listening=True, timeout=2)
        await self.send(w, cmd="key", state="up")
        self.assertEqual((await self.expect(r, "transcript"))["text"], "hi there")
        await self.expect(r, "done", text="Hello")

    async def test_esc_during_a_hold_stops_listening_and_the_next_tap_opens(self):
        r, w = await self.start()
        await self.send(w, cmd="key", state="down")  # its release never arrives
        await self.expect(r, "state", listening=True, timeout=2)
        await self.send(w, cmd="closed")
        await self.expect(r, "state", listening=False, timeout=2)  # closing turns the mic off
        await self.send(w, cmd="key", state="down")
        await asyncio.sleep(0.05)
        await self.send(w, cmd="key", state="up")
        await self.expect(r, "show")

    async def test_a_second_press_ends_a_hold_whose_release_was_lost(self):
        os.environ["STUB_OUT"] = "hi there"
        r, w = await self.start()
        await self.send(w, cmd="key", state="down")
        await self.expect(r, "state", listening=True, timeout=2)
        await self.send(w, cmd="key", state="down")  # no bind repeats, so the release was lost
        self.assertEqual((await self.expect(r, "transcript"))["text"], "hi there")
        await self.send(w, cmd="key", state="up")  # that press's own release: nothing to end
        await self.send(w, cmd="key", state="down")
        await asyncio.sleep(0.05)
        await self.send(w, cmd="key", state="up")
        await self.expect(r, "hide")  # a plain tap toggles again

    async def test_a_release_before_its_press_cannot_record_forever(self):
        with unittest.mock.patch("hummin_assistant.daemon.HOLD_MAX", 1, create=True):
            r, w = await self.start()
            await self.send(w, cmd="key", state="up")  # the two key processes raced: up landed first
            await self.send(w, cmd="key", state="down")
            await self.expect(r, "state", listening=True, timeout=2)
            await self.expect(r, "state", listening=False, timeout=3)
            evs = await self.events_for(r, 0.5)
            self.assertFalse(any(e["ev"] == "transcript" for e in evs))  # room audio is never sent

    async def test_empty_transcript_sends_no_prompt(self):
        os.environ["STUB_OUT"] = ""
        r, w = await self.start()
        await self.send(w, cmd="key", state="down")
        await self.expect(r, "state", listening=True, timeout=2)
        await self.send(w, cmd="key", state="up")
        await self.expect(r, "state", listening=False)
        await asyncio.sleep(0.2)
        self.assertNotIn("prompt", self.logged())

    async def test_move_dropdown_runs_kitty_and_hyprctl(self):
        r, w = await self.start()
        await self.send(w, cmd="move", look="dropdown")
        await self.expect(r, "hide")
        for _ in range(50):
            if os.path.exists(self.hypr) and os.path.exists(self.kitty):
                break
            await asyncio.sleep(0.05)
        self.assertIn('hl.dsp.workspace.toggle_special("hummin")', self.read(self.hypr))
        self.assertEqual(self.read(self.kitty).split()[-2:], ["--session", "/tmp/fake.jsonl"])
        self.assertIn("abort", self.logged())

    async def confirm_pending(self, w, r):
        os.environ["STUB_OUT"] = "allow"
        os.environ["STUB_NOTIFY"] = os.path.join(self.tmp.name, "notify")
        await self.send(w, cmd="toggle")
        await self.send(w, cmd="prompt", text="delete it", screenshot=False, voice=False)
        await self.expect(r, "confirm")

    async def wait_ui_response(self):
        for _ in range(100):
            if "ui_response" in self.logged():
                break
            await asyncio.sleep(0.03)
        self.assertIn("ui_response", self.logged())
        self.assertEqual(self.read(os.environ["STUB_NOTIFY"]).split("\n")[-3:-1], ["Delete?", "Really?"])

    async def test_closed_with_pending_confirm_notifies(self):
        r, w = await self.start()
        await self.confirm_pending(w, r)
        await self.send(w, cmd="closed")
        await self.wait_ui_response()

    async def test_disconnect_with_pending_confirm_notifies(self):
        r, w = await self.start()
        await self.confirm_pending(w, r)
        w.close()
        await self.wait_ui_response()

    async def test_transcribe_failure_resets_listening_and_errors(self):
        os.environ["STUB_MODE"] = "fail"  # whisper returns "" ; make the voice prompt itself fail
        r, w = await self.start()
        d = self.daemon
        async def boom(*a, **k):
            raise OSError("whisper missing")
        import hummin_assistant.voice as v
        orig, v.transcribe = v.transcribe, boom
        self.addCleanup(setattr, v, "transcribe", orig)
        await self.send(w, cmd="key", state="down")
        await self.expect(r, "state", listening=True, timeout=2)
        await self.send(w, cmd="key", state="up")
        await self.expect(r, "state", listening=False)  # reset as soon as transcription ends
        await self.expect(r, "error")
        self.assertFalse(d.listening)

    async def test_key_up_during_summon_still_stops_recording(self):
        os.environ["STUB_OUT"] = "hi there"
        orig = Daemon.summon
        async def slow(self_, *a, **k):
            await asyncio.sleep(0.4)
            await orig(self_, *a, **k)
        Daemon.summon = slow
        self.addCleanup(setattr, Daemon, "summon", orig)
        r, w = await self.start()
        await self.send(w, cmd="key", state="down")
        await self.expect(r, "state", listening=True, timeout=2)
        await self.send(w, cmd="key", state="up")
        self.assertEqual((await self.expect(r, "transcript"))["text"], "hi there")

    async def test_listen_transcribes_and_prompts_without_toggling(self):
        os.environ["STUB_OUT"] = "hi there"
        r, w = await self.start()
        await self.send(w, cmd="toggle")
        await self.expect(r, "show")
        await self.send(w, cmd="listen")
        await self.expect(r, "state", listening=True)
        self.assertEqual((await self.expect(r, "transcript"))["text"], "hi there")
        await self.expect(r, "done", text="Hello")
        self.assertTrue(self.daemon.visible)

    async def test_second_listen_stops_early(self):
        os.environ["STUB_OUT"], os.environ["STUB_HANG"] = "hi there", "1"  # pw-record never ends by itself
        r, w = await self.start()
        await self.send(w, cmd="listen")
        await self.expect(r, "state", listening=True)
        await self.send(w, cmd="listen")
        self.assertEqual((await self.expect(r, "transcript"))["text"], "hi there")
        self.assertFalse(self.daemon.visible)

    async def test_listen_while_sending_starts_no_second_recording(self):
        orig = Daemon.prompt

        async def slow(self_, *a, **k):
            await asyncio.sleep(0.4)
            await orig(self_, *a, **k)
        Daemon.prompt = slow
        self.addCleanup(setattr, Daemon, "prompt", orig)
        os.environ["STUB_OUT"] = "hi there"
        r, w = await self.start()
        await self.send(w, cmd="listen")
        await self.expect(r, "state", listening=True)
        await self.expect(r, "state", listening=False)  # transcribed; prompt() is still sleeping
        first = self.daemon.listen_task
        await self.send(w, cmd="listen")
        await asyncio.sleep(0.1)
        self.assertIs(self.daemon.listen_task, first)
        self.assertEqual((await self.expect(r, "transcript"))["text"], "hi there")

    async def test_mic_click_leaves_a_hold_alone(self):
        orig = Daemon.prompt

        async def slow(self_, *a, **k):
            await asyncio.sleep(0.5)
            await orig(self_, *a, **k)
        Daemon.prompt = slow
        self.addCleanup(setattr, Daemon, "prompt", orig)
        os.environ["STUB_OUT"] = "hi there"
        r, w = await self.start()
        d = self.daemon
        await self.send(w, cmd="listen")
        await self.expect(r, "state", listening=True)
        await self.expect(r, "state", listening=False)  # transcribed; prompt() is still sleeping
        os.environ["STUB_HANG"] = "1"  # the hold's pw-record runs until key up
        await self.send(w, cmd="key", state="down")
        await self.expect(r, "state", listening=True, timeout=2)
        hold = d.recorder.proc
        await self.send(w, cmd="listen")
        await asyncio.sleep(0.8)  # the click, then the first utterance's prompt finishing
        self.assertIsNone(hold.poll())  # the click did not cut the hold
        self.assertTrue(d.listening)  # the first utterance's cleanup did not reset it
        await self.send(w, cmd="key", state="up")

    async def test_hold_during_transcription_keeps_listening(self):
        import hummin_assistant.voice as v
        orig = v.transcribe

        async def slow(wav):
            await asyncio.sleep(1.2)
            return await orig(wav)
        v.transcribe = slow
        self.addCleanup(setattr, v, "transcribe", orig)
        os.environ["STUB_OUT"] = "hi there"
        r, w = await self.start()
        d = self.daemon
        await self.send(w, cmd="listen")
        await self.expect(r, "state", listening=True)
        await asyncio.sleep(0.1)  # the stub recording has ended; transcription is sleeping
        os.environ["STUB_HANG"] = "1"  # the hold's pw-record runs until key up
        await self.send(w, cmd="key", state="down")
        await asyncio.sleep(0.6)
        self.assertIsNotNone(d.wav)  # the hold started inside the transcription window
        await asyncio.sleep(0.8)  # the first utterance's transcription ends
        self.assertTrue(d.listening)
        await self.send(w, cmd="key", state="up")

    async def events_for(self, r, secs):
        out, loop = [], asyncio.get_running_loop()
        end = loop.time() + secs
        while (left := end - loop.time()) > 0:
            try:
                out.append(protocol.decode(await asyncio.wait_for(r.readline(), left)))
            except asyncio.TimeoutError:
                break
        return out

    async def start_with_wake(self, wakes):
        os.environ.update({"HUMMIN_ASSISTANT_WAKE": os.path.join(HERE, "stubs", "wake"), "STUB_WAKES": wakes,
                           "STUB_OUT": "hi there"})
        return await self.start({"voice": "voice_in", "wakeWord": True})

    async def test_wakes_during_a_wake_are_dropped_not_replayed(self):
        import hummin_assistant.voice as v
        orig = v.transcribe

        async def slow(wav):
            await asyncio.sleep(0.6)
            return await orig(wav)
        v.transcribe = slow
        self.addCleanup(setattr, v, "transcribe", orig)
        r, w = await self.start_with_wake("0.1 0.1 0.1")  # three wakes while the first is being handled
        evs = await self.events_for(r, 2.0)
        self.assertEqual(sum(e["ev"] == "transcript" for e in evs), 1)

    async def test_wake_right_after_speaking_is_ignored(self):
        r, w = await self.start_with_wake("0.6")  # the mic hears the speakers' last words
        os.environ["STUB_OUT"] = os.path.join(self.tmp.name, "pcm")  # the speaker stub writes its audio here
        await self.daemon.speak("Hummin here", False)
        evs = await self.events_for(r, 1.2)
        self.assertFalse(any(e["ev"] == "state" and e.get("listening") for e in evs))
        self.assertFalse(any(e["ev"] == "transcript" for e in evs))

    def fake_voice(self, say_secs=0.0, summary_secs=0.0):
        import hummin_assistant.voice as v
        said = []

        class Speaker:
            async def say(self, text):
                said.append(text)
                await asyncio.sleep(say_secs)
                return True

            def stop(self):
                pass

        async def summarize(text, model):
            await asyncio.sleep(summary_secs)
            return text
        orig = v.summarize
        v.summarize = summarize
        self.addCleanup(setattr, v, "summarize", orig)
        self.daemon.speaker = Speaker()
        return said

    async def test_voice_answer_speaks_the_final_message(self):
        r, w = await self.start({"voice": "voice_in_out"})
        said = self.fake_voice()
        await self.send(w, cmd="toggle")
        await self.expect(r, "show")
        await self.send(w, cmd="prompt", text="use a tool", screenshot=False, voice=True)
        await self.expect(r, "state", busy=False)
        await asyncio.sleep(0.2)
        self.assertEqual(said, ["Hello"])  # not the "Let me check." before the tool call

    async def test_voice_answer_speaks_in_the_dropdown_look(self):
        r, w = await self.start({"voice": "voice_in_out", "look": "dropdown"})  # summon opens kitty, no popup
        said = self.fake_voice()
        await self.send(w, cmd="toggle")
        await asyncio.sleep(0.2)
        await self.send(w, cmd="prompt", text="hi", screenshot=False, voice=True)
        await self.expect(r, "state", busy=False)
        await asyncio.sleep(0.2)
        self.assertEqual(said, ["Hello"])

    async def test_no_voice_answer_while_the_popup_is_closed(self):
        r, w = await self.start({"voice": "voice_in_out"})
        said = self.fake_voice()
        # e.g. closed during transcription: the voice prompt lands after the close
        await self.send(w, cmd="prompt", text="hi", screenshot=False, voice=True)
        await self.expect(r, "state", busy=False)
        await asyncio.sleep(0.2)
        self.assertEqual(said, [])

    async def test_a_failed_run_speaks_nothing_stale(self):
        r, w = await self.start({"voice": "voice_in_out"})
        said = self.fake_voice()
        await self.send(w, cmd="toggle")  # voice answers are spoken only while the popup is open
        await self.expect(r, "show")
        await self.send(w, cmd="prompt", text="hi", screenshot=False, voice=True)
        await self.expect(r, "state", busy=False)
        await self.send(w, cmd="prompt", text="fail please", screenshot=False, voice=True)
        await self.expect(r, "error")
        await self.expect(r, "state", busy=False)
        await asyncio.sleep(0.2)
        self.assertEqual(said, ["Hello"])  # not "Let me check." and not the earlier "Hello" again

    async def test_a_crash_forgets_the_pending_voice_answer(self):
        await self.start({"voice": "voice_in_out"})
        d = self.daemon
        await d.ensure()
        d.voice_prompt = True
        await d.conv_emit({"ev": "done", "text": "old answer"})
        await d.rpc.events.put({"type": "_exit"})
        await asyncio.sleep(0.1)
        self.assertEqual((d.voice_prompt, d.voice_answer), (False, ""))

    async def test_abort_silences_a_summary_in_progress(self):
        r, w = await self.start()
        d = self.daemon
        said = self.fake_voice(summary_secs=0.3)
        task = asyncio.create_task(d.speak("long answer", True))
        await asyncio.sleep(0.05)
        await self.send(w, cmd="abort")
        await task
        self.assertEqual(said, [])
        self.assertFalse(d.speaking)

    async def test_closing_the_popup_stops_talking(self):
        r, w = await self.start({"voice": "voice_in_out"})
        d = self.daemon
        said = self.fake_voice(summary_secs=0.3)
        await self.send(w, cmd="toggle")
        await self.expect(r, "show")
        task = asyncio.create_task(d.speak("a long answer", True))  # still summarising when Esc comes
        d.voice_prompt, d.voice_answer = True, "the next answer"
        await asyncio.sleep(0.05)
        await self.send(w, cmd="closed")
        await task
        self.assertEqual(said, [])
        self.assertFalse(d.speaking)
        self.assertEqual((d.voice_prompt, d.voice_answer), (False, ""))

    async def test_abort_drops_the_answer_still_to_be_read(self):
        r, w = await self.start({"voice": "voice_in_out"})
        d = self.daemon
        d.voice_prompt, d.voice_answer = True, "half an answer"
        await self.send(w, cmd="abort")
        await asyncio.sleep(0.1)
        self.assertEqual((d.voice_prompt, d.voice_answer), (False, ""))

    async def test_older_summary_does_not_cut_newer_playback(self):
        await self.start()
        d = self.daemon
        said = self.fake_voice(say_secs=0.1, summary_secs=0.3)
        auto = asyncio.create_task(d.speak("long answer", True))  # still summarising
        await asyncio.sleep(0.05)
        await d.speak("full answer", False)  # the play button
        await auto
        self.assertEqual(said, ["full answer"])
        self.assertFalse(d.speaking)

    async def test_wake_leaves_a_hold_that_started_during_summon(self):
        orig = Daemon.summon

        async def held(self_, *a, **k):
            await orig(self_, *a, **k)
            self_.wav, self_.listening = "held.wav", True  # Super+Space went down while the popup opened
        Daemon.summon = held
        self.addCleanup(setattr, Daemon, "summon", orig)
        r, w = await self.start_with_wake("0.1")
        evs = await self.events_for(r, 1.0)
        d = self.daemon
        self.assertTrue(d.listening)
        self.assertFalse(any(e["ev"] == "transcript" for e in evs))
        d.wav, d.listening = None, False

    async def test_a_hummin_crash_says_why(self):
        os.environ["FAKE_CRASH"] = "1"
        r, w = await self.start()
        await self.send(w, cmd="toggle")
        text = (await self.expect(r, "error", timeout=5))["text"]
        self.assertTrue(text.startswith("Hummin stopped: Error: EACCES: permission denied, copyfile"), text)

    async def test_provider_errors_read_as_plain_sentences(self):
        r, w = await self.start()
        await self.send(w, cmd="prompt", text="fail please", screenshot=False, voice=False)
        self.assertEqual((await self.expect(r, "error"))["text"],
                         "OpenRouter is out of credit for this request. Pick another model in the menu, "
                         "or try again later. (need more credits)")

    async def run_until_settled(self, r, w, text):
        await self.send(w, cmd="prompt", text=text, screenshot=False, voice=False)
        evs = []
        while not (evs and evs[-1]["ev"] == "state" and evs[-1].get("busy") is False):
            evs.append(protocol.decode(await asyncio.wait_for(r.readline(), 3)))
        return evs

    async def test_a_retry_that_succeeds_shows_status_and_no_error(self):
        r, w = await self.start()
        evs = await self.run_until_settled(r, w, "retry please")
        self.assertEqual([e["text"] for e in evs if e["ev"] == "status"],
                         ["Retrying (1/3): OpenRouter is rate-limiting requests.", ""])
        self.assertEqual([e for e in evs if e["ev"] == "error"], [])
        self.assertIn({"ev": "done", "text": "Hello", "html": "Hello"}, evs)

    async def test_an_error_before_a_queued_follow_up_still_shows(self):
        r, w = await self.start()
        evs = await self.run_until_settled(r, w, "queued question")
        self.assertEqual([e["text"][:35] for e in evs if e["ev"] == "error"], ["OpenRouter is out of credit for thi"])
        self.assertIn("ok", [e.get("text") for e in evs if e["ev"] == "done"])

    async def test_overflow_compaction_that_recovers_shows_no_error(self):
        r, w = await self.start()
        evs = await self.run_until_settled(r, w, "overflow")
        self.assertEqual([e for e in evs if e["ev"] == "error"], [])
        self.assertIn("ok", [e.get("text") for e in evs if e["ev"] == "done"])

    async def test_stopping_during_a_retry_shows_no_error(self):
        r, w = await self.start()
        evs = await self.run_until_settled(r, w, "cancelled")
        self.assertEqual([e for e in evs if e["ev"] == "error"], [])
        self.assertEqual([e["text"] for e in evs if e["ev"] == "status"][-1], "")

    async def test_retries_that_all_fail_show_one_error(self):
        r, w = await self.start()
        evs = await self.run_until_settled(r, w, "give up")
        errs = [e["text"] for e in evs if e["ev"] == "error"]
        self.assertEqual(len(errs), 1, errs)
        self.assertTrue(errs[0].startswith("OpenRouter is rate-limiting requests."), errs[0])

    async def test_events_carry_safe_html(self):
        r, w = await self.start()
        await self.send(w, cmd="prompt", text="**hi** <b>", screenshot=False, voice=False)
        self.assertEqual((await self.expect(r, "user"))["html"], "<b>hi</b> &lt;b&gt;")
        self.assertEqual((await self.expect(r, "done"))["html"], "Hello")

    async def test_move_side(self):
        r, w = await self.start()
        await self.send(w, cmd="move", look="side")
        self.assertEqual((await self.expect(r, "show"))["look"], "side")


class NotifyTests(unittest.TestCase):
    def test_double_dash_precedes_model_text(self):
        from hummin_assistant import notify
        with tempfile.TemporaryDirectory() as t:
            out = os.path.join(t, "argv")
            env = {"PATH": os.path.join(HERE, "stubs") + ":" + os.environ["PATH"], "STUB_NOTIFY": out, "STUB_OUT": "allow"}
            with unittest.mock.patch.dict(os.environ, env):
                self.assertTrue(asyncio.run(notify.ask("--evil", "-A x=y")))
            with open(out) as f:
                argv = f.read().split("\n")
        self.assertEqual(argv[argv.index("--") + 1:][:2], ["--evil", "-A x=y"])
        # serpantinum's Do Not Disturb hides every popup except critical ones (NotificationPopups.qml:207)
        self.assertIn("--urgency=critical", argv[:argv.index("--")])
        # a click on the notification body invokes the first action (serpantinum Notification.qml:333-342)
        self.assertEqual(argv[argv.index("-A") + 1], "deny=Deny")


class CliTests(unittest.TestCase):
    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "hummin_assistant", *args], capture_output=True, text=True,
                              timeout=10, env=env, cwd=os.path.dirname(HERE))

    def test_no_daemon(self):
        with tempfile.TemporaryDirectory() as t:
            p = self.run_cli("toggle", env={**os.environ, "XDG_RUNTIME_DIR": t})
        self.assertEqual(p.returncode, 1)
        self.assertIn("daemon not running", p.stderr)

    def test_no_runtime_dir(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        p = self.run_cli("toggle", env=env)
        self.assertEqual(p.returncode, 1)
        self.assertIn("XDG_RUNTIME_DIR", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
