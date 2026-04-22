"""Unit tests for :mod:`zotai.s1.classifier`.

These tests exercise the three branches in isolation. Stage-level
integration (excluded CSV, persistence gating, budget aborts) lives in
``test_stage_01.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from zotai.api.openai_client import UsageRecord
from zotai.s1.classifier import (
    ClassificationResult,
    GateResult,
    classify,
    heuristic_accept,
    heuristic_reject,
    llm_gate,
)


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeResponse:
    def __init__(
        self, content: str, prompt_tokens: int = 50, completion_tokens: int = 20
    ) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(prompt_tokens, completion_tokens)


def _fake_client(responses: list[str]) -> MagicMock:
    """Build a ``MagicMock`` that yields the given JSON strings, one per call."""
    iterator = iter(responses)

    async def _classify_document(*, prompt: str, model: str = "gpt-4o-mini") -> UsageRecord:
        content = next(iterator)
        resp = _FakeResponse(content)
        return UsageRecord(
            model=model,
            prompt_tokens=resp.usage.prompt_tokens,
            completion_tokens=resp.usage.completion_tokens,
            cost_usd=0.0001,
            response=resp,
        )

    client = MagicMock()
    client.classify_document = _classify_document
    client.budget_usd = 1.0
    client.spent_usd = 0.0
    return client


# ─── heuristic_accept ──────────────────────────────────────────────────────


def test_heuristic_accept_via_doi() -> None:
    assert heuristic_accept(["Some title", "Body 10.1234/example.xyz more body"])


def test_heuristic_accept_via_arxiv() -> None:
    assert heuristic_accept(["arXiv:2301.12345 submitted 2024"])


def test_heuristic_accept_via_keyword_abstract() -> None:
    assert heuristic_accept(["Title\n\nAbstract\nBody"])


def test_heuristic_accept_via_keyword_references() -> None:
    assert heuristic_accept(["References\n1. Smith 2020"])


def test_heuristic_accept_via_valid_isbn_13() -> None:
    # 978-3-16-148410-0 has a valid ISBN-13 checksum.
    assert heuristic_accept(["ISBN: 978-3-16-148410-0"])


def test_heuristic_accept_rejects_invalid_isbn_13() -> None:
    # Corrupt the checksum digit.
    assert not heuristic_accept(["ISBN: 978-3-16-148410-1 with no other markers"])


def test_heuristic_accept_no_markers_returns_false() -> None:
    assert not heuristic_accept(["Generic body text without academic signals."])


def test_heuristic_accept_empty_returns_false() -> None:
    assert not heuristic_accept([])
    assert not heuristic_accept([""])


# ─── heuristic_reject ──────────────────────────────────────────────────────


def test_heuristic_reject_short_no_text_returns_reason() -> None:
    assert (
        heuristic_reject(page_count=1, has_text=False, first_page_text="")
        == "short_no_text"
    )


def test_heuristic_reject_billing_keyword_factura() -> None:
    assert (
        heuristic_reject(
            page_count=1,
            has_text=True,
            first_page_text="Factura 001 total: $1500",
        )
        == "billing_keyword:factura"
    )


def test_heuristic_reject_billing_keyword_dni() -> None:
    assert (
        heuristic_reject(
            page_count=2,
            has_text=True,
            first_page_text="Autoridad emisora DNI 38123456",
        )
        == "billing_keyword:dni"
    )


def test_heuristic_reject_multipage_pass_through() -> None:
    assert (
        heuristic_reject(page_count=5, has_text=False, first_page_text="")
        is None
    )


def test_heuristic_reject_short_no_billing_pass_through() -> None:
    # The text must not contain any substring of the billing vocabulary —
    # "bill" is a keyword, so words like "billing" trigger a false match.
    assert (
        heuristic_reject(
            page_count=1,
            has_text=True,
            first_page_text="A short note without special markers.",
        )
        is None
    )


# ─── llm_gate ──────────────────────────────────────────────────────────────


async def test_llm_gate_returns_parsed_payload() -> None:
    client = _fake_client(
        [
            json.dumps(
                {
                    "is_academic": True,
                    "confidence": "high",
                    "reason": "abstract + methods",
                }
            )
        ]
    )
    result, usage = await llm_gate(
        first_page_snippet="any text",
        page_count=5,
        openai_client=client,
    )
    assert result == GateResult(
        is_academic=True, confidence="high", reason="abstract + methods"
    )
    assert usage.cost_usd == 0.0001


async def test_llm_gate_retries_once_then_defaults_when_malformed() -> None:
    client = _fake_client(["not json", "still not json"])
    result, _ = await llm_gate(
        first_page_snippet="x",
        page_count=3,
        openai_client=client,
    )
    assert result.is_academic is True
    assert result.confidence == "low"
    assert "malformed" in result.reason


async def test_llm_gate_recovers_on_second_attempt() -> None:
    client = _fake_client(
        [
            "{broken",
            json.dumps(
                {"is_academic": False, "confidence": "medium", "reason": "invoice"}
            ),
        ]
    )
    result, _ = await llm_gate(
        first_page_snippet="x",
        page_count=1,
        openai_client=client,
    )
    assert result == GateResult(
        is_academic=False, confidence="medium", reason="invoice"
    )


async def test_llm_gate_coerces_unknown_confidence_to_low() -> None:
    client = _fake_client(
        [json.dumps({"is_academic": True, "confidence": "kinda", "reason": "x"})]
    )
    result, _ = await llm_gate(
        first_page_snippet="x",
        page_count=1,
        openai_client=client,
    )
    assert result.confidence == "low"


# ─── classify (orchestrator) ──────────────────────────────────────────────


async def test_classify_positive_heuristic_wins() -> None:
    result, usage = await classify(
        pages_text=["Title\n\nAbstract\nBody\nReferences"],
        page_count=3,
        has_text=True,
        skip_llm_gate=False,
        openai_client=None,
    )
    assert result == ClassificationResult(
        decision="academic", needs_review=False, branch="heuristic_positive"
    )
    assert usage is None


async def test_classify_negative_heuristic_rejects_before_llm() -> None:
    client = _fake_client([])  # must not be called

    result, usage = await classify(
        pages_text=["factura 001 total: $1500"],
        page_count=1,
        has_text=True,
        skip_llm_gate=False,
        openai_client=client,
    )
    assert result.decision == "reject"
    assert result.branch == "heuristic_negative"
    assert result.rejection_reason == "billing_keyword:factura"
    assert usage is None


async def test_classify_skip_llm_gate_marks_needs_review() -> None:
    result, usage = await classify(
        pages_text=["Generic text without markers."],
        page_count=5,
        has_text=True,
        skip_llm_gate=True,
        openai_client=MagicMock(),  # present but unused
    )
    assert result == ClassificationResult(
        decision="academic", needs_review=True, branch="skipped_llm_gate"
    )
    assert usage is None


async def test_classify_llm_high_confidence_academic() -> None:
    client = _fake_client(
        [json.dumps({"is_academic": True, "confidence": "high", "reason": "wp"})]
    )
    result, usage = await classify(
        pages_text=["Body without markers"],
        page_count=5,
        has_text=True,
        skip_llm_gate=False,
        openai_client=client,
    )
    assert result.decision == "academic"
    assert result.needs_review is False
    assert result.branch == "llm_gate"
    assert usage is not None


async def test_classify_llm_low_confidence_academic_needs_review() -> None:
    client = _fake_client(
        [json.dumps({"is_academic": True, "confidence": "low", "reason": "unclear"})]
    )
    result, _ = await classify(
        pages_text=["Body"],
        page_count=5,
        has_text=True,
        skip_llm_gate=False,
        openai_client=client,
    )
    assert result.decision == "academic"
    assert result.needs_review is True


async def test_classify_llm_high_confidence_rejects() -> None:
    client = _fake_client(
        [
            json.dumps(
                {"is_academic": False, "confidence": "high", "reason": "receipt"}
            )
        ]
    )
    result, _ = await classify(
        pages_text=["Body"],
        page_count=3,
        has_text=True,
        skip_llm_gate=False,
        openai_client=client,
    )
    assert result.decision == "reject"
    assert result.rejection_reason == "llm_non_academic"
    assert result.branch == "llm_gate"


async def test_classify_llm_low_confidence_rejection_is_conservative() -> None:
    """``is_academic=False`` with ``confidence='low'`` keeps item with needs_review."""
    client = _fake_client(
        [
            json.dumps(
                {"is_academic": False, "confidence": "low", "reason": "unclear"}
            )
        ]
    )
    result, _ = await classify(
        pages_text=["Body"],
        page_count=3,
        has_text=True,
        skip_llm_gate=False,
        openai_client=client,
    )
    assert result.decision == "academic"
    assert result.needs_review is True
    assert result.branch == "llm_gate"


async def test_classify_malformed_llm_json_defaults_to_academic_needs_review() -> None:
    """Two malformed attempts → include with needs_review (conservative)."""
    client = _fake_client(["not json", "also not json"])
    result, _ = await classify(
        pages_text=["Body"],
        page_count=5,
        has_text=True,
        skip_llm_gate=False,
        openai_client=client,
    )
    assert result.decision == "academic"
    assert result.needs_review is True
    assert result.branch == "llm_gate"
