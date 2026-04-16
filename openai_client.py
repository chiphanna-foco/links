"""OpenAI client — analyzes linked-conversation updates and returns a JSON decision.

The system prompt is preserved verbatim from the original Google Apps Script
so behavior matches byte-for-byte.
"""
import json
import logging

import requests

from config import OPENAI_API_KEY, OPENAI_API_URL, OPENAI_MODEL

logger = logging.getLogger(__name__)

MAX_TOKENS = 500
TEMPERATURE = 0.3

SYSTEM_PROMPT = (
    "You are a helpful assistant that analyzes conversation updates and creates "
    "concise summaries. Always respond with valid JSON only, no markdown formatting."
)


def _format_context(updates: list[dict]) -> str:
    """Render the updates list in the same shape the Apps Script passed."""
    lines = [
        "Analyze the following updates from linked Front conversations and determine "
        "if they are significant enough to report.",
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


Respond in JSON format:
{{
  "shouldPost": true/false,
  "reasoning": "brief explanation",
  "message": "concise summary with linked subject and ALL key details in markdown format (if shouldPost is true)"
}}"""


class OpenAIClient:
    def __init__(self):
        self.api_url = OPENAI_API_URL
        self.api_key = OPENAI_API_KEY
        self.model = OPENAI_MODEL

    def analyze_updates(self, updates: list[dict]) -> dict:
        """Return {"shouldPost": bool, "reasoning": str, "message": str}.

        On any failure returns shouldPost=False so the caller skips posting."""
        if not self.api_key:
            logger.error("OPENAI_API_KEY not configured")
            return {"shouldPost": False, "reasoning": "no_api_key", "message": ""}

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(updates)},
            ],
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            resp = requests.post(self.api_url, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            result = json.loads(content)
            logger.info(
                "AI decision: shouldPost=%s reasoning=%s",
                result.get("shouldPost"),
                result.get("reasoning"),
            )
            return result
        except Exception:
            logger.exception("OpenAI analysis failed")
            return {"shouldPost": False, "reasoning": "error", "message": ""}
