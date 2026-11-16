import asyncio
import socket
import sys

from . import protocol

CMDS = {"toggle": lambda a: {"cmd": "toggle"},
        "key": lambda a: {"cmd": "key", "state": a[0]},
        "show": lambda a: {"cmd": "show", "look": a[0]}}
USAGE = "usage: hummin_assistant daemon | toggle | key down|up | show popup|side|dropdown"


def main(argv):
    if not argv or argv[0] not in ("daemon", *CMDS):
        return fail(USAGE)
    try:
        path = protocol.socket_path()
    except KeyError:
        return fail("XDG_RUNTIME_DIR is not set")
    if argv[0] == "daemon":
        from .daemon import Daemon
        try:
            asyncio.run(Daemon(["hummin", "--mode", "rpc"]).serve())
        except KeyboardInterrupt:
            pass
        return 0
    if (argv[0] == "key" and argv[1:2] not in (["down"], ["up"])) or \
            (argv[0] == "show" and argv[1:2] not in (["popup"], ["side"], ["dropdown"])):
        return fail(USAGE)
    with socket.socket(socket.AF_UNIX) as s:
        try:
            s.connect(path)
        except OSError:
            return fail("daemon not running")
        s.sendall(protocol.encode(CMDS[argv[0]](argv[1:])))
    return 0


def fail(msg):
    print("hummin-assistant: " + msg, file=sys.stderr)
    return 1


sys.exit(main(sys.argv[1:]))
