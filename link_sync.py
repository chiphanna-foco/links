"""Core orchestration — mirrors the Google Apps Script `Front Link Sync` flow.

For each Front conversation tagged LINKED_TAG_ID, extract Front links referenced
in its comments, check each linked conversation for new activity since we last
looked, ask Claude to summarize, and upsert a single pinned `🔗 Linked
Conversation Updates` master comment on the parent.
"""
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import (
    COLD_START_LOOKBACK_HOURS,
    PARENT_ACTIVE_WINDOW_DAYS,
    SWEEP_CONCURRENCY,
    EVENTS_ENABLED,
    EVENTS_MAX_CURSOR_AGE_HOURS,
    LINKED_TAG_ID,
)
from database import (
    FAILURE_SURFACE_THRESHOLD,
    cache_is_empty,
    clear_link_failure,
    get_known_links,
    get_persistent_failures,
    get_seen_parents,
    mark_parent_seen,
    get_last_checked,
    get_state_float,
    record_link_failure,
    record_sync_run,
    set_state,
    upsert_last_checked,
)
from anthropic_client import AnthropicClient
from front import EventsTruncated, FrontClient
from html_utils import strip_html

logger = logging.getLogger(__name__)

LINK_RE = re.compile(r"https://app\.frontapp\.com/open/(cnv_[a-zA-Z0-9]+)")
COMMENT_PREFIX = "🔗 **Linked Conversation Update**"
MASTER_PREFIX = "🔗 **Linked Conversation Updates**"
SECTION_HEADING_RE = re.compile(
    r"^### \[(?P<heading>.+?)\]\(https://app\.frontapp\.com/open/(?P<cnv_id>cnv_[a-zA-Z0-9]+)\)\s*$"
)
FORCE_LOOKBACK = timedelta(hours=2)

EVENTS_CURSOR_KEY = "events_cursor"
COLD_START_FLOOR_KEY = "cold_start_floor"

# The only event types that can change what a summary should say: a message in
# or out, or a teammate comment (which is also how a new link gets added).
ACTIVITY_EVENT_TYPES = ["inbound", "outbound", "out_reply", "comment"]


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
    active_ids: Optional[set] = None,
    window_start: Optional[float] = None,
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
        mark_parent_seen(parent_id)
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
    skipped_quiet = 0
    pending_cache_updates: list[tuple[str, datetime, Optional[datetime]]] = []
    now = datetime.now(tz=timezone.utc)

    floor_epoch = get_state_float(COLD_START_FLOOR_KEY)
    cold_start_floor = (
        datetime.fromtimestamp(floor_epoch, tz=timezone.utc) if floor_epoch else None
    )

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
            elif last_checked is None and cold_start_floor and since < cold_start_floor:
                # First time we have ever seen this pair AND we lost our cache.
                # Without this clamp a redeploy re-summarizes every linked
                # conversation from the day its link was added.
                logger.info(
                    "COLD START — clamping %s lookback from %s to %s",
                    linked_id, since.isoformat(), cold_start_floor.isoformat(),
                )
                since = cold_start_floor
            if force:
                two_h_ago = now - FORCE_LOOKBACK
                if since < two_h_ago:
                    since = two_h_ago
                logger.info("FORCE mode — checking %s since %s", linked_id, since.isoformat())
            else:
                logger.info("Checking %s for activity since %s", linked_id, since.isoformat())

        links_checked += 1

        # Front told us nothing happened in this conversation, and our window
        # covers everything we care about, so there is no reason to page its
        # whole message history just to discover that.
        if (
            active_ids is not None
            and window_start is not None
            and linked_id not in active_ids
            and since.timestamp() >= window_start
        ):
            skipped_quiet += 1
            pending_cache_updates.append((linked_id, now, None))
            continue

        try:
            activity = get_activity_since(front, linked_id, since)
        except Exception as exc:
            streak = record_link_failure(parent_id, linked_id, repr(exc))
            if streak >= FAILURE_SURFACE_THRESHOLD:
                logger.error(
                    "PERSISTENT FAILURE: %s has failed %d runs in a row for "
                    "parent %s — %s", linked_id, streak, parent_id, exc,
                )
            else:
                logger.exception("Failed to fetch activity for %s", linked_id)
            continue

        clear_link_failure(parent_id, linked_id)

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

    if skipped_quiet:
        logger.info(
            "Skipped %d quiet link(s) for %s — no events in the window",
            skipped_quiet, parent_id,
        )

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

    mark_parent_seen(parent_id)
    return links_checked, bullets_added, debug_info


