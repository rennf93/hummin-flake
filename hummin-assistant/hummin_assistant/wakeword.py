import os
import subprocess
import sys

FRAME = 1280  # samples; openWakeWord scores 80 ms chunks


class Detector:
    def __init__(self, threshold=0.5, refractory_frames=25):
        self.threshold, self.refractory, self.cooldown = threshold, refractory_frames, 0

    def update(self, score):
        if self.cooldown:
            self.cooldown -= 1
            return False
        if score < self.threshold:
            return False
        self.cooldown = self.refractory
        return True


def main():
    from openwakeword.model import Model  # not at import time: tests run without it
    model = Model(wakeword_models=[os.environ["HUMMIN_ASSISTANT_WAKE_MODEL"]], inference_framework="onnx",
                  melspec_model_path=os.environ["HUMMIN_ASSISTANT_MELSPEC"],
                  embedding_model_path=os.environ["HUMMIN_ASSISTANT_EMBEDDING"])
    det = Detector(float(os.environ.get("HUMMIN_ASSISTANT_WAKE_THRESHOLD", "0.5")))
    scores = "--scores" in sys.argv[1:]  # threshold tuning: print each second's peak instead of waking
    rec = subprocess.Popen(["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "-"],
                           stdout=subprocess.PIPE)
    import numpy as np
    peak, n = 0.0, 0
    while True:
        buf = rec.stdout.read(FRAME * 2)
        if len(buf) < FRAME * 2:
            return 1
        score = max(model.predict(np.frombuffer(buf, dtype=np.int16)).values())
        if not scores:
            if det.update(score):
                print("wake", flush=True)
            continue
        peak, n = max(peak, score), n + 1
        if n == 12:  # 12 x 80 ms, about a second
            if peak >= 0.05:
                print(f"{peak:.2f}", flush=True)
            peak, n = 0.0, 0


if __name__ == "__main__":
    sys.exit(main())
