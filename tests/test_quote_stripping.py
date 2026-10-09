"""Plain-python tests for quoted-reply stripping. Run: python3 tests/test_quote_stripping.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from html_utils import strip_quoted_html  # noqa: E402


def test_gmail_html():
    html = ('<div>Approved, go ahead Monday.</div><br><div class="gmail_quote">'
            '<div class="gmail_attr">On Tue, Oct 7, 2025 at 3:14 PM Vendor wrote:</div>'
            '<blockquote>Can we schedule for 4/20?</blockquote></div>')
    assert strip_quoted_html(html) == "Approved, go ahead Monday."


def test_blockquote_only_marker():
    assert strip_quoted_html("<p>Thanks!</p><blockquote>old text</blockquote>") == "Thanks!"


def test_plain_text_markers():
    body = "Looks good.\n\nOn Mon, Jan 5, 2026 at 9:00 AM Bob <b@x.com> wrote:\n> earlier\n> text"
    assert strip_quoted_html(body) == "Looks good."
    assert strip_quoted_html("Yes\n> quoted line\nno") == "Yes no"


def test_original_message_and_outlook():
    assert strip_quoted_html("<p>Done</p><p>-----Original Message-----</p><p>From: a</p>") == "Done"
    out = "<p>Sent it</p><p>From: A B Sent: Monday, May 4, 2026 1:00 PM To: C Subject: Re: x</p><p>old</p>"
    assert strip_quoted_html(out) == "Sent it"


def test_keeps_when_nothing_would_remain():
    # A forward / message that is only quoted text must not be emptied.
    assert strip_quoted_html("<blockquote>only quoted</blockquote>") == "only quoted"
    assert strip_quoted_html("On Mon, Jan 5 Bob wrote: hi") == "On Mon, Jan 5 Bob wrote: hi"


def test_no_false_positive_on_prose():
    t = "On Monday we will see what the owner wrote: nothing yet."
    assert strip_quoted_html(t) == t
    assert strip_quoted_html("") == ""


if __name__ == "__main__":
    fail = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fail += 1
                print("FAIL", name, e)
    sys.exit(1 if fail else 0)
