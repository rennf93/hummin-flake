"""Run openwakeword train.py with "hummin" added to the CMU dictionary.

"hummin" is OOV, which makes openwakeword download the DeepPhonemizer model (its S3 host is now 403).
generate_adversarial_texts only reaches that path when pronouncing.phones_for_word() returns [].
Usage: python train_wrapper.py <path/to/train.py> <train.py args...>
"""
import runpy
import sys

import pronouncing

pronouncing.init_cmu()
PHONES = "HH AH1 M IH0 N"
pronouncing.lookup["hummin"].append(PHONES)
pronouncing.pronunciations.append(("hummin", PHONES))
assert pronouncing.phones_for_word("hummin") == [PHONES]

script = sys.argv[1]
sys.argv = [script] + sys.argv[2:]
try:
    runpy.run_path(script, run_name="__main__")
except ModuleNotFoundError as e:
    # train.py's --convert_to_tflite default is the truthy string "False", so it always tries the
    # tflite export (needs TensorFlow, skipped on purpose) AFTER the .onnx is already saved.
    if e.name != "onnx_tf":
        raise
    print("tflite export skipped (onnx_tf not installed); .onnx already saved")
