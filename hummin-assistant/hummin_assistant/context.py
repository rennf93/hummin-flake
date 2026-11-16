import asyncio
import base64


async def _run(*cmd, timeout):
    """Return stdout bytes of cmd, or None on any failure."""
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        await p.wait()
        if isinstance(e, asyncio.CancelledError):
            raise
        return None
    return out if p.returncode == 0 else None


async def selection() -> str:
    for args in (("--primary", "--no-newline"), ("--no-newline",)):
        out = await _run("wl-paste", *args, timeout=2)
        if out is not None:
            return out.decode(errors="replace").strip()[:4000]
    return ""


async def screenshot():
    out = await _run("grim", "-", timeout=10)
    if out is None:
        return None
    return {"type": "image", "data": base64.b64encode(out).decode(), "mimeType": "image/png"}
