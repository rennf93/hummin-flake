"""Line-delimited JSON over a unix socket: UI->daemon carry "cmd", daemon->UI carry "ev"."""
import json
import os


def socket_path():
    return os.path.join(os.environ["XDG_RUNTIME_DIR"], "hummin-assistant.sock")


def encode(msg):
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line):
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None
