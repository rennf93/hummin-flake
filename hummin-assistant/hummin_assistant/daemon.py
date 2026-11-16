"""The assistant daemon: one Hummin RPC child, many UI clients on a unix socket."""
import asyncio
import fnmatch
import json
import os
import sys
import tempfile
import time

from . import context, errors, markup, notify, protocol, settings as settings_mod, voice
from .chat import Conversation
from .keys import KeyTracker
from .rpc import HumminRpc, RpcError

CONFIRM_TIMEOUT = 300
CAPTURE_DELAY = 0.25
TICK_AFTER = 0.31
HOLD_MAX = 60  # a hold whose key release never arrives must not keep the mic open
WAKE_TAIL = 2.0  # seconds after speech ends before a wake counts: the detector still hears the speakers


def log(*a):
    print("hummin-assistant:", *a, file=sys.stderr, flush=True)


class Daemon:
    def __init__(self, hummin_argv, settings_path=None):
        self.hummin_argv, self.settings_path = list(hummin_argv), settings_path
        self.settings = settings_mod.load(settings_path)
        self.clients = set()
        self.tasks = set()
        self.visible = self.mic = self.listening = self.speaking = False
        self.closed = True  # the user closed the assistant (or never opened it): stay quiet
        self.look = self.settings["look"]
        self.model = self.cfg_model = self.settings["model"]
        self.models = []
        self.all_models = []
        self.rpc = None
        self.conv = None
        self.keys = KeyTracker()
        self.recorder = voice.Recorder()
        self.speaker = voice.Speaker()
        self.wav = None
        self.listen_task, self.listen_recording = None, False
        self.wake_task, self.wake_quiet_until, self.speech = None, 0.0, None
        self.voice_prompt, self.voice_answer = False, ""
        self.prep = None
        self.kitty = None
        self.wake_proc = None
        self.confirm_msg = None  # the confirm event Hummin is waiting on
        self.notifying = set()
        self._start_lock = None

    # -- plumbing ---------------------------------------------------------
    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self._done)
        return t

    def _done(self, t):
        self.tasks.discard(t)
        if not t.cancelled() and t.exception():
            log("task failed", t.exception())

    async def emit(self, msg):
        for w in list(self.clients):
            try:
                w.write(protocol.encode(msg))
                await asyncio.wait_for(w.drain(), 1)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                self.drop(w)

    def drop(self, w):
        self.clients.discard(w)
        w.close()
        if not self.clients:  # nobody left to answer a confirm
            self.visible = False
            self.route_pending()

    def route_pending(self):
        """Hummin has no confirm timeout: a confirm nobody can see goes to a notification."""
        m = self.confirm_msg
        if m and self.conv.pending_confirm == m["id"] and m["id"] not in self.notifying:
            self.notifying.add(m["id"])
            self.spawn(self.background_confirm(m))

    def state(self):
        self.models = self.picker_models()
        return {"ev": "state", "visible": self.visible, "look": self.look, "busy": self.conv.busy if self.conv else False,
                "mic": self.mic, "listening": self.listening, "speaking": self.speaking,
                "model": self.model, "models": self.models}

    async def ensure(self):
        """Start (or lazily restart) Hummin."""
        async with self._start_lock:
            if self.rpc.alive:
                return
            self.rpc.argv = self.hummin_argv + ["--model", self.model]
            await self.rpc.start()
            self.conv.busy = False
            try:
                data = await self.rpc.request({"type": "get_available_models"})
                self.all_models = [{"id": f'{m["provider"]}/{m["id"]}', "name": m.get("name", m["id"]),
                                    "images": "image" in m.get("input", [])} for m in data["models"]]
                self.models = self.picker_models()
            except (RpcError, KeyError):
                pass

    def picker_models(self):
        """Hummin's enabledModels globs + local llamacpp-* fleet + the current model; full list if none set."""
        path = os.environ.get("HUMMIN_ASSISTANT_HUMMIN_SETTINGS") or os.path.join(
            os.path.expanduser("~"), ".hummin", "agent", "settings.json")
        try:
            with open(path, encoding="utf-8") as f:
                value = json.load(f)["enabledModels"]
        except (OSError, ValueError, KeyError, TypeError):
            value = None
        globs = [g for g in value if isinstance(g, str)] if isinstance(value, list) else []
        if not globs:
            return list(self.all_models)
        keep = [m for m in self.all_models if m["id"] == self.model or m["id"].startswith("llamacpp-")
                or any(fnmatch.fnmatchcase(m["id"], g) for g in globs)]
        return sorted(keep, key=lambda m: m["id"] != self.model)  # stable: current first

    async def pump(self):
        while True:
            ev = await self.rpc.events.get()
            try:
                if ev.get("type") == "_exit":
                    self.conv.busy = False
                    self.voice_prompt, self.voice_answer = False, ""  # Hummin died: nothing of that run gets read
                    self.conv.failed = None
                    self.conv.pending_confirm = self.confirm_msg = None
                    await self.emit({"ev": "state", "busy": False})
                else:
                    await self.conv.handle(ev)
            except Exception as e:  # one bad event must not stop the loop
                log("event failed", e)

    async def conv_emit(self, msg):
        if msg["ev"] == "done":
            msg = {**msg, "html": markup.render(msg["text"])}
        elif msg["ev"] == "error":
            msg = {**msg, "text": errors.friendly(msg.get("text"), self.model)}
        elif msg["ev"] == "retry":
            msg = {"ev": "status", "text": "" if msg.get("done") else
                   f'Retrying ({msg["attempt"]}/{msg["max"]}): {errors.friendly(msg["text"], self.model, brief=True)}'}
        await self.emit(msg)
        if msg["ev"] == "confirm":
            self.confirm_msg = msg
            if not (self.visible and self.clients):
                self.route_pending()
        elif msg["ev"] == "done" and self.voice_prompt:
            self.voice_answer = msg["text"]  # spoken once the run settles: earlier messages precede tool calls
        elif msg["ev"] == "error":
            self.voice_answer = ""  # a run that ends in an error has nothing worth reading out
        elif msg["ev"] == "state" and msg.get("busy") is False and self.voice_prompt:
            text, self.voice_prompt, self.voice_answer = self.voice_answer, False, ""
            if text and self.settings["voice"] == "voice_in_out" and not self.closed:
                self.spawn(self.speak(text, True))

    async def background_confirm(self, msg):
        try:
            allow = await asyncio.wait_for(notify.ask(msg["title"] or "Hummin", msg["message"] or ""), CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            allow = False
        finally:
            self.notifying.discard(msg["id"])
        await self.answer(msg["id"], allow)

    async def answer(self, id, allow):
        try:
            await self.conv.answer(id, allow)
        except RpcError:
            pass

    async def hush(self):
        """Stop speech now, including a summary still being prepared."""
        self.speech = None  # any speak() in flight is superseded and stays quiet
        self.voice_prompt, self.voice_answer = False, ""  # nor is the stopped answer read out when it settles
        self.speaker.stop()
        self.wake_quiet_until = time.monotonic() + WAKE_TAIL
        if self.speaking:
            self.speaking = False
            await self.emit({"ev": "state", "speaking": False})

    async def speak(self, text, summarise):
        self.speaking, self.speech = True, (mine := object())
        await self.emit({"ev": "state", "speaking": True})
        try:
            if summarise:
                text = await voice.summarize(text, self.model)
            if self.speech is mine:  # a newer speak() started while this one summarised: stay quiet
                if await self.speaker.say(text) is False:
                    await self.emit({"ev": "error", "text": "Couldn't play audio"})
        finally:
            self.wake_quiet_until = time.monotonic() + WAKE_TAIL
            if self.speech is mine:  # a newer speak() cut this one off and owns the flag now
                self.speaking = False
                await self.emit({"ev": "state", "speaking": False})

    async def set_model(self, model):
        await self.ensure()
        provider, _, model_id = model.partition("/")
        await self.rpc.request({"type": "set_model", "provider": provider, "modelId": model_id})
        self.model = model
        self.models = self.picker_models()
        await self.emit({"ev": "state", "model": self.model, "models": self.models})

    async def sync_model(self):
        """The settings page only writes settings.json: apply a model changed there once, so a model picked
        in the popup is not reverted by a settings reload that did not change it."""
        self.settings = settings_mod.load(self.settings_path)
        if self.settings["model"] != self.cfg_model:
            self.cfg_model = self.settings["model"]
            if self.cfg_model != self.model:
                await self.set_model(self.cfg_model)

    # -- summon / looks ---------------------------------------------------
    async def summon(self, look=None):
        self.closed = False
        await self.sync_model()
        look = look or self.settings["look"]
        if look == "dropdown":
            await self.dropdown()
            return
        self.visible, self.look = True, look
        models = self.picker_models()  # settings.json may have changed
        if models != self.models:
            self.models = models
            await self.emit({"ev": "state", "models": models})
        await self.emit({"ev": "show", "look": look})
        self.spawn(self.send_context())
        if not self.conv.busy:
            self.prep = self.spawn(self.fresh_session())

    async def send_context(self):
        await self.emit({"ev": "context", "selection": await context.selection()})

    async def fresh_session(self):
        try:
            await self.ensure()
            await self.conv.new()
        except (RpcError, OSError) as e:
            await self.emit({"ev": "error", "text": str(e)})

    async def hide(self):
        self.visible, self.closed = False, True
        await self.cancel_hold()  # Esc mid-hold: the release may be lost, and closed means the mic is off
        await self.hush()  # closed means quiet; the run itself keeps going (spec: background tasks)
        await self.emit({"ev": "hide"})
        self.route_pending()

    async def dropdown(self, session=None):
        if not (self.kitty and self.kitty.returncode is None):
            self.kitty = await asyncio.create_subprocess_exec(
                "kitty", "--class", "hummin-dropdown", "-e", "hummin", *(["--session", session] if session else []),
                cwd=os.path.expanduser("~"), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        # serpantinum's Lua dispatcher first: hyprctl exits 0 even when a classic dispatch is
        # rejected by a Lua config, so classic must be the fallback, not the first try
        p = await asyncio.create_subprocess_exec("hyprctl", "dispatch", 'hl.dsp.workspace.toggle_special("hummin")',
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await p.wait() != 0:  # stock Hyprland has no hl.dsp; use its native special-workspace dispatch
            p = await asyncio.create_subprocess_exec("hyprctl", "dispatch", "togglespecialworkspace", "hummin",
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.wait()

    async def move(self, look):
        if look == "dropdown":
            await self.ensure()
            file = await self.conv.session_file()
            try:
                await self.rpc.request({"type": "abort"}, timeout=5)
            except RpcError:
                pass
            await self.rpc.stop()  # one writer per session file
            self.conv.busy = False
            self.conv.pending_confirm = self.confirm_msg = None
            await self.dropdown(session=file)
            await self.hide()
        else:
            self.visible, self.look, self.closed = True, look, False
            await self.emit({"ev": "show", "look": look})

    # -- prompts and voice ------------------------------------------------
    async def prompt(self, text, screenshot=False, voice_=False):
        images = []
        if screenshot:
            await self.emit({"ev": "capture"})
            await asyncio.sleep(CAPTURE_DELAY)
            shot = await context.screenshot()
            if shot:
                images.append(shot)
            await self.emit({"ev": "show", "look": self.look, "keep": True})  # back after the capture: keep the chat
        await self.emit({"ev": "transcript" if voice_ else "user", "text": text, "html": markup.render(text)})
        self.voice_prompt = voice_ or (self.voice_prompt and self.conv.busy)  # a follow-up joins the run
        if self.prep:
            await asyncio.shield(self.prep)  # new_session must land before the prompt
        await self.ensure()
        await self.conv.ask(text, images)

    async def transcribe_and_send(self, wav):
        # listening is cleared by whoever recorded, when the recording ends: a newer hold may own the mic by now
        try:
            try:
                text = await voice.transcribe(wav)
            finally:
                if os.path.exists(wav):
                    os.unlink(wav)
            if text:
                await self.prompt(text, voice_=True)
        except (RpcError, OSError) as e:
            await self.emit({"ev": "error", "text": str(e)})

    async def hold_start(self):
        # record before the first await so a key-up during summon() still finds self.wav
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            self.recorder.start(wav)
        except OSError:
            os.unlink(wav)
            raise
        self.wav = wav
        self.mic = self.listening = True
        self.spawn(self.hold_cap(wav))
        await self.emit({"ev": "state", "mic": True, "listening": True})
        await self.summon()

    async def hold_cap(self, wav):
        await asyncio.sleep(HOLD_MAX)
        if self.wav == wav:  # still the same hold: its release never came (e.g. it overtook the press)
            await self.cancel_hold()
            await self.emit({"ev": "error", "text": f"Stopped listening after {HOLD_MAX} s: the key release never arrived."})

    async def cancel_hold(self):
        """End a hold without sending it: what the mic heard is dropped."""
        wav, self.wav = self.wav, None
        if not wav:
            return
        self.keys = KeyTracker()  # its release is lost; the next press starts clean
        await self.recorder.stop()
        if os.path.exists(wav):
            os.unlink(wav)
        self.listening, self.mic = False, bool(self.wake_proc)
        await self.emit({"ev": "state", "mic": self.mic, "listening": False})

    async def hold_end(self):
        wav, self.wav = self.wav, None
        if not wav:
            return
        await self.recorder.stop()
        self.listening, self.mic = False, bool(self.wake_proc)
        await self.emit({"ev": "state", "mic": self.mic, "listening": False})
        self.spawn(self.transcribe_and_send(wav))

    async def listen(self):
        """The popup's mic button: one utterance, ended by silence or a second click. Never toggles visibility."""
        if self.listen_recording:
            await self.recorder.stop()  # end the utterance now; the task processes what was captured
            return
        if self.listening or (self.listen_task and not self.listen_task.done()):
            return  # a hold or the wake word has the mic, or the last utterance is still being sent
        self.mic = self.listening = self.listen_recording = True
        await self.emit({"ev": "state", "mic": True, "listening": True})
        self.listen_task = self.spawn(self.listen_once())

    async def listen_once(self):
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            try:
                await self.recorder.record_utterance(wav)
            finally:
                self.listen_recording = self.listening = False
                self.mic = bool(self.wake_proc)
                await self.emit({"ev": "state", "mic": self.mic, "listening": False})
            await self.transcribe_and_send(wav)
        except Exception as e:
            log("listen failed", e)
            await self.emit({"ev": "error", "text": str(e)})
        finally:
            if os.path.exists(wav):
                os.unlink(wav)

    async def tick_later(self):
        await asyncio.sleep(TICK_AFTER)
        if self.keys.tick(time.monotonic()) == "hold_start":
            await self.hold_start()

    async def key(self, state):
        if state == "down" and self.keys.pressed:  # the bind doesn't repeat: the last release was lost
            state = "up"
        elif state == "down":
            self.keys.down(time.monotonic())
            self.spawn(self.tick_later())
            return
        act = self.keys.up(time.monotonic())
        if act == "hold_end":
            await self.hold_end()
        elif act == "tap":
            await self.toggle()

    async def toggle(self):
        if self.visible:
            await self.hide()
        else:
            await self.summon()

    def wake_ready(self):
        """A wake counts only when no recording, speech or earlier wake is under way (the mic hears the speakers)."""
        return not (self.wav or self.listening or self.speaking or time.monotonic() < self.wake_quiet_until
                    or (self.wake_task and not self.wake_task.done()))

    async def wake_once(self):
        wav = None
        try:
            await self.summon()
            if self.wav or self.listening:
                return  # a hold or the mic button took the mic while the popup opened
            fd, wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            self.listening = True
            await self.emit({"ev": "state", "listening": True})
            try:
                await self.recorder.record_utterance(wav)
            finally:
                self.listening = False
                await self.emit({"ev": "state", "listening": False})
            await self.transcribe_and_send(wav)
        except Exception as e:
            log("wake failed", e)
        finally:
            if wav and os.path.exists(wav):
                os.unlink(wav)

    async def wake_loop(self):
        self.wake_proc = await asyncio.create_subprocess_exec(
            os.environ["HUMMIN_ASSISTANT_WAKE"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.mic = True
        await self.emit({"ev": "state", "mic": True})
        try:
            # never block on a wake: lines read late would replay as extra questions
            async for line in self.wake_proc.stdout:
                if line.strip() == b"wake" and self.wake_ready():
                    self.wake_task = self.spawn(self.wake_once())
        finally:
            self.mic = False
            if self.wake_proc.returncode is None:
                self.wake_proc.kill()
            self.wake_proc = None

    # -- clients ----------------------------------------------------------
    async def command(self, w, m):
        c = m.get("cmd")
        if c == "hello":
            try:
                await self.ensure()  # fills the model list
            except OSError:
                pass
            w.write(protocol.encode(self.state()))
        elif c == "prompt":
            await self.prompt(m.get("text", ""), m.get("screenshot", False), m.get("voice", False))
        elif c == "confirm":
            await self.answer(m["id"], bool(m.get("allow")))
        elif c == "set_model":
            await self.set_model(m["model"])
        elif c == "abort":
            await self.hush()
            await self.ensure()
            await self.rpc.request({"type": "abort"}, timeout=5)
        elif c == "move":
            await self.move(m.get("look"))
        elif c == "speak_full":
            self.spawn(self.speak(self.conv.last_answer, False))
        elif c == "closed":
            await self.hide()
        elif c == "key":
            await self.key(m.get("state"))
        elif c == "listen":
            await self.listen()
        elif c == "toggle":
            await self.toggle()
        elif c == "show":
            if self.visible:
                await self.move(m.get("look"))
            else:
                await self.summon(m.get("look"))

    async def client(self, r, w):
        self.clients.add(w)
        try:
            async for line in r:
                m = protocol.decode(line)
                if m is None:
                    continue
                try:
                    await self.command(w, m)
                except (RpcError, OSError, KeyError) as e:
                    await self.emit({"ev": "error", "text": str(e)})
                except Exception as e:  # a wrongly typed field must not cut the UI off
                    log(f"bad command {m.get('cmd')!r}: {e!r}")
                    await self.emit({"ev": "error", "text": f"Bad command: {e}"})
        except ConnectionError:  # peer vanished: a normal disconnect
            pass
        finally:
            if w in self.clients:
                self.drop(w)

    async def serve(self):
        path = protocol.socket_path()
        env = {**os.environ, "HUMMIN_CONFIRM": "1"}
        self._start_lock = asyncio.Lock()
        self.rpc = HumminRpc(self.hummin_argv, env, os.path.expanduser("~"))
        self.conv = Conversation(self.rpc, self.conv_emit)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(self.client, path, limit=1 << 24)  # big pastes
        os.chmod(path, 0o600)
        self.spawn(self.pump())
        if self.settings["wakeWord"] and self.settings["voice"] != "typing" and os.environ.get("HUMMIN_ASSISTANT_WAKE"):
            self.spawn(self.wake_loop())
        try:
            await server.serve_forever()
        finally:
            for t in list(self.tasks):
                t.cancel()
            self.speaker.stop()
            for p in (self.kitty, self.wake_proc):
                if p and p.returncode is None:
                    p.kill()
            await self.rpc.stop()
            try:
                os.unlink(path)
            except OSError:
                pass
