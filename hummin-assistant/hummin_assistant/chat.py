"""Maps hummin RPC events to UI events for one conversation."""

HEADLINE_MAX = 80
DIALOGS = ("select", "input", "editor")


def headlines(recall_text):
    out = []
    for line in recall_text.splitlines():
        if line.startswith("Problem:"):
            h = line[len("Problem:"):].strip()
            out.append(h if len(h) <= HEADLINE_MAX else h[:HEADLINE_MAX - 1] + "…")
    return out


def _text(message):
    return "".join(p.get("text", "") for p in message.get("content") or [] if p.get("type") == "text")


class Conversation:
    def __init__(self, rpc, emit):
        self.rpc, self.emit = rpc, emit
        self.busy = False
        self.pending_confirm = None
        self.last_answer = ""
        self.failed = None  # an error held until Hummin says whether it recovers (retry or compaction)

    async def new(self):
        await self.rpc.request({"type": "new_session"})

    async def ask(self, text, images):
        cmd = {"type": "prompt", "message": text, "images": images}
        was = self.busy
        if was:
            cmd["streamingBehavior"] = "followUp"
        self.busy = True  # an answer is on its way: summon() must not start a fresh session under it
        try:
            await self.rpc.request(cmd)
        except BaseException:
            self.busy = was
            raise

    async def answer(self, id, allow):
        self.pending_confirm = None
        await self.rpc.respond_ui(id, confirmed=allow)

    async def session_file(self):
        return (await self.rpc.request({"type": "get_state"}))["sessionFile"]

    async def handle(self, ev):
        t = ev.get("type")
        if self.failed is not None:
            # Hummin says so explicitly: agent_end/compaction_end carry willRetry (agent-session.ts:1162, 232)
            if (t in ("agent_end", "compaction_end") and ev.get("willRetry")) or t == "auto_retry_start":
                self.failed = None  # it recovers by itself; the retry status line takes over
            elif t in ("agent_start", "auto_retry_end", "agent_settled"):  # final: a follow-up, give-up or the end
                failed, self.failed = self.failed, None
                await self.emit({"ev": "error", "text": failed})
        if t in ("agent_start", "agent_settled"):
            self.busy = t == "agent_start"
            if not self.busy:
                self.pending_confirm = None
            await self.emit({"ev": "state", "busy": self.busy})
        elif t == "message_update":
            ame = ev.get("assistantMessageEvent") or {}
            if ame.get("type") == "text_delta":
                await self.emit({"ev": "delta", "text": ame["delta"]})
        elif t == "message_end":
            await self._message_end(ev.get("message") or {})
        elif t == "auto_retry_start":  # Hummin retries rate limits and overloads by itself
            await self.emit({"ev": "retry", "attempt": ev.get("attempt"), "max": ev.get("maxAttempts"),
                             "text": ev.get("errorMessage") or ""})
        elif t == "auto_retry_end":  # an announced retry that never ran was cancelled by the user: no error
            await self.emit({"ev": "retry", "done": True})
        elif t in ("tool_execution_start", "tool_execution_end"):
            await self.emit({"ev": "tool", "name": ev.get("toolName"),
                             "phase": "start" if t.endswith("start") else "end"})
        elif t == "extension_ui_request":
            method = ev.get("method")
            if method == "confirm":
                self.pending_confirm = ev["id"]
                await self.emit({"ev": "confirm", "id": ev["id"], "title": ev.get("title"),
                                 "message": ev.get("message")})
            elif method in DIALOGS:
                await self.rpc.respond_ui(ev["id"], cancelled=True)

    async def _message_end(self, msg):
        role = msg.get("role")
        if role == "assistant":
            if msg.get("stopReason") == "error":
                self.failed = msg.get("errorMessage") or ""
            else:
                self.failed = None  # an answer got through after all
                self.last_answer = _text(msg)
                await self.emit({"ev": "done", "text": self.last_answer})
        elif role == "custom" and msg.get("customType") == "hummin-memory-recall":
            await self.emit({"ev": "memories", "items": headlines(_text(msg))})
