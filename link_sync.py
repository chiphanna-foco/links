"""Core orchestration — mirrors the Google Apps Script `Front Link Sync` flow.

For each Front conversation tagged LINKED_TAG_ID, extract Front links referenced
in its comments, check each linked conversation for new activity since we last
looked, ask Claude to summarize, and upsert a single pinned `🔗 Linked
Conversation Updates` master comment on the parent.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import LINKED_TAG_ID
from database import (
    get_last_checked,
    record_sync_run,
    upsert_last_checked,
)
from anthropic_client import AnthropicClient
from front import FrontClient
from html_utils import strip_html

logger = logging.getLogger(__name__)

LINK_RE = re.compile(r"https://app\.frontapp\.com/open/(cnv_[a-zA-Z0-9]+)")
COMMENT_PREFIX = "🔗 **Linked Conversation Update**"
MASTER_PREFIX = "🔗 **Linked Conversation Updates**"
SECTION_HEADING_RE = re.compile(
    r"^### \[(?P<heading>.+?)\]\(https://app\.frontapp\.com/open/(?P<cnv_id>cnv_[a-zA-Z0-9]+)\)\s*$"
)
FORCE_LOOKBACK = timedelta(hours=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _epoch_to_dt(ts) -> Optional[datetime]:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _author_name(obj: dict) -> str:
    author = obj.get("author") or {}
    return (
        author.get("name")
        or author.get("email")
        or author.get("username")
        or "Unknown"
    )


def extract_links_from_comments(comments: list[dict], self_id: str) -> list[dict]:
    """Collect unique linked convo IDs from comment bodies, excluding self.

    For each unique linked ID, keep the earliest comment timestamp as
    `link_added_at` — this is used as the cold-start "since" time, matching
    the Apps Script's behavior of only reporting activity AFTER the link was
    added.
    """
    found: dict[str, datetime] = {}
    for c in comments:
        body = c.get("body") or ""
        posted = _epoch_to_dt(c.get("posted_at"))
        for m in LINK_RE.finditer(body):
            cnv = m.group(1)
            if cnv == self_id:
                continue
            existing = found.get(cnv)
            if posted and (existing is None or posted < existing):
                found[cnv] = posted
    return [
        {"conversation_id": cnv, "link_added_at": ts}
        for cnv, ts in found.items()
    ]


def get_activity_since(
    front: FrontClient, convo_id: str, since_dt: datetime
) -> list[dict]:
    """Return message+comment updates with created_at > since_dt.

    Skips comments that look like our own summaries (legacy per-update comments
    or the new master comment) so we never re-summarize ourselves.
    """
    updates: list[dict] = []

    for m in front.get_messages(convo_id):
        created = _epoch_to_dt(m.get("created_at"))
        if not created or created <= since_dt:
            continue
        body = m.get("body") or m.get("text") or ""
        updates.append(
            {
                "type": "message",
                "timestamp": created,
                "author": _author_name(m),
                "subject": m.get("subject"),
                "body": strip_html(body)[:2000],
                "is_inbound": m.get("is_inbound"),
            }
        )

    for c in front.get_comments(convo_id):
        body = c.get("body") or ""
        if body.startswith(COMMENT_PREFIX) or body.startswith(MASTER_PREFIX):
            continue
        posted = _epoch_to_dt(c.get("posted_at"))
        if not posted or posted <= since_dt:
            continue
        updates.append(
            {
                "type": "comment",
                "timestamp": posted,
                "author": _author_name(c),
                "body": body[:2000],
            }
        )

    updates.sort(key=lambda u: u["timestamp"])
    return updates


# ---------------------------------------------------------------------------
# Master comment find / parse / serialize
# ---------------------------------------------------------------------------

def _find_master_comment(comments: list[dict]) -> Optional[dict]:
    """Return the pinned master comment if present.

    If multiple match, log a warning and return the oldest (smallest posted_at)
    so subsequent runs converge on a single canonical comment.
    """
    matches = [
        c for c in comments
        if c.get("is_pinned") is True
        and (c.get("body") or "").startswith(MASTER_PREFIX)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Found %d pinned master comments; using oldest", len(matches)
        )
        matches.sort(key=lambda c: c.get("posted_at") or 0)
    return matches[0]


def _parse_master_body(body: str) -> list[dict]:
    """Parse a master-comment body into ordered sections.

    Each section: {cnv_id, heading, bullets: [str, ...]}. Lines that don't fit
    the heading/bullet shape are preserved as `extra` on the current section
    (or dropped if before any heading) so manual edits inside a section round-trip.
    """
    sections: list[dict] = []
    current: Optional[dict] = None
    for raw in body.splitlines():
        line = raw.rstrip()
        m = SECTION_HEADING_RE.match(line)
        if m:
            current = {
                "cnv_id": m.group("cnv_id"),
                "heading": m.group("heading"),
                "bullets": [],
            }
            sections.append(current)
            continue
        if current is None:
            continue
        stripped = line.lstrip()
        if stripped.startswith("- "):
            current["bullets"].append(stripped[2:])
    return sections


def _serialize_master_body(sections: list[dict]) -> str:
    parts: list[str] = [MASTER_PREFIX, ""]
    for i, s in enumerate(sections):
        url = f"https://app.frontapp.com/open/{s['cnv_id']}"
        parts.append(f"### [{s['heading']}]({url})")
        for bullet in s["bullets"]:
            parts.append(f"- {bullet}")
        if i != len(sections) - 1:
            parts.append("")
    return "\n".join(parts)


def _upsert_section(
    sections: list[dict], cnv_id: str, heading_text: str, dated_bullets: list[str]
) -> list[dict]:
    """Append `dated_bullets` to the section for cnv_id (creating it if needed).

    Also refreshes the heading text on existing sections so subject renames
    flow through. Mutates and returns `sections`.
    """
    for s in sections:
        if s["cnv_id"] == cnv_id:
            s["heading"] = heading_text
            s["bullets"].extend(dated_bullets)
            return sections
    sections.append(
        {"cnv_id": cnv_id, "heading": heading_text, "bullets": list(dated_bullets)}
    )
    return sections


def _replace_section(
    sections: list[dict], cnv_id: str, heading_text: str, dated_bullets: list[str]
) -> list[dict]:
    """Replace the section's bullets entirely (for backfill). Creates if missing."""
    for s in sections:
        if s["cnv_id"] == cnv_id:
            s["heading"] = heading_text
            s["bullets"] = list(dated_bullets)
            return sections
    sections.append(
        {"cnv_id": cnv_id, "heading": heading_text, "bullets": list(dated_bullets)}
    )
    return sections


