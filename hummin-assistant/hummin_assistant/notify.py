import asyncio


async def ask(title: str, message: str) -> bool:
    """Desktop Allow/Deny prompt; True only on Allow."""
    try:
        p = await asyncio.create_subprocess_exec(
            # critical: serpantinum's Do Not Disturb hides everything else, and the step waits on this answer
            "notify-send", "--app-name=Hummin", "--wait", "--urgency=critical",
            # Deny first: a click on the body invokes the first action (serpantinum Notification.qml:333-342)
            "-A", "deny=Deny", "-A", "allow=Allow", "--", title, message,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return False
    try:
        out, _ = await p.communicate()
    except asyncio.CancelledError:  # caller timed out or was cancelled
        try:
            p.kill()
        except ProcessLookupError:
            pass
        await p.wait()
        raise
    return p.returncode == 0 and out.decode().strip() == "allow"
