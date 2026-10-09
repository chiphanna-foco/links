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


# --- Quoted reply history -------------------------------------------------
# Replies carry the whole earlier thread underneath the new text. Every one of
# those earlier messages is already summarized (or is its own message in the
# conversation), so re-sending it is pure token waste. Cuts are deliberately
# conservative: only well-known client markers, and never when nothing would be
# left (a forward, or a message that is only quoted text, is kept whole).

_HTML_QUOTE_RE = re.compile(
    r"<blockquote\b"
    r"|<[^>]*\b(?:class|id)\s*=\s*[\"'][^\"']*"
    r"(?:gmail_quote|gmail_attr|yahoo_quoted|moz-cite-prefix|divRplyFwdMsg"
    r"|appendonsend|OutlookMessageHeader)",
    re.IGNORECASE,
)
_TAG_PRESENT_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# "On Tue, Oct 7, 2025 at 3:14 PM Name wrote:" / "On 10/7/25, Name wrote:"
_ON_WROTE_RE = re.compile(
    r"\bOn (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|\d{1,2}[/.-]|[A-Z][a-z]{2,8}\.? \d)"
    r".{0,200}?\bwrote:"
)
_ORIGINAL_MSG_RE = re.compile(r"-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE)
# Outlook plain header block, flattened to one line by strip_html.
_OUTLOOK_HDR_RE = re.compile(r"\bFrom: .{1,150}? Sent: .{1,100}? To: ")


def _cut_at_first(text: str, patterns) -> str:
    """Cut `text` at the earliest pattern match, if some real text precedes it."""
    cut = None
    for pat in patterns:
        m = pat.search(text)
        if m and m.start() > 0 and (cut is None or m.start() < cut):
            cut = m.start()
    if cut is None:
        return text
    head = text[:cut]
    return head.rstrip() if head.strip() else text


def strip_quoted_html(s: str) -> str:
    """strip_html() minus the quoted earlier-thread text, when it is clearly marked."""
    if not s:
        return ""
    s = _cut_at_first(s, [_HTML_QUOTE_RE])
    if not _TAG_PRESENT_RE.search(s):
        # Plain-text body: newlines are still intact, so ">" quoting is usable.
        kept = [ln for ln in s.splitlines() if not ln.lstrip().startswith(">")]
        s = "\n".join(kept) if any(ln.strip() for ln in kept) else s
    text = strip_html(s)
    return _cut_at_first(text, [_ON_WROTE_RE, _ORIGINAL_MSG_RE, _OUTLOOK_HDR_RE])