def _is_our_own_comment(event: dict) -> bool:
    """True for a `comment` event that is our own master-comment write.

    Verified against the live API 2026-08-10: our posts appear as a `comment`
    event whose `target.data.body` starts with our own MASTER_PREFIX/
    COMMENT_PREFIX, posted by our token's own Front teammate identity. Without
    this, every parent we write a summary to gets re-selected on the very next
    cycle by our own write — the sweep chasing its own tail.
    """
    if event.get("type") != "comment":
        return False
    body = ((event.get("target") or {}).get("data") or {}).get("body") or ""
    return body.startswith(MASTER_PREFIX) or body.startswith(COMMENT_PREFIX)


def _active_conversation_ids(front: FrontClient, since_epoch: float) -> set:
    """Conversation ids Front says actually changed since `since_epoch`."""
    ids = set()
    count = 0
    own_writes = 0
    for ev in front.get_events_since(since_epoch, ACTIVITY_EVENT_TYPES):
        count += 1
        if _is_our_own_comment(ev):
            own_writes += 1
            continue
        convo = ev.get("conversation") or {}
        if convo.get("id"):
            ids.add(convo["id"])
    logger.info(
        "Events feed: %d activity event(s) (%d our own writes, excluded) "
        "touching %d conversation(s) since %s",
        count, own_writes, len(ids),
        datetime.fromtimestamp(since_epoch, tz=timezone.utc).isoformat(),
    )
    return ids


