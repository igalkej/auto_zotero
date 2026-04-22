"""Stage 01 academic / non-academic classifier (plan_01 §3.1).

Three branches run in order, each cheaper than the next:

1. **Positive heuristic** (cost: $0) — accept immediately when the text of
   pages 1-3 contains a DOI, arXiv ID, valid ISBN, or one of a small set
   of academic keywords.
2. **Negative heuristic** (cost: $0) — reject immediately when the PDF
   has ≤ 2 pages *and* either lacks extractable text or contains a
   billing/personal-document keyword on page 1.
3. **LLM gate** (cost: ~$0.0004/call) — anything left over is sent to
   ``gpt-4o-mini`` with a short prompt asking for
   ``{"is_academic", "confidence", "reason"}``. Low-confidence results
   keep the item as academic but mark ``needs_review`` for manual review
   in Stage 06 (conservative bias — we would rather keep a borderline
   item than lose a real paper).

Each branch is a pure function; the orchestrator :func:`classify` is the
only async entry point (it awaits the LLM gate when needed). Tests hit
the branches directly and mock the OpenAI client when exercising the
gate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from zotai.api.openai_client import OpenAIClient, UsageRecord
from zotai.utils.logging import get_logger

log = get_logger(__name__)

Decision = Literal["academic", "reject"]
Confidence = Literal["low", "medium", "high"]
ClassifierBranch = Literal[
    "heuristic_positive",
    "heuristic_negative",
    "llm_gate",
    "skipped_llm_gate",
]


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)
_ARXIV_RE = re.compile(
    r"(?:arXiv:|arxiv\.org/abs/)(\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
_ISBN_CANDIDATE_RE = re.compile(
    r"\bISBN(?:-1[03])?[:\s]*([\d\-X ]{10,17})",
    re.IGNORECASE,
)

_ACADEMIC_KEYWORDS: frozenset[str] = frozenset(
    {
        "abstract",
        "references",
        "bibliography",
        "introduction",
        "keywords",
        "jel codes",
        "et al.",
        "university of",
        "universidad de",
        "instituto de",
    }
)

_BILLING_KEYWORDS: frozenset[str] = frozenset(
    {
        "factura",
        "recibo",
        "invoice",
        "receipt",
        "cuit",
        "cuil",
        "dni",
        "ticket",
        "boleta",
        "comprobante",
        "nota de débito",
        "nota de crédito",
        "voucher",
        "bill",
    }
)

_LLM_GATE_MODEL = "gpt-4o-mini"
_LLM_GATE_MAX_ATTEMPTS = 2
_FIRST_PAGE_SNIPPET_CHARS = 500


@dataclass(frozen=True)
class ClassificationResult:
    """The outcome of one :func:`classify` call."""

    decision: Decision
    needs_review: bool
    branch: ClassifierBranch
    rejection_reason: str | None = None
    llm_reason: str | None = None


@dataclass(frozen=True)
class GateResult:
    """Decoded payload from the LLM gate (plan_01 §3.1 Rama 3)."""

    is_academic: bool
    confidence: Confidence
    reason: str


# ─── Branch 1: positive heuristic (zero cost) ──────────────────────────────


def _is_valid_isbn_10(digits: str) -> bool:
    if len(digits) != 10:
        return False
    total = 0
    for idx, char in enumerate(digits):
        if char == "X" and idx == 9:
            value = 10
        elif char.isdigit():
            value = int(char)
        else:
            return False
        total += value * (10 - idx)
    return total % 11 == 0


def _is_valid_isbn_13(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    return total % 10 == 0


def _has_valid_isbn(text: str) -> bool:
    for match in _ISBN_CANDIDATE_RE.finditer(text):
        digits = re.sub(r"[\-\s]", "", match.group(1)).upper()
        if _is_valid_isbn_10(digits) or _is_valid_isbn_13(digits):
            return True
    return False


def heuristic_accept(pages_text: Iterable[str]) -> bool:
    """Return True when pages 1-3 carry at least one positive academic marker."""
    joined = "\n".join(pages_text)
    if not joined:
        return False
    if _DOI_RE.search(joined):
        return True
    if _ARXIV_RE.search(joined):
        return True
    if _has_valid_isbn(joined):
        return True
    lowered = joined.lower()
    return any(keyword in lowered for keyword in _ACADEMIC_KEYWORDS)


# ─── Branch 2: negative heuristic (zero cost) ──────────────────────────────


def heuristic_reject(
    *,
    page_count: int,
    has_text: bool,
    first_page_text: str,
) -> str | None:
    """Return a rejection-reason string for short non-papers, else None.

    Applies only when ``page_count <= 2``. Matches plan_01 §3.1 Rama 2:
    reject when the PDF is short *and* either contains a billing /
    personal-document keyword on page 1 *or* has no extractable text.
    Billing-keyword matches are checked first because the keyword is the
    informative signal — a short receipt PDF whose text is below the
    ``has_text`` threshold still deserves the ``billing_keyword:...``
    label rather than a generic ``short_no_text``.
    """
    if page_count > 2:
        return None
    lowered = first_page_text.lower()
    for keyword in _BILLING_KEYWORDS:
        if keyword in lowered:
            return f"billing_keyword:{keyword}"
    if not has_text:
        return "short_no_text"
    return None


# ─── Branch 3: LLM gate (ambiguous) ────────────────────────────────────────


_LLM_PROMPT_TEMPLATE = """\
You are classifying a PDF document for a researcher's bibliographic library.