def _format_dated_bullet(text: str, date_str: str) -> str:
    return f"{date_str} — {text}"


def _parse_md(s: str) -> Optional[tuple[int, int]]:
    """Parse 'M/D', 'MM/DD', or 'M/D/YY' into (month, day). Returns None on bad input."""
    if not s:
        return None
    try:
        parts = s.strip().split("/")
        if len(parts) < 2:
            return None
        m = int(parts[0])
        d = int(parts[1])
        if 1 <= m <= 12 and 1 <= d <= 31:
            return (m, d)
    except (ValueError, AttributeError):
        return None
    return None


def _build_dated_bullets(
    bullets: list[dict], activity: list[dict]
) -> tuple[list[str], list[dict]]:
    """Convert Claude's [{date,text}, ...] into dated-bullet strings.

    - Drops bullets whose date doesn't match any activity (month, day).
    - Preserves Claude's order (which the prompt instructs to be chronological).
    Returns (kept, dropped) where dropped is for diagnostics.
    """
    valid_md = {
        (a["timestamp"].month, a["timestamp"].day)
        for a in activity
        if a.get("timestamp")
    }
    kept: list[str] = []
    dropped: list[dict] = []
    for b in bullets:
        date = (b.get("date") or "").strip()
        text = (b.get("text") or "").strip()
        if not date or not text:
            dropped.append({"reason": "empty", "bullet": b})
            continue
        md = _parse_md(date)
        if md is None or md not in valid_md:
            dropped.append({"reason": "bad_date", "bullet": b, "valid_dates": sorted(f"{m}/{d}" for m, d in valid_md)})
            logger.warning("Dropping bullet with invalid date %r: %r", date, text)
            continue
        # Normalize to "M/D" (no zero-padding) regardless of how Claude formatted it.
        canonical = f"{md[0]}/{md[1]}"
        kept.append(_format_dated_bullet(text, canonical))
    return kept, dropped


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------