def _select_parents(front: FrontClient, parents: list, force: bool) -> tuple:
    """Narrow the work. Returns (parents, mode, active_ids, window_start).

    `active_ids` is every conversation Front reports as changed since
    `window_start`; both are None when we could not get a trustworthy answer,
    which forces the old exhaustive behaviour.

    Falls back to the full list — loudly — whenever the events feed can't be
    trusted, so a degraded run is visible in the logs rather than silent.
    """
    now = time.time()

    if force:
        return parents, "full (force)", None, None

    if not EVENTS_ENABLED:
        logger.warning("DEGRADED: EVENTS_ENABLED=false — walking every conversation")
        return parents, "full (events disabled)", None, None

    cursor = get_state_float(EVENTS_CURSOR_KEY)
    if cursor is None:
        logger.warning(
            "DEGRADED: no events cursor yet — one full sweep to establish it"
        )
        return parents, "full (no cursor)", None, None

    age_h = (now - cursor) / 3600
    if age_h > EVENTS_MAX_CURSOR_AGE_HOURS:
        logger.warning(
            "DEGRADED: events cursor is %.1fh old (limit %dh) — full sweep instead",
            age_h, EVENTS_MAX_CURSOR_AGE_HOURS,
        )
        return parents, f"full (cursor {age_h:.1f}h old)", None, None

    try:
        active = _active_conversation_ids(front, cursor)
    except EventsTruncated:
        return parents, "full (events truncated)", None, None
    except Exception:
        logger.exception("DEGRADED: events feed unavailable — full sweep instead")
        return parents, "full (events failed)", None, None

    known = get_known_links()
    # Parents with no links never appear in `known`; without `seen` they would
    # be re-fetched on every run forever.
    seen = get_seen_parents() | set(known)
    selected = [
        p for p in parents
        if p.get("id") in active or (known.get(p.get("id"), set()) & active)
    ]
    selected_ids = {p.get("id") for p in selected}

    # A parent we have never cached could hold links we know nothing about, so
    # it needs looking at once — but only if it has been touched recently.
    # A dormant conversation stays skipped until an event wakes it, at which
    # point the branch above picks it up regardless of how old it is.
    cutoff = now - PARENT_ACTIVE_WINDOW_DAYS * 86400
    unseen, dormant = [], 0
    for p in parents:
        if p.get("id") in seen or p.get("id") in selected_ids:
            continue
        touched = p.get("updated_at") or p.get("waiting_since")
        if touched and float(touched) < cutoff:
            dormant += 1
            continue
        unseen.append(p)
    if unseen:
        logger.info(
            "Including %d parent(s) never checked before (within %dd)",
            len(unseen), PARENT_ACTIVE_WINDOW_DAYS,
        )
    if dormant:
        logger.info(
            "Skipping %d dormant parent(s) untouched for over %dd — an event "
            "still wakes any of them",
            dormant, PARENT_ACTIVE_WINDOW_DAYS,
        )
    selected.extend(unseen)

    logger.info(
        "Events fast path: %d of %d parent(s) had activity — skipping %d",
        len(selected), len(parents), len(parents) - len(selected),
    )
    return selected, "incremental", active, cursor


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

    # Claim the cursor BEFORE doing the work: anything that happens during this
    # run must be picked up by the next one, not skipped as already-seen.
    run_started_epoch = time.time()

    if cache_is_empty() and get_state_float(COLD_START_FLOOR_KEY) is None:
        floor = run_started_epoch - COLD_START_LOOKBACK_HOURS * 3600
        set_state(COLD_START_FLOOR_KEY, str(floor))
        # Seed the cursor to the same instant so the very first run can already
        # skip the message history of links that saw no events in the window.
        # Without this a cold start walks all ~17.6k links exhaustively.
        set_state(EVENTS_CURSOR_KEY, str(floor))
        logger.warning(
            "DEGRADED: cold start with an empty cache — clamping first-time "
            "lookback to %dh (from %s) instead of re-deriving all history",
            COLD_START_LOOKBACK_HOURS,
            datetime.fromtimestamp(floor, tz=timezone.utc).isoformat(),
        )

    selected, mode, active_ids, window_start = _select_parents(front, parents, force)

    total_links = 0
    total_posts = 0
    counter_lock = threading.Lock()
    done = [0]

    def _one(parent):
        # Each worker gets its own clients: requests.Session and the Anthropic
        # client are not guaranteed safe to share across threads.
        f = FrontClient()
        c = AnthropicClient()
        try:
            links, posts, _ = process_conversation(
                f, c, parent, force=force,
                active_ids=active_ids, window_start=window_start,
            )
        except Exception:
            logger.exception("Failed processing parent %s", parent.get("id"))
            return 0, 0
        return links, posts

    logger.info(
        "Processing %d parent(s) with %d worker(s)", len(selected), SWEEP_CONCURRENCY
    )
    with ThreadPoolExecutor(max_workers=SWEEP_CONCURRENCY) as pool:
        futures = {pool.submit(_one, p): p for p in selected}
        for fut in as_completed(futures):
            links, posts = fut.result()
            with counter_lock:
                total_links += links
                total_posts += posts
                done[0] += 1
                if done[0] % 250 == 0:
                    logger.info(
                        "Progress: %d/%d parents, %d links, %d posts",
                        done[0], len(selected), total_links, total_posts,
                    )

    # Only advance the cursor after a run that actually finished; a crash must
    # leave it where it was so the next run re-covers the same window.
    set_state(EVENTS_CURSOR_KEY, str(run_started_epoch))

    persistent = get_persistent_failures()
    if persistent:
        logger.error(
            "%d link(s) have failed %d+ consecutive runs and are not "
            "recovering on their own — %s",
            len(persistent), FAILURE_SURFACE_THRESHOLD,
            ", ".join(
                f"{p['linked_conversation_id']} (parent {p['parent_conversation_id']}, "
                f"{p['consecutive_failures']}x, last: {p['last_error'][:80]})"
                for p in persistent[:10]
            ),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    record_sync_run(
        "success", len(selected), total_links, total_posts, duration_ms,
        f"mode={mode}; {len(selected)}/{len(parents)} parents"
        + (f"; {len(persistent)} persistent failure(s)" if persistent else ""),
    )
    logger.info(
        "=== Run complete [%s]: %d of %d parents, %d links, %d posts, "
        "%d persistent failure(s) in %d ms ===",
        mode, len(selected), len(parents), total_links, total_posts,
        len(persistent), duration_ms,
    )
    return {
        "status": "ok",
        "mode": mode,
        "parents_in_tag": len(parents),
        "parents_checked": len(selected),
        "links_checked": total_links,
        "comments_posted": total_posts,
        "persistent_failures": len(persistent),
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
