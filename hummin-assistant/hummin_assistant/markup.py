"""Markdown -> the Qt StyledText subset (b, i, tt, pre, br, a). Escapes first, so nothing from the model is live markup."""
import html
import re

_FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.S)
_CODE = re.compile(r"`([^`\n]+)`")
# spans are capped so a huge reply cannot go quadratic and stall the daemon
_SPAN = 300
_URL = r"((?:[^()\s]|\([^()\s]*\)){0,2000})"  # one level of nested parens
_IMAGE = re.compile(rf"!\[([^\]\n]{{0,{_SPAN}}})\]\(" + _URL + r"\)")
_LINK = re.compile(rf"\[([^\]\n]{{1,{_SPAN}}})\]\(" + _URL + r"\)")
_BOLD = re.compile(rf"\*\*(?=\S)(.{{1,{_SPAN}}}?)(?<=\S)\*\*|__(?=\S)(.{{1,{_SPAN}}}?)(?<=\S)__")
_ITALIC = re.compile(rf"(?<![\w*])\*(?=[^\s*])(.{{1,{_SPAN}}}?)(?<=[^\s*])\*(?![\w*])|(?<![\w])_(?=[^\s_])(.{{1,{_SPAN}}}?)(?<=[^\s_])_(?!\w)")
_HEADING = re.compile(r"^#{1,3} +(.*)$", re.M)
_BULLET = re.compile(r"^([ \t]*)[-*] +", re.M)
_QUOTE = re.compile(r"^[ \t]*&gt; ?(.*)$", re.M)  # runs after escaping, so the > is already &gt;


def render(md: str) -> str:
    stash = []

    def unstash(s):
        return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], s)

    def keep(markup):  # protect finished markup from the later passes
        stash.append(unstash(markup))  # link text can hold an earlier placeholder (code in a link)
        return f"\x00{len(stash) - 1}\x00"

    def link(m):
        url = m.group(2)  # a placeholder in the url would put markup inside href
        return keep(f'<a href="{url}">{m.group(1)}</a>') if re.fullmatch(r'https?://[^\s\x00"]+', url) else m.group(1)

    s = html.escape(md.replace("\x00", ""), quote=False)  # StyledText may not know &#x27;
    s = _FENCE.sub(lambda m: keep(f"<pre>{m.group(1).rstrip(chr(10))}</pre>"), s)
    s = _CODE.sub(lambda m: keep(f"<tt>{m.group(1)}</tt>"), s)
    s = _IMAGE.sub(lambda m: m.group(1), s)
    s = _LINK.sub(link, s)
    s = _HEADING.sub(r"<b>\1</b>", s)
    s = _QUOTE.sub("<i>\u2502 \\1</i>", s)  # StyledText has no blockquote
    s = _BULLET.sub(lambda m: m.group(1) + "• ", s)
    s = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", s)
    s = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", s)
    s = s.replace("\n", "<br>")
    return unstash(s)