def process_conversation(
    front: FrontClient,
    claude: AnthropicClient,
    parent: dict,
    force: bool = False,
    backfill: bool = False,
    debug: bool = False,
) -> tuple[int, int, list[dict]]:
    """Process a single parent conversation. Returns (links_checked, bullets_added, debug_info).

    backfill=True: ignore cache, look back to link_added_at, REPLACE each
    section's bullets with a freshly-generated set covering full history.
    debug=True: collect per-link diagnostic info (always-empty when False).
    """
    parent_id = parent["id"]
    logger.info(
        "Processing conversation %s (force=%s, backfill=%s)",
        parent_id, force, backfill,
    )

    comments = front.get_comments(parent_id)
    links = extract_links_from_comments(comments, self_id=parent_id)
    debug_info: list[dict] = []
    if not links:
        logger.info("No Front links found in comments for %s", parent_id)
        return 0, 0, debug_info

    logger.info("Found %d linked conversation(s) for %s", len(links), parent_id)

    master = _find_master_comment(comments)
    if master:
        try:
            sections = _parse_master_body(master.get("body") or "")
        except Exception:
            logger.exception(
                "Failed to parse existing master comment %s; appending without merge",
                master.get("id"),
            )
            sections = []
    else:
        sections = []

    links_checked = 0
    bullets_added = 0
    pending_cache_updates: list[tuple[str, datetime, Optional[datetime]]] = []
    now = datetime.now(tz=timezone.utc)

    for link in links:
        linked_id = link["conversation_id"]
        link_added_at = link["link_added_at"] or datetime.fromtimestamp(0, tz=timezone.utc)
        last_checked = get_last_checked(parent_id, linked_id)

        if backfill:
            since = link_added_at
            logger.info("BACKFILL %s since %s", linked_id, since.isoformat())
        else:
            since = link_added_at
            if last_checked and last_checked > since:
                since = last_checked
            if force:
                two_h_ago = now - FORCE_LOOKBACK
                if since < two_h_ago:
                    since = two_h_ago
                logger.info("FORCE mode — checking %s since %s", linked_id, since.isoformat())
            else:
                logger.info("Checking %s for activity since %s", linked_id, since.isoformat())

        links_checked += 1

        try:
            activity = get_activity_since(front, linked_id, since)
        except Exception:
            logger.exception("Failed to fetch activity for %s", linked_id)
            continue

        if not activity:
            logger.info("No new activity in %s", linked_id)
            pending_cache_updates.append((linked_id, now, None))
            continue

        try:
            linked_convo = front.get_conversation(linked_id)
            subject = linked_convo.get("subject") or "Linked conversation"
        except Exception:
            logger.exception("Could not fetch subject for %s", linked_id)
            subject = "Linked conversation"

        existing_section = next(
            (s for s in sections if s["cnv_id"] == linked_id), None
        )
        # In backfill we regenerate from scratch, so don't bias Claude with
        # bullets it would only re-derive.
        previous_bullets = (
            [] if backfill
            else (list(existing_section["bullets"]) if existing_section else [])
        )

        update_payload = [
            {
                "conversation_id": linked_id,
                "conversation_subject": subject,
                "activity": activity,
            }
        ]
        result = claude.analyze_updates(update_payload, previous_bullets=previous_bullets)

        raw_bullets = result.get("bullets") or []
        dated, dropped = _build_dated_bullets(raw_bullets, activity)
        latest = max(a["timestamp"] for a in activity)

        if debug:
            debug_info.append({
                "linked_id": linked_id,
                "subject": subject,
                "since": since.isoformat(),
                "activity_count": len(activity),
                "shouldPost": result.get("shouldPost"),
                "reasoning": result.get("reasoning"),
                "raw_bullets": raw_bullets,
                "kept_bullets": dated,
                "dropped_bullets": dropped,
            })

        if result.get("shouldPost") and dated:
            if backfill:
                _replace_section(sections, linked_id, subject, dated)
            else:
                _upsert_section(sections, linked_id, subject, dated)
            bullets_added += len(dated)
            pending_cache_updates.append((linked_id, latest, now))
        else:
            logger.info("AI decided not to post for %s (bullets=%d)", linked_id, len(dated))
            pending_cache_updates.append((linked_id, now, None))

    if bullets_added > 0:
        new_body = _serialize_master_body(sections)
        try:
            if master:
                front.patch_comment(master["id"], new_body)
            else:
                front.post_comment(parent_id, new_body, is_pinned=True)
        except Exception:
            logger.exception("Failed to upsert master comment on %s", parent_id)
            # Don't advance any cache rows for pairs we just appended bullets
            # for — they'll retry next run.
            advanced_ids = {
                pid for (pid, _, posted) in pending_cache_updates if posted is not None
            }
            for linked_id, checked_at, posted_at in pending_cache_updates:
                if linked_id in advanced_ids:
                    continue
                upsert_last_checked(parent_id, linked_id, checked_at, last_posted_at=posted_at)
            return links_checked, 0, debug_info

    for linked_id, checked_at, posted_at in pending_cache_updates:
        upsert_last_checked(parent_id, linked_id, checked_at, last_posted_at=posted_at)

    return links_checked, bullets_added, debug_info


