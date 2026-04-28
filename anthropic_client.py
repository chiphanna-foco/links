"""Anthropic client — analyzes linked-conversation updates and returns a JSON decision.

Uses Claude Haiku 4.5 via the official Anthropic Python SDK with structured
outputs for guaranteed-valid JSON responses.
"""
import json
import logging

import anthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

logger = logging.getLogger(__name__)

MAX_TOKENS = 1024

SYSTEM_PROMPT = (
    "You write extremely short status bullets for a property-management "
    "operations log. Each bullet appears under a heading that already names "
    "the linked conversation, so the bullet itself must NOT restate the "
    "subject or property. Aim for 30-60 characters; hard cap 80. Lead with "
    "the actor (Owner, Vendor, Manager, Tenant, etc.) or the action. No "
    "filler phrases like 'Update:', 'FYI', 'Just letting you know'. No "
    "leading dash or date — those are added by the caller. Return JSON "
    "only.\n\n"
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
            "bullet": {"type": "string"},
        },
        "required": ["shouldPost", "reasoning", "bullet"],
        "additionalProperties": False,
    },
}


def _format_context(updates: list[dict], previous_bullets: list[str]) -> str:
    lines = [
        "Summarize the new activity from a linked conversation into ONE "
        "short bullet for the operations log.",
        "",
    ]
    if previous_bullets:
        lines.append("Bullets ALREADY logged for this linked thread (do not repeat these facts):")
        for b in previous_bullets:
            lines.append(f"- {b}")
        lines.append("")

    for update in updates:
        subject = update.get("conversation_subject") or "Linked conversation"
        lines.append(f"## New activity in: {subject}")
        for item in update.get("activity", []):
            if item["type"] == "message":
                direction = "Inbound" if item.get("is_inbound") else "Outbound"
                author = item.get("author") or "Unknown"
                lines.append(f"- [{direction} Message] From: {author}")
                if item.get("subject"):
                    lines.append(f"  Subject: {item['subject']}")
                lines.append(f"  {item.get('body', '')}")
            else:
                author = item.get("author") or "Unknown"
                lines.append(f"- [Internal Comment] {author}: {item.get('body', '')}")
        lines.append("")
    return "\n".join(lines)


def _build_user_prompt(updates: list[dict], previous_bullets: list[str]) -> str:
    context = _format_context(updates, previous_bullets)
    return f"""{context}

TASK: Produce ONE bullet capturing the most decision-relevant new fact.

Rules:
- Hard cap 80 characters; aim for 30-60.
- Do NOT restate the conversation subject or property — the heading already shows it.
- Lead with the actor (Owner, Vendor, Manager, Tenant) or the action.
- Include concrete specifics that drive a decision (dollar amounts, deadlines, dates) when present, but cut detail before going long.
- One sentence. No leading dash or date.
- Set shouldPost=false if the activity is not materially new (automated bounce, duplicate of a logged bullet, status ping with no new fact). Leave bullet empty in that case.

Output JSON: {{"shouldPost": bool, "reasoning": str, "bullet": str}}"""


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
        """Return {"shouldPost": bool, "reasoning": str, "bullet": str}.

        On any failure, returns shouldPost=False so the caller skips posting.
        """
        if self._client is None:
            logger.error("ANTHROPIC_API_KEY not configured")
            return {"shouldPost": False, "reasoning": "no_api_key", "bullet": ""}

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": _build_user_prompt(updates, previous_bullets or []),
                    }
                ],
                output_config={"format": RESPONSE_SCHEMA},
            )
        except anthropic.APIError:
            logger.exception("Anthropic API call failed")
            return {"shouldPost": False, "reasoning": "api_error", "bullet": ""}

        try:
            text = next(b.text for b in response.content if b.type == "text")
            result = json.loads(text)
        except (StopIteration, json.JSONDecodeError):
            logger.exception("Could not parse Anthropic response")
            return {"shouldPost": False, "reasoning": "parse_error", "bullet": ""}

        logger.info(
            "AI decision: shouldPost=%s reasoning=%s",
            result.get("shouldPost"),
            result.get("reasoning"),
        )
        return result
