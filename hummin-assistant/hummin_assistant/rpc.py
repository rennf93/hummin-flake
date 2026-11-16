"""JSONL client for `hummin --mode rpc` over the child's stdin/stdout."""
import asyncio
import re

from . import protocol

_ERROR_LINE = re.compile(r"^\s*\w*Error\b")


def exit_reason(stderr_tail):
    """The last 'Error: ...' line Node printed before dying, or ''."""
    for line in reversed(stderr_tail.decode(errors="replace").splitlines()):
        if _ERROR_LINE.match(line):
            line = line.strip()
            return line if len(line) <= 200 else line[:199] + "…"
    return ""


class RpcError(Exception):
    pass


class HumminRpc:
    def __init__(self, argv, env, cwd):
        self.argv, self.env, self.cwd = argv, env, cwd
        self.events = asyncio.Queue()
        self._pending = {}
        self._n = 0
        self._proc = None
        self._tasks = []
        self._stderr = b""

    @property
    def alive(self):
        return self._proc is not None and self._proc.returncode is None

    async def start(self):
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv, env=self.env, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=1 << 24)  # screenshots-sized lines
        self._stderr = b""
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._drain())]

    async def _drain(self):
        while chunk := await self._proc.stderr.read(65536):
            self._stderr = (self._stderr + chunk)[-8192:]  # kept to say why it died

    async def _exited(self):
        drain = self._tasks[1] if len(self._tasks) > 1 else None
        if drain is not None and not drain.done():
            try:  # stdout can close before the last stderr bytes are read
                await asyncio.wait_for(asyncio.shield(drain), 1)
            except Exception:  # timed out or the drain died: the reason is just less precise
                pass
        why = exit_reason(self._stderr)
        return RpcError(f"Hummin stopped: {why}" if why else "hummin exited")

    async def _read(self):
        try:
            async for line in self._proc.stdout:
                msg = protocol.decode(line)
                if msg is None:
                    continue
                if msg.get("type") == "response":
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                else:
                    await self.events.put(msg)
        finally:  # child exited (or reader died): fail waiters, tell the daemon
            err = RpcError("hummin exited")
            try:
                if self.alive:  # the reader died (a line over the limit): a live child would never be restarted
                    self._proc.kill()
                    await self._proc.wait()
                err = await self._exited()
            finally:  # even when stop() cancels us mid-wait
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(err)
                self.events.put_nowait({"type": "_exit"})

    def _write(self, msg):
        self._proc.stdin.write(protocol.encode(msg))
        return self._proc.stdin.drain()

    async def request(self, cmd, timeout=30):
        self._n += 1
        rid = f"r{self._n}"
        fut = self._pending[rid] = asyncio.get_running_loop().create_future()
        try:
            await self._write({**cmd, "id": rid})
            resp = await asyncio.wait_for(fut, timeout)
        except (BrokenPipeError, ConnectionResetError) as e:
            raise await self._exited() from e
        except asyncio.TimeoutError:
            raise RpcError("timeout") from None
        finally:
            self._pending.pop(rid, None)
        if not resp.get("success"):
            raise RpcError(resp.get("error"))
        return resp.get("data")

    async def respond_ui(self, id, **fields):
        try:
            await self._write({"type": "extension_ui_response", "id": id, **fields})
        except (BrokenPipeError, ConnectionResetError) as e:
            raise RpcError("hummin exited") from e

    async def stop(self):
        if self.alive:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 3)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        for t in self._tasks:
            t.cancel()
