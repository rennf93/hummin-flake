#!/usr/bin/env bash
# Train the "Hey Hummin" openWakeWord model. Idempotent: every step skips if already done.
# Usage: train.sh [config.yml]   (default: hey_hummin.yml next to this script)
# Workspace: ~/hummin-wakeword-training (outside the repo). Output: <output_dir>/hey_hummin.onnx
set -euo pipefail
HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
W=$HOME/hummin-wakeword-training
CFG=${1:-$HERE/hey_hummin.yml}
mkdir -p "$W"; cd "$W"

nixpkg() { nix --extra-experimental-features 'nix-command flakes' build --no-link --print-out-paths \
  "path:$HOME/nixos-config#nixosConfigurations.omen.pkgs.$1" | head -1; }

# --- env ----------------------------------------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  PY311=$(nixpkg python311); UV=$(nixpkg uv)
  "$UV/bin/uv" venv -p "$PY311/bin/python3.11" .venv
fi
# nix-ld has no libstdc++/zlib by default; espeak-ng for piper-phonemize fallback; libcuda from the driver
LIBS="$(nixpkg stdenv.cc.cc.lib)/lib:$(nixpkg zlib)/lib:/run/opengl-driver/lib"
BINS="$W/.venv/bin:$(nixpkg uv)/bin:$(nixpkg git)/bin"
export LD_LIBRARY_PATH="$LIBS" PATH="$BINS:$PATH"
export PYTHONUNBUFFERED=1 TQDM_MININTERVAL=60

if [ ! -f .env-done ]; then
  [ -d openwakeword ] || git clone https://github.com/dscripka/openwakeword
  git -C openwakeword checkout -q 368c0371  # commit the first run used
  [ -d piper-sample-generator ] || git clone https://github.com/rhasspy/piper-sample-generator
  # master moved to a package needing piper-tts; v2.0.0 (matches the .pt release) has root generate_samples.py
  git -C piper-sample-generator checkout -q v2.0.0
  uv pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121
  # webrtcvad-wheels = prebuilt webrtcvad (no gcc on PATH); numpy<2, setuptools<81 (pkg_resources) for the old pins; tensorflow/onnx_tf skipped (tflite only)
  uv pip install "numpy<2" "setuptools<81" "pyarrow<15" "huggingface_hub<0.24" "fsspec[http]<=2023.10.0" "pandas<2.3" "scipy<1.15" piper-phonemize webrtcvad-wheels mutagen==1.47.0 torchinfo==1.8.0 \
    torchmetrics==1.2.0 speechbrain==0.5.14 audiomentations==0.33.0 torch-audiomentations==0.11.0 \
    acoustics==0.2.6 pronouncing==0.2.0 datasets==2.14.6 deep-phonemizer==0.0.19 soundfile onnx onnxruntime \
    -e ./openwakeword
  touch .env-done
fi
python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, torch.cuda.get_device_name(0))"

# --- models + data ------------------------------------------------------------------------------
R=openwakeword/openwakeword/resources/models; mkdir -p $R piper-sample-generator/models
fetch() { [ -s "$2" ] || { curl -fL -C - --retry 5 -o "$2.part" "$1" && mv "$2.part" "$2"; }; }
OWW=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1
for m in melspectrogram embedding_model; do fetch $OWW/$m.onnx $R/$m.onnx; fetch $OWW/$m.tflite $R/$m.tflite; done
fetch https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt \
  piper-sample-generator/models/en_US-libritts_r-medium.pt
HF=https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main
fetch $HF/validation_set_features.npy validation_set_features.npy
fetch $HF/openwakeword_features_ACAV100M_2000_hrs_16bit.npy openwakeword_features_ACAV100M_2000_hrs_16bit.npy

if [ ! -f .mit-done ]; then
python - <<'PY'
import os, numpy as np, scipy.io.wavfile, datasets
os.makedirs("mit_rirs", exist_ok=True)
for row in datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True):
    n = row['audio']['path'].split('/')[-1]
    scipy.io.wavfile.write(f"mit_rirs/{n}", 16000, (row['audio']['array']*32767).astype(np.int16))
PY
touch .mit-done; fi

if [ ! -f .audioset-done ]; then
  # HF repo moved from data/bal_train09.tar to parquet shards; shard 09 is the same ~1/38 slice
  mkdir -p audioset audioset_16k
  fetch https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/09.parquet audioset/bal_train_09.parquet
python - <<'PY'
import io, math, numpy as np, scipy.io.wavfile, scipy.signal, soundfile as sf, pyarrow.parquet as pq
pf = pq.ParquetFile("audioset/bal_train_09.parquet")
for batch in pf.iter_batches(batch_size=64, columns=["video_id", "audio"]):
    for vid, a in zip(batch.column("video_id").to_pylist(), batch.column("audio").to_pylist()):
        x, sr = sf.read(io.BytesIO(a["bytes"]), dtype="float32")
        if x.ndim > 1: x = x.mean(1)
        g = math.gcd(sr, 16000); x = scipy.signal.resample_poly(x, 16000 // g, sr // g)
        scipy.io.wavfile.write(f"audioset_16k/{vid}.wav", 16000, (np.clip(x, -1, 1)*32767).astype(np.int16))
PY
  touch .audioset-done; fi

if [ ! -f .fma-done ]; then
python - <<'PY'
import os, numpy as np, scipy.io.wavfile, datasets
os.makedirs("fma", exist_ok=True)
ds = iter(datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True)
          .cast_column("audio", datasets.Audio(sampling_rate=16000)))
for _ in range(3600//30):  # 1 h of 30 s clips
    row = next(ds)
    n = row['audio']['path'].split('/')[-1].replace(".mp3", ".wav")
    scipy.io.wavfile.write(f"fma/{n}", 16000, (row['audio']['array']*32767).astype(np.int16))
PY
touch .fma-done; fi

# --- pipeline (train.py resumes clip generation; stages marked done per output_dir) -------------
OUT=$(python -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['output_dir'])" "$CFG")
NAME=$(python -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['model_name'])" "$CFG")
mkdir -p "$OUT"
for stage in generate_clips augment_clips train_model; do
  if [ ! -f "$OUT/.$stage-done" ]; then
    echo "=== $stage $(date) ==="
    python "$HERE/train_wrapper.py" openwakeword/openwakeword/train.py --training_config "$CFG" --$stage
    touch "$OUT/.$stage-done"
  fi
done
ls -l "$OUT/$NAME.onnx"
echo "=== DONE $(date) ==="
