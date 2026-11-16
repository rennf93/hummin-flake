"""Assistant settings: the `assistant` key of serpantinum's settings.json."""
import json
import os

DEFAULTS = {"look": "popup", "voice": "typing", "wakeWord": True, "model": "openrouter/z-ai/glm-5.3-flash"}
LOOKS = ("popup", "dropdown", "side")
VOICES = ("typing", "voice_in", "voice_in_out")


def load(path=None):
    path = path or os.environ.get("QS_SETTINGS") or os.path.join(
        os.path.expanduser("~"), ".config", "serpantinum", "settings.json")
    out = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)["assistant"]
        out.update(user)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(DEFAULTS)
    if out["look"] not in LOOKS:
        out["look"] = DEFAULTS["look"]
    if out["voice"] not in VOICES:
        out["voice"] = DEFAULTS["voice"]
    return out
