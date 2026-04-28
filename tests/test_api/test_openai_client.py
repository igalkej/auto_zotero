"""Tests for :mod:`zotai.api.openai_client`.

The adapter wraps the official ``openai`` AsyncOpenAI SDK rather than
hitting HTTP directly, so we monkeypatch ``AsyncOpenAI`` with a minimal
in-memory fake. The fake records every call (model, messages, response
format) for assertions and returns a preconfigured response.

Coverage:

- ``estimate_cost``: pricing table, unknown-model fallback, embedding
  models (input-only cost).
- ``OpenAIClient.__init__``: empty api_key rejection, organization passthrough.
- Budget enforcement: every public method short-circuits with
  ``BudgetExceededError`` when the running ledger is already over.
- ``classify_document`` / ``extract_metadata`` / ``tag_paper``: prompt
  shape (system vs user, JSON mode) and cost charged on success.
- ``embed_text``: returns ``(vector, UsageRecord)``, falls back to a
  byte-count estimate when ``resp.usage`` is missing.
- ``_build_usage_record``: tolerates ``resp.usage = None``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from zotai.api import openai_client as oac
from zotai.api.openai_client import (
    BudgetExceededError,
    OpenAIClient,
    UsageRecord,
    estimate_cost,
)

# ── Fakes ──────────────────────────────────────────────────────────────────


@dataclass
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _FakeChatResponse:
    usage: _FakeUsage | None
    content: str = ""


@dataclass
class _FakeEmbeddingItem:
    embedding: list[float]


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingItem]
    usage: _FakeUsage | None


@dataclass
class _CallRecord:
    method: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class _FakeChatCompletions:
    def __init__(self, parent: _FakeAsyncOpenAI) -> None:
        self._parent = parent

    async def create(self, **kwargs: Any) -> _FakeChatResponse:
        self._parent.calls.append(_CallRecord(method="chat", kwargs=kwargs))
        return self._parent.chat_response


class _FakeChat:
    def __init__(self, parent: _FakeAsyncOpenAI) -> None:
        self.completions = _FakeChatCompletions(parent)


class _FakeEmbeddings:
    def __init__(self, parent: _FakeAsyncOpenAI) -> None:
        self._parent = parent

    async def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        self._parent.calls.append(_CallRecord(method="embed", kwargs=kwargs))
        return self._parent.embed_response


class _FakeAsyncOpenAI:
    """Drop-in for ``openai.AsyncOpenAI`` covering only the surface used."""

    def __init__(
        self,
        *,
        api_key: str,
        organization: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.organization = organization
        self.calls: list[_CallRecord] = []
        self.chat_response: _FakeChatResponse = _FakeChatResponse(
            usage=_FakeUsage(prompt_tokens=100, completion_tokens=50)
        )
        self.embed_response: _FakeEmbeddingResponse = _FakeEmbeddingResponse(
            data=[_FakeEmbeddingItem(embedding=[0.1, 0.2, 0.3])],
            usage=_FakeUsage(prompt_tokens=20, completion_tokens=0),
        )
        self.chat = _FakeChat(self)
        self.embeddings = _FakeEmbeddings(self)


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> _FakeAsyncOpenAI:
    """Patch ``AsyncOpenAI`` so every ``OpenAIClient(...)`` reuses one fake."""
    fake = _FakeAsyncOpenAI(api_key="placeholder")

    def _factory(**kwargs: Any) -> _FakeAsyncOpenAI:
        # Capture the kwargs the adapter passes through so tests can
        # assert on api_key / organization wiring.
        fake.api_key = kwargs.get("api_key", "")
        fake.organization = kwargs.get("organization")
        return fake

    monkeypatch.setattr(oac, "AsyncOpenAI", _factory)
    return fake


# ── estimate_cost ──────────────────────────────────────────────────────────


def test_estimate_cost_known_chat_model() -> None:
    cost = estimate_cost("gpt-4o-mini", prompt_tokens=1000, completion_tokens=500)
    assert cost == pytest.approx(0.00015 + 0.5 * 0.0006)


def test_estimate_cost_embedding_only_charges_input() -> None:
    cost = estimate_cost(
        "text-embedding-3-large", prompt_tokens=2000, completion_tokens=999
    )
    assert cost == pytest.approx(2 * 0.00013)


def test_estimate_cost_unknown_model_returns_zero() -> None:
    cost = estimate_cost("not-a-real-model", prompt_tokens=1000, completion_tokens=0)
    assert cost == 0.0


# ── __init__ ───────────────────────────────────────────────────────────────


def test_init_rejects_empty_api_key(fake_openai: _FakeAsyncOpenAI) -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIClient(api_key="", budget_usd=1.0)


def test_init_passes_organization_through(fake_openai: _FakeAsyncOpenAI) -> None:
    OpenAIClient(api_key="sk-x", budget_usd=1.0, organization="org-abc")
    assert fake_openai.api_key == "sk-x"
    assert fake_openai.organization == "org-abc"


# ── classify_document ──────────────────────────────────────────────────────


async def test_classify_document_happy_path(fake_openai: _FakeAsyncOpenAI) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=1.0)
    result = await client.classify_document(prompt="Is this academic? text=...")

    assert len(fake_openai.calls) == 1
    call = fake_openai.calls[0]
    assert call.method == "chat"
    assert call.kwargs["model"] == "gpt-4o-mini"
    assert call.kwargs["response_format"] == {"type": "json_object"}
    assert call.kwargs["messages"] == [
        {"role": "user", "content": "Is this academic? text=..."}
    ]

    assert isinstance(result, UsageRecord)
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50
    # gpt-4o-mini: 0.1 * 0.00015 + 0.05 * 0.0006 = 1.5e-5 + 3e-5 = 4.5e-5.
    assert result.cost_usd == pytest.approx(0.0000450)
    assert client.spent_usd == pytest.approx(0.0000450)


async def test_classify_document_aborts_when_over_budget(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=0.001)
    client.spent_usd = 0.002
    with pytest.raises(BudgetExceededError):
        await client.classify_document(prompt="x")
    assert fake_openai.calls == []


# ── extract_metadata ───────────────────────────────────────────────────────


async def test_extract_metadata_includes_system_and_user_messages(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=1.0)
    await client.extract_metadata(text="Page 1 — Title and abstract...")

    call = fake_openai.calls[0]
    assert call.kwargs["response_format"] == {"type": "json_object"}
    messages = call.kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "bibliographic metadata" in messages[0]["content"]
    assert messages[1] == {
        "role": "user",
        "content": "Page 1 — Title and abstract...",
    }


async def test_extract_metadata_aborts_when_over_budget(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=0.0)
    client.spent_usd = 0.5
    with pytest.raises(BudgetExceededError):
        await client.extract_metadata(text="any text")


# ── tag_paper ──────────────────────────────────────────────────────────────


async def test_tag_paper_serializes_metadata_and_taxonomy_ids(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=1.0)
    metadata = {"title": "Inflación en LATAM", "abstract": "Estudio sobre..."}
    taxonomy = {
        "tema": [{"id": "tema:inflation"}, {"id": "tema:fiscal"}],
        "metodo": [{"id": "metodo:dsge"}],
    }
    await client.tag_paper(metadata=metadata, taxonomy=taxonomy)

    call = fake_openai.calls[0]
    messages = call.kwargs["messages"]
    assert messages[0]["role"] == "system"
    payload = json.loads(messages[1]["content"])
    assert payload["paper"] == metadata
    assert payload["tema"] == ["tema:inflation", "tema:fiscal"]
    assert payload["metodo"] == ["metodo:dsge"]


async def test_tag_paper_handles_taxonomy_without_keys(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=1.0)
    await client.tag_paper(metadata={"title": "x"}, taxonomy={})
    call = fake_openai.calls[0]
    payload = json.loads(call.kwargs["messages"][1]["content"])
    assert payload["tema"] == []
    assert payload["metodo"] == []


async def test_tag_paper_aborts_when_over_budget(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=0.001)
    client.spent_usd = 0.002
    with pytest.raises(BudgetExceededError):
        await client.tag_paper(metadata={}, taxonomy={})


# ── embed_text ─────────────────────────────────────────────────────────────


async def test_embed_text_returns_vector_and_usage(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=1.0)
    vector, usage = await client.embed_text(text="hello world")

    assert vector == [0.1, 0.2, 0.3]
    assert usage.prompt_tokens == 20
    assert usage.completion_tokens == 0
    assert usage.cost_usd == pytest.approx(20 / 1000.0 * 0.00013)
    assert client.spent_usd == pytest.approx(usage.cost_usd)
    call = fake_openai.calls[0]
    assert call.method == "embed"
    assert call.kwargs["model"] == "text-embedding-3-large"
    assert call.kwargs["input"] == "hello world"


async def test_embed_text_falls_back_when_usage_missing(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=1.0)
    fake_openai.embed_response = _FakeEmbeddingResponse(
        data=[_FakeEmbeddingItem(embedding=[0.0])],
        usage=None,
    )
    text = "x" * 400  # 400 / 4 == 100 token estimate.
    _, usage = await client.embed_text(text=text)
    assert usage.prompt_tokens == 100
    assert usage.cost_usd == pytest.approx(100 / 1000.0 * 0.00013)


async def test_embed_text_aborts_when_over_budget(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=0.0)
    client.spent_usd = 0.5
    with pytest.raises(BudgetExceededError):
        await client.embed_text(text="anything")


# ── _build_usage_record ────────────────────────────────────────────────────


async def test_chat_response_without_usage_is_charged_zero(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=1.0)
    fake_openai.chat_response = _FakeChatResponse(usage=None)
    result = await client.classify_document(prompt="x")
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.cost_usd == 0.0
    assert client.spent_usd == 0.0


# ── ledger accumulation across calls ───────────────────────────────────────


async def test_consecutive_calls_accumulate_spend(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    client = OpenAIClient(api_key="sk-x", budget_usd=1.0)
    await client.classify_document(prompt="a")
    await client.classify_document(prompt="b")
    per_call = 100 / 1000.0 * 0.00015 + 50 / 1000.0 * 0.0006
    assert client.spent_usd == pytest.approx(2 * per_call)


async def test_call_after_budget_breach_raises_before_network(
    fake_openai: _FakeAsyncOpenAI,
) -> None:
    # Budget allows two calls; the third must abort before reaching the SDK.
    per_call = 100 / 1000.0 * 0.00015 + 50 / 1000.0 * 0.0006
    client = OpenAIClient(api_key="sk-x", budget_usd=per_call * 1.5)
    await client.classify_document(prompt="a")
    await client.classify_document(prompt="b")
    assert client.spent_usd == pytest.approx(2 * per_call)
    calls_before = len(fake_openai.calls)
    with pytest.raises(BudgetExceededError):
        await client.classify_document(prompt="c")
    assert len(fake_openai.calls) == calls_before
