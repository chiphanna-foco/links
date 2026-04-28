"""Front API client — tagged conversations, messages, comments, post comment."""
import logging
from typing import Iterator, Optional

import requests

from config import FRONT_API_KEY, FRONT_API_URL

logger = logging.getLogger(__name__)


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

    def _get_paginated(self, url: str, params: dict | None = None) -> Iterator[dict]:
        """Yield items across Front's cursor-paginated `_results` responses."""
        next_url = url
        current_params = params
        while next_url:
            resp = requests.get(
                next_url, headers=self._headers(), params=current_params, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("_results", []):
                yield item
            next_url = (data.get("_pagination") or {}).get("next")
            # After the first call, the `next` URL includes the cursor;
            # don't re-send original params.
            current_params = None

    # ------------------------------------------------------------------ reads

    def get_conversations_by_tag(self, tag_id: str) -> list[dict]:
        url = f"{self.api_url}/tags/{tag_id}/conversations"
        return list(self._get_paginated(url))

    def get_conversation(self, conversation_id: str) -> dict:
        url = f"{self.api_url}/conversations/{conversation_id}"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

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
