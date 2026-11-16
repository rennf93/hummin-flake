#!/usr/bin/env python3
"""Fake `hummin --mode rpc`: JSONL on stdin, JSONL on stdout."""
import json
import os
import sys
import time

RATE = '429: {"error":{"message":"slow down"}}'

MODELS = [
    {"provider": "openrouter", "id": "z-ai/glm-5.3-flash", "name": "Z.ai: GLM 5.3 Flash", "input": ["text", "image"]},
    {"provider": "openrouter", "id": "other/big-model", "name": "Big", "input": ["text"]},
    {"provider": "llamacpp-nas-9991", "id": "north-mini-code", "name": "North Mini Code", "input": ["text"]},
    {"provider": "zai", "id": "glm-4.7", "name": "GLM 4.7", "input": ["text"]},
]


def failed_attempt(error, will_retry=False):  # agent-loop.ts: an error ends the turn and the agent loop
    out(type="message_end", message={"role": "assistant", "stopReason": "error", "errorMessage": error})
    out(type="turn_end")
    out(type="agent_end", willRetry=will_retry)  # agent-session.ts adds willRetry


def out(**msg):
    print(json.dumps(msg), flush=True)


def reply(req, data=None):
    out(id=req["id"], type="response", command=req["type"], success=True, data=data or {})


def assistant(text):
    out(type="message_end", message={"role": "assistant", "stopReason": "stop",
                                     "content": [{"type": "text", "text": text}]})


if os.environ.get("FAKE_CRASH"):  # like Node dying in seedBundledExtensions at startup
    sys.stderr.write("node:fs:3100\n  binding.copyFile(\n          ^\n\nError: EACCES: permission denied, copyfile "
                     "'/nix/store/x/hummin-todos.ts' -> '/home/u/.hummin/agent/extensions/hummin-todos.ts'\n"
                     "    at copyFileSync (node:fs:3100:11)\n\nNode.js v22.23.4\n")
    sys.exit(1)

for line in sys.stdin:
    req = json.loads(line)
    kind = req.get("type")
    if os.environ.get("FAKE_LOG"):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(kind + "\n")
    if kind == "abort":
        reply(req)
    elif kind == "new_session":
        if os.environ.get("FAKE_SLOW_NEW") == "1":
            time.sleep(1)
        reply(req, {"cancelled": False})
    elif kind == "set_model":
        if os.environ.get("FAKE_LOG"):
            with open(os.environ["FAKE_LOG"], "a") as f:
                f.write("set_model %s %s\n" % (req.get("provider"), req.get("modelId")))
        reply(req)
    elif kind == "get_available_models":
        reply(req, {"models": MODELS})
    elif kind == "get_state":
        reply(req, {"sessionFile": "/tmp/fake.jsonl", "sessionId": "fake"})
    elif kind == "prompt":
        msg = req["message"]
        reply(req, {"disposition": "started"})
        out(type="agent_start")
        if msg == "hang":
            continue  # never settles
        if "remember" in msg:
            out(type="message_end", message={"role": "custom", "customType": "hummin-memory-recall", "content": [
                {"type": "text", "text": "Project memory (1 recent lesson(s) for this project):\n\n"
                                         "Problem: menubar lost tray\nApproach: re-register"}]})
        if "delete" in msg:
            out(type="extension_ui_request", id="c1", method="confirm", title="Delete?", message="Really?")
            resp = json.loads(sys.stdin.readline())
            if os.environ.get("FAKE_LOG"):
                with open(os.environ["FAKE_LOG"], "a") as f:
                    f.write("ui_response\n")
            assistant("deleted" if resp.get("confirmed") else "denied")
        elif "fail" in msg:
            assistant("Let me check.")
            out(type="message_end", message={"role": "assistant", "stopReason": "error",
                                             "errorMessage": '402: {"message":"need more credits"}'})
        elif "give up" in msg:  # like Hummin: each failed attempt ends in an error, then the retry starts
            for attempt in (1, 2, 3):
                failed_attempt(RATE, will_retry=True)
                out(type="auto_retry_start", attempt=attempt, maxAttempts=3, delayMs=10, errorMessage=RATE)
                out(type="agent_start")
            failed_attempt(RATE)
            out(type="auto_retry_end", success=False, attempt=3, finalError=RATE)
        elif "overflow" in msg:  # too long: not retried, but compacted and continued (agent-session.ts ~3146-3240)
            failed_attempt("400: prompt is too long")
            out(type="entry_appended")
            out(type="compaction_start", reason="overflow")
            out(type="compaction_end", reason="overflow", aborted=False, willRetry=True)
            out(type="agent_start")
            assistant("ok")
        elif "cancelled" in msg:  # stop pressed during the retry delay (_finishCancelledRetry)
            failed_attempt(RATE, will_retry=True)
            out(type="auto_retry_start", attempt=1, maxAttempts=3, delayMs=10, errorMessage=RATE)
            out(type="auto_retry_end", success=False, attempt=1, finalError="Retry cancelled")
        elif "queued" in msg:  # a 402 is not retried; Hummin goes straight on to a follow-up queued meanwhile
            failed_attempt('402: {"message":"need more credits"}')
            out(type="agent_start")
            assistant("ok")
        else:
            if "retry" in msg:  # one failed attempt, then Hummin's retry succeeds
                failed_attempt(RATE, will_retry=True)
                out(type="message_start", message={"role": "custom", "customType": "x", "content": []})
                out(type="message_end", message={"role": "custom", "customType": "x", "content": []})
                out(type="extension_ui_request", id="n1", method="notify", message="noise")
                out(type="auto_retry_start", attempt=1, maxAttempts=3, delayMs=10, errorMessage=RATE)
                out(type="agent_start")
            if "tool" in msg:
                assistant("Let me check.")  # the message before a tool call; the run goes on
            for d in ("Hel", "lo"):
                out(type="message_update", assistantMessageEvent={"type": "text_delta", "delta": d})
            assistant("Hello")
            if "retry" in msg:
                out(type="auto_retry_end", success=True, attempt=1)
        out(type="agent_settled", aborted=False)
