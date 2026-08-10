"""Front API client — tagged conversations, messages, comments, post comment."""
import logging
import time
from typing import Iterator, Optional

import requests

from config import FRONT_API_KEY, FRONT_API_URL

logger = logging.getLogger(__name__)

# Front rate-limits hard. A 429 on the first page of the tag listing used to
# abort the entire sweep, which is how a burst of triggers turned into a burst
# of "Failed to fetch tagged conversations" errors.
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_DEFAULT_WAIT = 10.0
RATE_LIMIT_MAX_WAIT = 120.0
RATE_LIMIT_MIN_WAIT = 1.0


class EventsTruncated(RuntimeError):
    """The events feed had more pages than we allow — cursor is too far back."""


def _retry_after_seconds(resp: requests.Response, attempt: int) -> float:
    """How long to wait before retrying.

    Front does not send Retry-After on its 429s (confirmed: 0 occurrences
    across 114 live 429s on 2026-08-10). It does send X-RateLimit-Reset — a
    unix timestamp for when the window clears — which is a real measurement
    of the wait, unlike a guessed constant. Prefer that; only fall back to
    exponential backoff when neither header is present.
    """
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), RATE_LIMIT_MAX_WAIT)
        except ValueError:
            pass

    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            wait = float(reset) - time.time()
            if wait > 0:
                return min(max(wait, RATE_LIMIT_MIN_WAIT), RATE_LIMIT_MAX_WAIT)
        except ValueError:
            pass

    return min(RATE_LIMIT_DEFAULT_WAIT * (2 ** attempt), RATE_LIMIT_MAX_WAIT)


class FrontClient:
    def __init__(self):
        self.api_url = FRONT_API_URL
        self.token = FRONT_API_KEY

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get_with_backoff(
        self, url: str, params: dict | None = None, timeout: int = 30
    ) -> requests.Response:
        """GET with retry on 429. Any other error status raises as before."""
        for attempt in range(RATE_LIMIT_MAX_RETRIES):
            resp = requests.get(
                url, headers=self._headers(), params=params, timeout=timeout
            )
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp
            wait = _retry_after_seconds(resp, attempt)
            logger.warning(
                "Front rate-limited %s — retry %d/%d in %.1fs",
                url, attempt + 1, RATE_LIMIT_MAX_RETRIES, wait,
            )
            time.sleep(wait)
        resp.raise_for_status()
        return resp

    def _get_paginated(self, url: str, params: dict | None = None) -> Iterator[dict]:
        """Yield items across Front's cursor-paginated `_results` responses."""
        next_url = url
        current_params = params
        while next_url:
            resp = self._get_with_backoff(next_url, current_params)
            data = resp.json()
            for item in data.get("_results", []):
                yield item
            next_url = (data.get("_pagination") or {}).get("next")
            # After the first call, the `next` URL includes the cursor;
            # don't re-send original params.
            current_params = None

    # ------------------------------------------------------------------ events

    def get_events_since(
        self, after_epoch: float, types: Optional[list] = None, max_pages: int = 1500
    ) -> Iterator[dict]:
        """Yield account events emitted after `after_epoch`.

        Front rejects a float here with a 400, so the cursor is floored to an
        int (verified against the live API 2026-08-10). Pages hold ~15 events;
        this account emits ~570 activity events/hour, so `max_pages` caps a
        runaway cursor at roughly 40 hours of history.
        """
        params = {"q[after]": int(after_epoch), "limit": 100}
        if types:
            params["q[types][]"] = types
        url = f"{self.api_url}/events"
        pages = 0
        while url and pages < max_pages:
            resp = self._get_with_backoff(url, params if pages == 0 else None)
            data = resp.json()
            pages += 1
            for item in data.get("_results", []):
                yield item
            url = (data.get("_pagination") or {}).get("next")
        if url:
            logger.warning(
                "DEGRADED: events feed still had pages after %d — cursor too old",
                max_pages,
            )
            raise EventsTruncated(f"events feed exceeded {max_pages} pages")

    # ------------------------------------------------------------------ reads

    def get_conversations_by_tag(self, tag_id: str) -> list[dict]:
        url = f"{self.api_url}/tags/{tag_id}/conversations"
        return list(self._get_paginated(url))

    def get_conversation(self, conversation_id: str) -> dict:
        url = f"{self.api_url}/conversations/{conversation_id}"
        return self._get_with_backoff(url, timeout=15).json()

    def get_comments(self, conversation_id: str) -> list[dict]:
        url = f"{self.api_url}/conversations/{conversation_id}/comments"
        return list(self._get_paginated(url))

    def get_messages(self, conversation_id: str) -> list[dict]:
        url = f"{self.api_url}/conversations/{conversation_id}/messages"
        return list(self._get_paginated(url))

    # ------------------------------------------------------------------ write

    def post_comment(
        self, conversation_id: str, body: str, is_pinned: bool = False
    ) -> dict:
        url = f"{self.api_url}/conversations/{conversation_id}/comments"
        payload: dict = {"body": body}
        if is_pinned:
            payload["is_pinned"] = True
        resp = requests.post(
            url, json=payload, headers=self._headers(), timeout=15
        )
        resp.raise_for_status()
        logger.info(
            "Posted comment to %s (pinned=%s)", conversation_id, is_pinned
        )
        return resp.json()

    def patch_comment(
        self, comment_id: str, body: str, is_pinned: Optional[bool] = None
    ) -> dict:
        url = f"{self.api_url}/comments/{comment_id}"
        payload: dict = {"body": body}
        if is_pinned is not None:
            payload["is_pinned"] = is_pinned
        resp = requests.patch(
            url, json=payload, headers=self._headers(), timeout=15
        )
        resp.raise_for_status()
        logger.info("Patched comment %s", comment_id)
        if resp.status_code == 204 or not resp.content:
            return {"id": comment_id}
        return resp.json()
