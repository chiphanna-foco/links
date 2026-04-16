"""HTML stripping — port of the Apps Script stripHtml helper."""
import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    if not s:
        return ""
    text = _TAG_RE.sub(" ", s)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()
