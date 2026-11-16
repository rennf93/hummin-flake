import asyncio
import os
import struct
import tempfile
import unittest
import wave

from hummin_assistant import voice
from tests.test_context import StubEnv


def pcm(ms, amp, rate=16000):
    n = rate * ms // 1000
    return b"".join(struct.pack("<h", amp if i % 2 else -amp) for i in range(n))


class SilenceDetectorTests(unittest.TestCase):
    def test_no_speech_never_fires(self):
        self.assertFalse(voice.SilenceDetector().feed(pcm(500, 0)))

    def test_speech_then_silence(self):
        d = voice.SilenceDetector()
        self.assertFalse(d.feed(pcm(300, 3000)))
        self.assertTrue(d.feed(pcm(900, 0)))

    def test_max_ms(self):
        self.assertTrue(voice.SilenceDetector().feed(pcm(15000, 3000)))

    # Renzo's room measured 1429-1588 RMS of background (fans): a fixed 500 never saw quiet
    @staticmethod
    def stops_at(*segments):
        """Feed (ms, rms) segments in 100 ms chunks like record_utterance; seconds when it fires, else None."""
        d, t = voice.SilenceDetector(), 0
        for ms, amp in segments:
            for _ in range(ms // 100):
                t += 100
                if d.feed(pcm(100, amp)):
                    return t / 1000
        return None

    def test_noisy_room_stops_after_speech(self):
        self.assertLessEqual(self.stops_at((500, 1500), (1000, 6000), (5000, 1500)), 2.5)

    def test_noisy_room_speech_from_the_start(self):  # after "Hey Hummin" you may already be talking
        self.assertLessEqual(self.stops_at((1000, 6000), (5000, 1500)), 2.0)

    def test_noisy_room_soft_speech(self):
        self.assertLessEqual(self.stops_at((500, 1500), (1000, 2800), (5000, 1500)), 2.5)

    def test_uneven_noise_still_stops(self):
        wobble = [(100, 1450), (100, 1600), (100, 1500), (100, 1550)] * 10
        self.assertLessEqual(self.stops_at((500, 1500), (1000, 6000), *wobble), 2.5)

    def test_noise_alone_runs_to_the_cap(self):
        self.assertIsNone(self.stops_at((10000, 1500)))

    # review of 7844f5b: a running-minimum floor broke on one quiet chunk, rising noise and clicks
    def test_digital_silence_at_stream_start(self):
        self.assertLessEqual(self.stops_at((100, 0), (500, 1500), (2000, 5000), (5000, 1500)), 4.5)

    def test_one_dip_does_not_pin_the_floor(self):
        self.assertLessEqual(self.stops_at((500, 1500), (100, 600), (1000, 5000), (5000, 1500)), 3.5)

    def test_a_click_is_not_speech(self):
        stop = self.stops_at((300, 1500), (100, 20000), (1000, 1500), (1000, 5000), (5000, 1500))
        self.assertGreater(stop, 2.4)  # not 0.9 s after the click, before the speech at 1.4 s
        self.assertLessEqual(stop, 4.0)

    def test_fans_ramping_up_after_speech_still_end_it(self):  # fans spin up over seconds, not in one step
        ramp = [(100, 1500 + 40 * i) for i in range(60)]
        self.assertLessEqual(self.stops_at((500, 1500), (1000, 5000), *ramp), 2.6)

    def test_long_steady_speech_is_not_cut_off(self):
        # review of f61e8b8: 7-10 s of even-volume talk filled the 5 s window and was cut mid-sentence
        import random
        rnd = random.Random(7)
        for base in (100, 1500):
            for trial in range(40):
                speech = [(100, int(max(4000, 4 * base) * rnd.uniform(0.6, 1.2))) for _ in range(100)]  # 10 s
                noise = [(100, int(base * rnd.uniform(0.95, 1.05))) for _ in range(40)]
                stop = self.stops_at((500, base), *speech, *noise)
                self.assertIsNotNone(stop, (base, trial))
                self.assertGreaterEqual(stop, 10.5 + 0.8, (base, trial))  # speech ends at 10.5 s
                self.assertLessEqual(stop, 10.5 + 1.1, (base, trial))

    def test_a_pause_after_speech_first_is_not_the_end(self):  # you start talking right after "Hey Hummin"
        self.assertGreaterEqual(self.stops_at((1000, 6000), (300, 1500), (1000, 6000), (5000, 1500)), 3.2)

    def test_no_speech_with_a_quiet_first_chunk_runs_to_the_cap(self):
        self.assertIsNone(self.stops_at((100, 0), (10000, 1500)))

    # review of 264231d: a quiet first chunk shut the fan out of the floor, so recordings ran to the 15 s cap
    def test_a_quiet_first_chunk_does_not_lock_the_fan_out(self):
        for first in (3, 300, 790):
            self.assertLessEqual(self.stops_at((100, first), (1500, 1500), (2000, 6000), (5000, 1500)), 4.8, first)

    # review of ef51769: a loud word then steadier, softer talk looked like the room stepping up and was cut off
    def test_softer_talk_after_a_loud_word_is_not_cut_off(self):
        self.assertGreaterEqual(self.stops_at((2000, 1500), (300, 10000), (3000, 3000), (5000, 1500)), 6.1)
        import random
        rnd = random.Random(4)
        for trial in range(40):
            soft = [(100, int(rnd.uniform(2500, 4500))) for _ in range(60)]
            stop = self.stops_at((500, 1500), (300, 10000), *soft, (5000, 1500))
            self.assertGreaterEqual(stop, 6.8 + 0.8, trial)

    def test_speech_from_the_first_chunk_is_not_cut_off(self):
        import random
        rnd = random.Random(3)
        for trial in range(40):
            speech = [(100, int(6000 * rnd.uniform(0.6, 1.2))) for _ in range(rnd.randint(10, 100))]
            noise = [(100, int(1500 * rnd.uniform(0.95, 1.05))) for _ in range(40)]
            end = len(speech) / 10
            stop = self.stops_at(*speech, *noise)
            self.assertIsNotNone(stop, trial)
            self.assertGreaterEqual(stop, end + 0.8, trial)
            self.assertLessEqual(stop, end + 1.5, trial)


class VoiceTests(StubEnv):
    def test_transcribe(self):
        self.stub(out=" Hey Hummin\n")
        os.environ["HUMMIN_ASSISTANT_WHISPER_MODEL"] = "m.bin"
        self.assertEqual(asyncio.run(voice.transcribe("x.wav")), "Hey Hummin")

    def test_summarize(self):
        self.stub(out="short")
        self.assertEqual(asyncio.run(voice.summarize("Long. Answer.", "m")), "short")
        self.stub("fail")
        self.assertEqual(asyncio.run(voice.summarize("First one. Second.", "m")), "First one.")

    def test_say(self):
        os.environ["HUMMIN_ASSISTANT_VOICE"] = "v.onnx"
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "pcm")
            self.stub(out=out)
            asyncio.run(voice.Speaker().say("hello"))
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"PCMDATA")

    def test_say_reports_failure(self):
        self.stub("fail", out=os.devnull)
        self.assertFalse(asyncio.run(voice.Speaker().say("hello")))

    def test_a_new_say_cuts_the_old_one_off(self):
        self.stub(out=os.devnull)
        sp = voice.Speaker()

        async def go():
            os.environ["STUB_HANG"] = "1"
            first = asyncio.create_task(sp.say("one"))
            await asyncio.sleep(0.3)
            old = sp.procs
            del os.environ["STUB_HANG"]
            try:
                second = await sp.say("two")
                self.assertIsNotNone(old[1].poll())  # one voice at a time
            finally:
                for p in old:
                    if p.poll() is None:
                        p.kill()
            self.assertTrue(await first)  # being cut off is not a playback failure
            self.assertTrue(second)
        asyncio.run(go())

    def test_recorder_stop(self):
        async def go():
            r = voice.Recorder()
            r.start("x.raw")
            await r.stop()
            return r.proc.returncode
        self.assertIsNotNone(asyncio.run(go()))

    def test_recorder_stop_kills_a_recorder_that_ignores_sigint(self):
        os.environ["STUB_IGNORE_INT"] = "1"  # e.g. pw-record stuck on a wedged PipeWire
        self.addCleanup(os.environ.pop, "STUB_IGNORE_INT", None)

        async def go():
            r = voice.Recorder()
            r.start("x.raw")
            await asyncio.sleep(0.2)  # let the stub install its trap
            await asyncio.wait_for(r.stop(), 5)  # the mic must not stay open
            return r.proc.returncode
        self.assertIsNotNone(asyncio.run(go()))

    def test_record_utterance(self):
        with tempfile.TemporaryDirectory() as d:
            raw, args, wav = (os.path.join(d, n) for n in ("raw", "args", "out.wav"))
            with open(raw, "wb") as f:
                f.write(pcm(300, 3000) + pcm(1000, 0))
            os.environ["STUB_OUT"], os.environ["STUB_ARGS"] = raw, args
            self.addCleanup(os.environ.pop, "STUB_ARGS", None)
            asyncio.run(voice.Recorder().record_utterance(wav))
            with open(args) as f:
                self.assertIn("--raw", f.read().split())
            with wave.open(wav) as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
                self.assertEqual(w.getnframes(), 16000 * 1200 // 1000)  # stops after 900 ms of silence


if __name__ == "__main__":
    unittest.main()
