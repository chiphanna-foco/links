"""Core orchestration — mirrors the Google Apps Script `Front Link Sync` flow.

For each Front conversation tagged LINKED_TAG_ID, extract Front links referenced
in its comments, check each linked conversation for new activity since we last
looked, ask Claude to summarize, and post a `🔗 Linked Conversation Update`
comment back to the parent.
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

    Skips comments that start with COMMENT_PREFIX (our own summaries) as a
    defensive measure. Message bodies have HTML stripped.
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
        if body.startswith(COMMENT_PREFIX):
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
# Core flow
# ---------------------------------------------------------------------------

def process_conversation(
    front: FrontClient,
    claude: AnthropicClient,
    parent: dict,
    force: bool = False,
) -> tuple[int, int]:
    """Process a single parent conversation. Returns (links_checked, comments_posted)."""
    parent_id = parent["id"]
    logger.info("Processing conversation %s", parent_id)

    comments = front.get_comments(parent_id)
    links = extract_links_from_comments(comments, self_id=parent_id)
    if not links:
        logger.info("No Front links found in comments for %s", parent_id)
        return 0, 0

    logger.info("Found %d linked conversation(s) for %s", len(links), parent_id)

    links_checked = 0
    comments_posted = 0
    now = datetime.now(tz=timezone.utc)

    for link in links:
        linked_id = link["conversation_id"]
        link_added_at = link["link_added_at"] or datetime.fromtimestamp(0, tz=timezone.utc)
        last_checked = get_last_checked(parent_id, linked_id)

        # Match Apps Script: since = max(last_checked, link_added_at)
        since = link_added_at
        if last_checked and last_checked > since:
            since = last_checked

        if force:
            # Apps Script test-mode: check back 2h, but respect cache if newer
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
            upsert_last_checked(parent_id, linked_id, now)
            continue

        # Fetch subject for the summary prompt
        try:
            linked_convo = front.get_conversation(linked_id)
            subject = linked_convo.get("subject") or "Linked conversation"
        except Exception:
            logger.exception("Could not fetch subject for %s", linked_id)
            subject = "Linked conversation"

        update_payload = [
            {
                "conversation_id": linked_id,
                "conversation_subject": subject,
                "activity": activity,
            }
        ]
        result = claude.analyze_updates(update_payload)

        if result.get("shouldPost") and result.get("message"):
            body = f"{COMMENT_PREFIX}\n\n{result['message']}"
            try:
                front.post_comment(parent_id, body)
                comments_posted += 1
            except Exception:
                logger.exception("Failed posting comment to %s", parent_id)
                # Don't advance cache — retry next run
                continue

            # Advance cache to the latest activity timestamp we reported,
            # matching the Apps Script's updateLastCheckedTimeWithTimestamp.
            latest = max(a["timestamp"] for a in activity)
            upsert_last_checked(parent_id, linked_id, latest, last_posted_at=now)
        else:
            logger.info("AI decided not to post for %s", linked_id)
            upsert_last_checked(parent_id, linked_id, now)

    return links_checked, comments_posted


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
            links, posts = process_conversation(front, claude, parent, force=force)
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


def process_single_conversation(conversation_id: str, force: bool = True) -> dict:
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
        links, posts = process_conversation(front, claude, parent, force=force)
    except Exception as exc:
        logger.exception("Failed processing %s", conversation_id)
        return {"status": "error", "details": str(exc)}

    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "status": "ok",
        "conversation_id": conversation_id,
        "links_checked": links,
        "comments_posted": posts,
        "duration_ms": duration_ms,
    }
