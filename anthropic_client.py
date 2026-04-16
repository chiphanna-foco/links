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

SYSTEM_PROMPT = (
    "You are a helpful assistant that analyzes conversation updates and "
    "creates concise summaries."
)

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "shouldPost": {"type": "boolean"},
            "reasoning": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["shouldPost", "reasoning", "message"],
        "additionalProperties": False,
    },
}


def _format_context(updates: list[dict]) -> str:
    lines = [
        "Analyze the following updates from linked Front conversations and "
        "determine if they are significant enough to report.",
        "",
    ]
    for update in updates:
        subject = update.get("conversation_subject") or "Linked conversation"
        convo_id = update.get("conversation_id", "")
        lines.append(f"\n## Linked Conversation: {subject}")
        lines.append(f"Link: https://app.frontapp.com/open/{convo_id}\n")
        for item in update.get("activity", []):
            if item["type"] == "message":
                direction = "Inbound" if item.get("is_inbound") else "Outbound"
                author = item.get("author") or "Unknown"
                lines.append(f"- [{direction} Message] From: {author}")
                if item.get("subject"):
                    lines.append(f"  Subject: {item['subject']}")
                lines.append(f"  {item.get('body', '')}\n")
            else:
                author = item.get("author") or "Unknown"
                lines.append(f"- [Internal Comment] {author}: {item.get('body', '')}\n")
    return "\n".join(lines)


def _build_user_prompt(updates: list[dict]) -> str:
    context = _format_context(updates)
    return f"""{context}


TASK: Determine if these updates are significant enough to notify the main conversation. Consider:
- Customer replies or new questions (HIGH priority)
- Status changes or resolutions (HIGH priority)
- Important internal decisions or updates (MEDIUM priority)
- Minor administrative messages (LOW priority - skip)


Review ALL the activity listed above and summarize ALL significant items together in ONE concise message.


If significant, write a VERY CONCISE summary using this format:
[Conversation Subject](https://app.frontapp.com/open/CONVERSATION_ID): Summarize all key updates in 1-2 sentences.


IMPORTANT DETAILS TO INCLUDE:
- Dates and times (e.g., "scheduled for Monday 4/20 at 8 AM")
- Specific decisions or changes (e.g., "changed mind", "approved", "declined")
- Action items or next steps
- Any dollar amounts or quantities


Make the conversation subject a clickable markdown link using the conversation ID provided above.


Examples:
- "[General Repairs Job #65840](https://app.frontapp.com/open/cnv_abc123): Owner initially requested hold, then changed mind. Now scheduled for Monday 4/20 at 8 AM."
- "[Billing Question - Acme Corp](https://app.frontapp.com/open/cnv_def456): Customer replied requesting $500 refund. Support approved and processed payment."


Keep it SHORT but COMPREHENSIVE - capture all important updates in 1-2 sentences.
Set `shouldPost` to false for insignificant updates; leave `message` empty in that case."""


class AnthropicClient:
    def __init__(self):
        if not ANTHROPIC_API_KEY:
            self._client = None
        else:
            self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.model = ANTHROPIC_MODEL

    def analyze_updates(self, updates: list[dict]) -> dict:
        """Return {"shouldPost": bool, "reasoning": str, "message": str}.

        On any failure, returns shouldPost=False so the caller skips posting.
        """
        if self._client is None:
            logger.error("ANTHROPIC_API_KEY not configured")
            return {"shouldPost": False, "reasoning": "no_api_key", "message": ""}

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_user_prompt(updates)}],
                output_config={"format": RESPONSE_SCHEMA},
            )
        except anthropic.APIError:
            logger.exception("Anthropic API call failed")
            return {"shouldPost": False, "reasoning": "api_error", "message": ""}

        try:
            text = next(b.text for b in response.content if b.type == "text")
            result = json.loads(text)
        except (StopIteration, json.JSONDecodeError):
            logger.exception("Could not parse Anthropic response")
            return {"shouldPost": False, "reasoning": "parse_error", "message": ""}

        logger.info(
            "AI decision: shouldPost=%s reasoning=%s",
            result.get("shouldPost"),
            result.get("reasoning"),
        )
        return result
