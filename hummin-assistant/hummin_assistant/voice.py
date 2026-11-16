import array
import asyncio
import collections
import math
import os
import signal
import subprocess
import sys
import wave

RATE = 16000
SUMMARY_PROMPT = ("Summarize the answer below in one or two short sentences for text-to-speech. "
                  "Plain words only, no markdown, no lists.\n\n")


class SilenceDetector:
    """Ends an utterance after silence_ms of quiet once speech was heard.

    Quiet is relative to the room. A fixed threshold alone failed on this laptop, whose fans sit at ~1500 RMS, so
    recordings always ran to max_ms. The noise floor is a low percentile of recent chunks that weren't speech: one
    silent or loud chunk can't pin it, slowly rising noise is followed, and long steady talking is never taken for the
    room (that cut people off mid-sentence). Speech must last speech_ms, so a click isn't speech. A chunk in the quieter
    half of everything recent is the room too, so a quiet first buffer can't shut the room out; quiet must also sit
    well under the loudest recent chunk, so talking never counts as the room's quiet.
    """
    SPEECH, QUIET = 1.8, 1.5  # x noise floor; QUIET stays above the noise's own wobble
    PEAK = 0.6  # quiet is also under this x the loudest chunk in the window
    WINDOW = 50  # chunks of history (5 s at record_utterance's 0.1 s chunks)

    def __init__(self, threshold_rms=500, silence_ms=900, max_ms=15000, speech_ms=250, rate=RATE):
        self.threshold, self.rate = threshold_rms, rate
        self.silence_n = rate * silence_ms // 1000
        self.max_n = rate * max_ms // 1000
        self.speech_n = rate * speech_ms // 1000
        self.total = self.quiet = 0
        self.levels = collections.deque(maxlen=self.WINDOW)  # (rms, samples) of every chunk: was there speech
        self.noise = collections.deque(maxlen=self.WINDOW)  # rms of chunks that weren't speech: the room
        self.floor = None
        self.heard = False

    def feed(self, chunk: bytes) -> bool:
        s = array.array("h")
        s.frombytes(chunk[: len(chunk) // 2 * 2])  # native order == s16le on supported hosts
        if not s:
            return False
        return self.level(math.sqrt(sum(x * x for x in s) / len(s)), len(s))

    def level(self, rms: float, n: int) -> bool:
        self.total += n
        self.levels.append((rms, n))
        # the median decides what isn't speech: one dip can't make the room's own noise look like speech
        typical = sorted(self.noise)[len(self.noise) // 2] if self.noise else None
        recent = sorted(r for r, _ in self.levels)
        if typical is None or rms < max(self.threshold, typical * self.SPEECH) or rms <= recent[len(recent) // 2]:
            if rms or self.noise:  # digital silence while the stream starts says nothing about the room
                self.noise.append(rms)
        if not self.noise:
            return self.total >= self.max_n
        floor = sorted(self.noise)[len(self.noise) // 10]  # ~10th percentile
        quiet = lambda r: r < max(self.threshold, floor * self.QUIET) and r < recent[-1] * self.PEAK
        if self.floor is not None and floor * self.QUIET < self.floor:
            # far quieter than assumed (we started mid-speech, or long talk raised it): recount the quiet run
            self.quiet = 0
            for r, k in list(self.levels)[-2::-1]:
                if not quiet(r):
                    break
                self.quiet += k
        self.floor = floor
        loud = max(self.threshold, floor * self.SPEECH)
        self.heard = sum(k for r, k in self.levels if r >= loud) >= self.speech_n
        self.quiet = self.quiet + n if quiet(rms) else 0
        return self.total >= self.max_n or (self.heard and self.quiet >= self.silence_n)


class Recorder:
    proc = None

    def start(self, path: str):
        """path '-' streams raw s16le to proc.stdout."""
        # --raw: without it pw-record prefixes an AU header that reads as loud samples
        self.proc = subprocess.Popen(
            ["pw-record", *(["--raw"] if path == "-" else []),
             "--rate", str(RATE), "--channels", "1", "--format", "s16", path],
            stdout=subprocess.PIPE if path == "-" else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    async def stop(self):
        p = self.proc  # a recording started while we wait must not be the one killed
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)
        if p:
            try:
                await asyncio.to_thread(p.wait, 2)
            except subprocess.TimeoutExpired:  # stuck (e.g. wedged PipeWire): the mic must not stay open
                p.kill()
                await asyncio.to_thread(p.wait)

    async def record_utterance(self, path: str) -> None:
        self.start("-")
        det, data = SilenceDetector(), bytearray()
        try:
            while True:
                chunk = await asyncio.to_thread(self.proc.stdout.read, 3200)
                if not chunk:
                    break
                data += chunk
                if det.feed(chunk):
                    break
        finally:
            await self.stop()
            # stop() reaps the process but not its stdout: the reader leaks an fd without this
            if self.proc and self.proc.stdout:
                self.proc.stdout.close()
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(bytes(data))


async def _run(*cmd, timeout, stdin=None):
    """Return stdout bytes of cmd, or None on any failure."""
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(p.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return None
    return out if p.returncode == 0 else None


async def transcribe(wav: str) -> str:
    out = await _run("whisper-cli", "-m", os.environ.get("HUMMIN_ASSISTANT_WHISPER_MODEL", ""),
                     "-f", wav, "-nt", "-np", "-l", "en", timeout=60)
    return (out or b"").decode(errors="replace").strip()


class Speaker:
    procs = ()

    async def say(self, text: str):
        self.stop()  # one voice at a time: a newer answer or the play button cuts the old one off
        piper = subprocess.Popen(
            ["piper", "-m", os.environ.get("HUMMIN_ASSISTANT_VOICE", ""), "--output-raw"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        play = subprocess.Popen(
            ["pw-play", "--raw", "--rate", "22050", "--channels", "1", "--format", "s16", "-"],
            stdin=piper.stdout, stderr=subprocess.PIPE)
        piper.stdout.close()
        self.procs = mine = (piper, play)
        def feed():
            try:
                piper.stdin.write(text.encode())
                piper.stdin.close()
            except BrokenPipeError:  # stop() killed piper mid-write
                pass
        await asyncio.to_thread(feed)
        await asyncio.to_thread(play.wait)
        await asyncio.to_thread(piper.wait)
        with play.stderr:
            if self.procs is mine and (piper.returncode or play.returncode):  # not stopped or superseded
                err = (await asyncio.to_thread(play.stderr.read)).decode(errors="replace").strip()
                print(f"hummin-assistant: playback failed (piper={piper.returncode} pw-play={play.returncode}): {err}",
                      file=sys.stderr, flush=True)
                return False
        return True

    def stop(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        self.procs = ()


async def summarize(answer: str, model: str) -> str:
    # llamacpp-* providers come from the hummin-local extension: without it the model is "not found"
    flags = [] if model.startswith("llamacpp-") else ["--no-extensions"]
    out = await _run("hummin", "-p", "--no-session", *flags, "--no-tools",
                     "--model", model, SUMMARY_PROMPT + answer, timeout=60)
    text = (out or b"").decode(errors="replace").strip()
    if text:
        return text
    i = answer.find(". ")
    return (answer[: i + 1] if i >= 0 else answer)[:200]