def check_linked_conversations(force: bool = False) -> dict:
    """Top-level sweep — called by the scheduler and /api/check."""
    started = time.monotonic()
    logger.info("=== Starting link-sync run (force=%s) ===", force)

    if not LINKED_TAG_ID:
        logger.error("LINKED_TAG_ID not configured")
        record_sync_run("error", 0, 0, 0, 0, "LINKED_TAG_ID not configured")
        return {"status": "error", "details": "LINKED_TAG_ID not configured"}

    front = FrontClient()
    claude = AnthropicClient()

    try:
        parents = front.get_conversations_by_tag(LINKED_TAG_ID)
    except Exception as exc:
        logger.exception("Failed to fetch tagged conversations")
        record_sync_run(
            "error", 0, 0, 0,
            int((time.monotonic() - started) * 1000),
            str(exc),
        )
        return {"status": "error", "details": str(exc)}

    logger.info("Found %d conversation(s) with Linked tag", len(parents))

    total_links = 0
    total_posts = 0
    for parent in parents:
        try:
            links, posts, _ = process_conversation(front, claude, parent, force=force)
            total_links += links
            total_posts += posts
        except Exception:
            logger.exception("Failed processing parent %s", parent.get("id"))

    duration_ms = int((time.monotonic() - started) * 1000)
    record_sync_run("success", len(parents), total_links, total_posts, duration_ms)
    logger.info(
        "=== Run complete: %d parents, %d links, %d posts in %d ms ===",
        len(parents), total_links, total_posts, duration_ms,
    )
    return {
        "status": "ok",
        "parents_checked": len(parents),
        "links_checked": total_links,
        "comments_posted": total_posts,
        "duration_ms": duration_ms,
    }


def process_single_conversation(
    conversation_id: str,
    force: bool = True,
    backfill: bool = False,
    debug: bool = False,
) -> dict:
    """Test a single parent conversation (replaces Apps Script `testConversation`)."""
    front = FrontClient()
    claude = AnthropicClient()
    started = time.monotonic()

    try:
        parent = front.get_conversation(conversation_id)
    except Exception as exc:
        logger.exception("Could not fetch conversation %s", conversation_id)
        return {"status": "error", "details": str(exc)}

    try:
        links, posts, dbg = process_conversation(
            front, claude, parent, force=force, backfill=backfill, debug=debug
        )
    except Exception as exc:
        logger.exception("Failed processing %s", conversation_id)
        return {"status": "error", "details": str(exc)}

    duration_ms = int((time.monotonic() - started) * 1000)
    out = {
        "status": "ok",
        "conversation_id": conversation_id,
        "links_checked": links,
        "bullets_added": posts,
        "backfill": backfill,
        "duration_ms": duration_ms,
    }
    if debug:
        out["debug"] = dbg
    return out