Here are the first {snippet_len} characters of page 1:
---
{snippet}
---

Page count: {page_count}

Return JSON: {{"is_academic": bool, "confidence": "low"|"medium"|"high", "reason": "<one short sentence>"}}

Academic = research paper, preprint, book chapter, thesis, technical report, working paper, or a similar scholarly work.
Non-academic = bill, receipt, ID card, manual, slideshow deck, contract, personal document, administrative form, screenshot.\
"""


async def llm_gate(
    *,
    first_page_snippet: str,
    page_count: int,
    openai_client: OpenAIClient,
    model: str = _LLM_GATE_MODEL,
) -> tuple[GateResult, UsageRecord]:
    """Ask ``gpt-4o-mini`` whether an ambiguous PDF is academic.

    Retries once on JSON parse failure (two attempts total). If both fail,
    defaults to ``(is_academic=True, confidence="low", reason="malformed_json")``
    — the conservative bias from plan_01 §3.1 edge cases: do not drop a
    PDF we could not classify.
    """
    snippet = first_page_snippet[:_FIRST_PAGE_SNIPPET_CHARS]
    prompt = _LLM_PROMPT_TEMPLATE.format(
        snippet_len=_FIRST_PAGE_SNIPPET_CHARS,
        snippet=snippet,
        page_count=page_count,
    )
    last_usage: UsageRecord | None = None
    for attempt in range(1, _LLM_GATE_MAX_ATTEMPTS + 1):
        usage = await openai_client.classify_document(prompt=prompt, model=model)
        last_usage = usage
        try:
            content = usage.response.choices[0].message.content or "{}"
            payload = json.loads(content)
            is_academic = bool(payload["is_academic"])
            raw_confidence = payload.get("confidence", "low")
            confidence: Confidence = (
                raw_confidence
                if raw_confidence in ("low", "medium", "high")
                else "low"
            )
            reason = str(payload.get("reason", "")).strip() or "unspecified"
            return GateResult(is_academic, confidence, reason), usage
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            log.warning(
                "classifier.llm_gate.parse_failed",
                attempt=attempt,
                error=str(exc),
            )
    assert last_usage is not None  # loop always sets it before exit
    return (
        GateResult(is_academic=True, confidence="low", reason="malformed_json"),
        last_usage,
    )


# ─── Orchestrator ──────────────────────────────────────────────────────────


async def classify(
    *,
    pages_text: list[str],
    page_count: int,
    has_text: bool,
    skip_llm_gate: bool,
    openai_client: OpenAIClient | None,
) -> tuple[ClassificationResult, UsageRecord | None]:
    """Run the three branches in order and return the final decision.

    Returns:
        ``(result, usage)`` — ``usage`` is the OpenAI ``UsageRecord`` when
        the LLM gate ran, otherwise ``None``.
    """
    if heuristic_accept(pages_text):
        return (
            ClassificationResult(
                decision="academic",
                needs_review=False,
                branch="heuristic_positive",
            ),
            None,
        )

    first_page = pages_text[0] if pages_text else ""
    rejection = heuristic_reject(
        page_count=page_count,
        has_text=has_text,
        first_page_text=first_page,
    )
    if rejection is not None:
        return (
            ClassificationResult(
                decision="reject",
                needs_review=False,
                branch="heuristic_negative",
                rejection_reason=rejection,
            ),
            None,
        )

    if skip_llm_gate or openai_client is None:
        return (
            ClassificationResult(
                decision="academic",
                needs_review=True,
                branch="skipped_llm_gate",
            ),
            None,
        )

    gate_result, usage = await llm_gate(
        first_page_snippet=first_page,
        page_count=page_count,
        openai_client=openai_client,
    )

    if gate_result.is_academic and gate_result.confidence in ("medium", "high"):
        return (
            ClassificationResult(
                decision="academic",
                needs_review=False,
                branch="llm_gate",
                llm_reason=gate_result.reason,
            ),
            usage,
        )
    if gate_result.is_academic and gate_result.confidence == "low":
        return (
            ClassificationResult(
                decision="academic",
                needs_review=True,
                branch="llm_gate",
                llm_reason=gate_result.reason,
            ),
            usage,
        )
    if not gate_result.is_academic and gate_result.confidence in ("medium", "high"):
        return (
            ClassificationResult(
                decision="reject",
                needs_review=False,
                branch="llm_gate",
                rejection_reason="llm_non_academic",
                llm_reason=gate_result.reason,
            ),
            usage,
        )
    # is_academic=False + confidence=low → conservative include
    return (
        ClassificationResult(
            decision="academic",
            needs_review=True,
            branch="llm_gate",
            llm_reason=gate_result.reason,
        ),
        usage,
    )


__all__ = [
    "ClassificationResult",
    "ClassifierBranch",
    "Confidence",
    "Decision",
    "GateResult",
    "classify",
    "heuristic_accept",
    "heuristic_reject",
    "llm_gate",
]
