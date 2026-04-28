"""Anthropic client — analyzes linked-conversation updates and returns a JSON decision.

Uses Claude Haiku 4.5 via the official Anthropic Python SDK with structured
outputs for guaranteed-valid JSON responses.
"""
import json
import logging

import anthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

logger = logging.getLogger(__name__)

MAX_TOKENS = 2048
MAX_BULLETS = 8
SDK_OUTPUT_CONFIG = False  # Anthropic's structured-output validator rejects our nested-array schema; rely on prompt + json.loads instead.

SYSTEM_PROMPT = (
    "You write extremely short status bullets for a property-management "
    "operations log. Each bullet appears under a heading that already names "
    "the linked conversation, so the bullet itself must NOT restate the "
    "subject or property. Aim for 30-60 characters; hard cap 80. Lead with "
    "the actor (Owner, Vendor, Manager, Tenant, etc.) or the action. No "
    "filler phrases like 'Update:', 'FYI', 'Just letting you know'. No "
    "leading dash or date in the text — those are stored separately. Return "
    "JSON only.\n\n"
    "Examples of the target style:\n"
    "- Owner has approved a vendor\n"
    "- Vendor has scheduled appointment\n"
    "- Repairs is following up for invoice\n"
    "- Milestone bid $1,235 received; 72hr deadline\n"
    "- Three estimates in; owner selection needed\n"
    "- Tenant declined first time slot; rescheduling"
)

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "shouldPost": {"type": "boolean"},
            "reasoning": {"type": "string"},
            "bullets": {
                "type": "array",
                "maxItems": MAX_BULLETS,
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["date", "text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["shouldPost", "reasoning", "bullets"],
        "additionalProperties": False,
    },
}


def _extract_json(text: str) -> str:
    """Pull a JSON object out of Claude's response.

    Without strict structured output, Claude sometimes wraps JSON in ```json
    fences or adds a brief preamble. This finds the outermost {...}.
    """
    text = text.strip()
    # Strip code fences
    if text.startswith("```"):
        # Remove first line (```json or ```) and trailing ```
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    # Find outermost JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _format_activity_item(item: dict) -> list[str]:
    ts = item.get("timestamp")
    date_str = f"{ts.month}/{ts.day}" if ts else "?"
    if item["type"] == "message":
        direction = "Inbound" if item.get("is_inbound") else "Outbound"
        author = item.get("author") or "Unknown"
        lines = [f"- [{date_str}] [{direction} Message] From: {author}"]
        if item.get("subject"):
            lines.append(f"  Subject: {item['subject']}")
        lines.append(f"  {item.get('body', '')}")
        return lines
    author = item.get("author") or "Unknown"
    return [f"- [{date_str}] [Internal Comment] {author}: {item.get('body', '')}"]


def _format_context(updates: list[dict], previous_bullets: list[str]) -> str:
    lines = [
        "Summarize the activity from a linked conversation into short bullets "
        "for the operations log.",
        "",
    ]
    if previous_bullets:
        lines.append("Bullets ALREADY logged (do NOT repeat these facts):")
        for b in previous_bullets:
            lines.append(f"- {b}")
        lines.append("")

    for update in updates:
        subject = update.get("conversation_subject") or "Linked conversation"
        lines.append(f"## Activity in: {subject}")
        for item in update.get("activity", []):
            lines.extend(_format_activity_item(item))
        lines.append("")
    return "\n".join(lines)


def _build_user_prompt(updates: list[dict], previous_bullets: list[str]) -> str:
    context = _format_context(updates, previous_bullets)
    return f"""{context}

TASK: Produce up to {MAX_BULLETS} short bullets capturing the distinct decision-relevant beats of this thread.

Rules:
- Each bullet covers ONE distinct event or milestone (vendor scheduled, bid received, owner approval, follow-up sent, etc.).
- Hard cap 80 characters per bullet text; aim 30-60.
- Do NOT restate the conversation subject or property — the heading already shows it.
- Lead with the actor (Owner, Vendor, Manager, Tenant) or the action.
- The "date" field MUST be in M/D format and MUST match an actual activity date shown above (in the [M/D] tags). Do not invent dates.
- One sentence per bullet, no leading dash or date inside the text.
- Order bullets chronologically (oldest first).
- Skip insignificant pings, automated bounces, and anything already covered by the "already logged" bullets above.
- If nothing new is worth posting, return shouldPost=false and bullets=[].

Output JSON: {{"shouldPost": bool, "reasoning": str, "bullets": [{{"date": "M/D", "text": str}}, ...]}}"""


class AnthropicClient:
    def __init__(self):
        if not ANTHROPIC_API_KEY:
            self._client = None
        else:
            self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.model = ANTHROPIC_MODEL

    def analyze_updates(
        self,
        updates: list[dict],
        previous_bullets: list[str] | None = None,
    ) -> dict:
        """Return {"shouldPost": bool, "reasoning": str, "bullets": [{date, text}]}.

        On any failure, returns shouldPost=False so the caller skips posting.
        """
        empty = {"shouldPost": False, "reasoning": "", "bullets": []}
        if self._client is None:
            logger.error("ANTHROPIC_API_KEY not configured")
            return {**empty, "reasoning": "no_api_key"}

        kwargs = dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_prompt(updates, previous_bullets or []),
                }
            ],
        )
        if SDK_OUTPUT_CONFIG:
            kwargs["output_config"] = {"format": RESPONSE_SCHEMA}

        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            logger.exception("Anthropic API call failed")
            return {**empty, "reasoning": f"api_error: {exc}"}

        try:
            text = next(b.text for b in response.content if b.type == "text")
            result = json.loads(_extract_json(text))
        except (StopIteration, json.JSONDecodeError) as exc:
            logger.exception("Could not parse Anthropic response")
            return {**empty, "reasoning": f"parse_error: {exc}"}

        logger.info(
            "AI decision: shouldPost=%s bullets=%d reasoning=%s",
            result.get("shouldPost"),
            len(result.get("bullets") or []),
            result.get("reasoning"),
        )
        return result
