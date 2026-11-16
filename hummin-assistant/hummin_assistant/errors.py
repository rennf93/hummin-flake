"""Provider errors ("402: {json}", "529 overloaded") as one plain sentence for the popup."""
import json
import re

_STATUS = re.compile(r"^\s*([1-5]\d\d)(?::|\s)\s*(.*)$", re.S)
_NAMES = {"openrouter": "OpenRouter", "zai": "Z.ai", "zai-api": "Z.ai", "anthropic": "Anthropic", "openai": "OpenAI"}
_OFFLINE = ("fetch failed", "econnrefused", "enotfound", "etimedout", "econnreset", "network")
HINT = "Pick another model in the menu, or try again later."
MAX = 300


def _detail(body):
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return body
    err = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), dict) else data
    return str(err.get("message") or body) if isinstance(err, dict) else body


def _clip(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def friendly(error, model="", brief=False):
    """brief: the headline only, for the retry status line."""
    name = _NAMES.get(str(model or "").split("/", 1)[0]) or str(model or "").split("/", 1)[0] or "The model provider"
    error = (error if isinstance(error, str) else "" if error is None else str(error)).strip()

    def say(headline, detail="", hint=True):
        if brief:
            return _clip(headline, MAX)
        return _clip(" ".join([headline] + ([HINT] if hint else []) + ([f"({detail})"] if detail else [])), MAX)

    m = _STATUS.match(error)
    if not m:
        if any(k in error.lower() for k in _OFFLINE):
            return say(f"Can't reach {name}. Check the connection, or pick another model.", hint=False)
        return _clip(error, MAX) if error else "Something went wrong."
    code, detail = int(m.group(1)), _clip(_detail(m.group(2).strip()).strip(), 160)
    low = detail.lower()
    if code == 402 or "balance" in low or "credit" in low:  # Z.ai reports an empty balance as 429
        return say(f"{name} is out of credit for this request.", detail)
    if code == 429:
        return say(f"{name} is rate-limiting requests.", detail)
    if code in (401, 403):
        return say(f"{name} rejected the API key or login.", detail, hint=False)
    if code >= 500:
        return say(f"{name} is having trouble right now ({code}). Try again shortly.", hint=False)
    return _clip(f"{name} error {code}." if brief else f"{name} error {code}: {detail}", MAX)
