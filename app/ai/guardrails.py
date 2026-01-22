"""Tutor guardrails. Users are minors, so input and output are checked before and after the model.

Input: self-harm (supportive reply + escalation advice, no LLM call), prompt injection / answer-key fishing
(polite refusal, no LLM call), personal data (redacted before it reaches the model).
Output: system-prompt leakage. Optionally also Amazon Bedrock Guardrails.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal

from app.core.config import get_settings

log = logging.getLogger("eduhub.guardrails")

Action = Literal["allow", "redact", "block"]


@dataclass(frozen=True)
class Verdict:
    action: Action
    category: str | None
    text: str
    reply: str | None = None


SELF_HARM_REPLY = (
    "I'm really sorry you're feeling this way. You don't have to go through it alone. Please talk to a trusted adult, "
    "a teacher or your school counsellor today. If you are in immediate danger, call 991 (Ethiopia emergency) or your "
    "local emergency number right now. I'm here to help with your studies whenever you're ready."
)
INJECTION_REPLY = "I can only help with your own studies, like explaining concepts, reviewing your mistakes or planning practice. What would you like to work on?"
LEAK_REPLY = "Sorry, I can't share that. Let's get back to your studies. What topic should we work on?"
OFF_TOPIC_REPLY = (
    "I'm your exam-prep tutor, so I can't write code or build things for you. I can help you master mathematics, "
    "English, SAT, physics, chemistry, biology, history and geography. What would you like to work on?"
)

_SELF_HARM = re.compile(
    r"\b(kill|hurt|harm|cut)\s+(myself|me)\b|\bsuicid|\bend\s+(my|it)\s+(life|all)\b|\bself[-\s]?harm|\bwant\s+to\s+die\b",
    re.I,
)
_INJECTION = re.compile(
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules|prompts?)"
    r"|(reveal|show|print|repeat|tell\s+me)\s+(me\s+)?(your|the)\s+(system\s+prompt|instructions|hidden\s+rules)"
    r"|\bsystem\s+prompt\b|developer\s+mode|\bjailbreak|pretend\s+(you\s+are|to\s+be)\s+(?!a\s+(tutor|teacher))"
    r"|answer\s+key|(answers?|solutions?)\s+(to|for)\s+(the\s+)?(live\s+|current\s+|upcoming\s+)?(contest|exam|quiz)",
    re.I,
)
# Programming is outside the curriculum, and a tutor that writes code becomes a free general assistant
# on the school's budget. Language names alone are ambiguous ("Is a python a reptile?"), as are
# words like function, loop or react, which are ordinary maths, physics and chemistry. So a request
# is refused only when a language and a code word appear together, or when it plainly asks for a
# program to be written. Anything subtler is left to the system prompt.
_LANGUAGE = re.compile(
    r"(?<!\w)(?:python|javascript|typescript|java|c\+\+|c#|html|css|sql|php|golang|kotlin|node(?:\.?js)?)(?!\w)", re.I
)
_CODE_WORD = re.compile(r"\b(?:code|coding|program|script|function|app|website|calculator|game|bot|loop|class)\b", re.I)
_BUILD_REQUEST = re.compile(
    r"\b(?:write|wrte|create|generate|build|make|fix|debug)\s+(?:me\s+)?(?:a|an|some|the|my|this|these)?\s*"
    r"(?:simple|small|quick|basic)?\s*(?:code|program|script|software|app|website)\b",
    re.I,
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\w)(\+?251[\s-]?|0)?9\d{2}[\s-]?\d{3}[\s-]?\d{3}(?!\w)|(?<!\w)\+\d{10,14}(?!\w)")
_LEAK_MARKERS = ("You are EduHub Tutor", "The student's name is", "Ground advice in the student's real data")
# How much of a streamed reply to hold back from the student. A marker can't be fully written
# until this many characters after it begins, so withholding the trailing OUTPUT_HOLDBACK
# characters means no part of a leak is ever displayed before `check_output` can catch it.
OUTPUT_HOLDBACK = max(len(m) for m in _LEAK_MARKERS)


def check_input(text: str) -> Verdict:
    if _SELF_HARM.search(text):
        return Verdict("block", "self_harm", text, SELF_HARM_REPLY)
    if _INJECTION.search(text):
        return Verdict("block", "prompt_injection", text, INJECTION_REPLY)
    if (_LANGUAGE.search(text) and _CODE_WORD.search(text)) or _BUILD_REQUEST.search(text):
        return Verdict("block", "off_topic", text, OFF_TOPIC_REPLY)
    redacted = _PHONE.sub("[phone]", _EMAIL.sub("[email]", text))
    if redacted != text:
        return Verdict("redact", "pii", redacted)
    return Verdict("allow", None, text)


def check_output(text: str) -> Verdict:
    if any(marker.lower() in text.lower() for marker in _LEAK_MARKERS):
        return Verdict("block", "prompt_leak", text, LEAK_REPLY)
    return Verdict("allow", None, text)


def _bedrock_guardrail(text: str) -> bool:
    """True if the configured Bedrock Guardrail intervenes."""
    import boto3

    s = get_settings()
    client = boto3.client("bedrock-runtime", region_name=s.aws_region)
    r = client.apply_guardrail(
        guardrailIdentifier=s.bedrock_guardrail_id,
        guardrailVersion=s.bedrock_guardrail_version,
        source="INPUT",
        content=[{"text": {"text": text}}],
    )
    return r.get("action") == "GUARDRAIL_INTERVENED"


async def check_input_full(text: str) -> Verdict:
    verdict = check_input(text)
    if verdict.action == "block" or not get_settings().bedrock_guardrail_id:
        return verdict
    try:
        if await asyncio.to_thread(_bedrock_guardrail, verdict.text):
            return Verdict("block", "bedrock_guardrail", text, INJECTION_REPLY)
    except Exception as e:  # Fail open to the built-in checks if the managed guardrail is unreachable.
        log.warning("bedrock guardrail unavailable", extra={"fields": {"error": type(e).__name__}})
    return verdict
