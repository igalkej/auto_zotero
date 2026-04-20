"""OpenAI adapter with cost tracking + budget enforcement.

Cost is computed from a static price table sized for the models this
project uses (Jan 2026 pricing). When the per-call cost would push
`spent_usd` over `budget_usd`, `BudgetExceededError` is raised *before*
the call, so no accidental spend happens.

The adapter is deliberately narrow:
- `extract_metadata`: JSON-mode extraction for Stage 04d (plan_01 §3).
- `tag_paper`: JSON-mode tagging for Stage 05.
- `embed_text`: embedding for S2 semantic scoring + S3 re-use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

from openai import AsyncOpenAI

from zotai.utils.logging import get_logger

log = get_logger(__name__)


class BudgetExceededError(RuntimeError):
    """Raised when a call would exceed the configured spend ceiling."""


# Prices are $ per 1K tokens for chat models, $ per 1K tokens for embeddings.
# Numbers are from Jan 2026; adjust when OpenAI revises them.
_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "text-embedding-3-large": {"input": 0.00013, "output": 0.0},
    "text-embedding-3-small": {"input": 0.00002, "output": 0.0},
}


@dataclass
class UsageRecord:
    """Per-call accounting returned by every `OpenAIClient` method."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    response: Any = field(repr=False)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return the dollar cost of a call. Unknown models are billed at 0.0 (and logged)."""
    prices = _PRICING.get(model)
    if prices is None:
        log.warning("openai.unknown_model_pricing", model=model)
        return 0.0
    return (prompt_tokens / 1000.0) * prices["input"] + (
        completion_tokens / 1000.0
    ) * prices["output"]


class OpenAIClient:
    """Async OpenAI client with an always-on spend ledger."""

    def __init__(
        self,
        *,
        api_key: str,
        budget_usd: float,
        organization: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self._client = AsyncOpenAI(api_key=api_key, organization=organization)
        self.budget_usd = budget_usd
        self.spent_usd = 0.0

    def _charge(self, cost: float) -> None:
        """Add `cost` to the ledger after a successful call."""
        self.spent_usd += cost

    def _check_budget(self, projected_cost: float = 0.0) -> None:
        """Raise BudgetExceededError if the next call would go over budget."""
        if self.spent_usd + projected_cost > self.budget_usd:
            raise BudgetExceededError(
                f"Budget exceeded: spent=${self.spent_usd:.4f}, "
                f"projected=${projected_cost:.4f}, budget=${self.budget_usd:.4f}"
            )

    async def extract_metadata(
        self, *, text: str, model: str = "gpt-4o-mini"
    ) -> UsageRecord:
        """Ask the model to return bibliographic metadata as JSON.

        The caller validates the returned JSON against a Pydantic schema
        (Stage 04d in plan_01); this function stays schema-agnostic.
        """
        self._check_budget()
        system = (
            "You extract bibliographic metadata. Return JSON with fields: "
            "title, authors (list of {first, last}), year, item_type "
            "(journalArticle, book, bookSection, thesis, report, preprint, "
            "conferencePaper), venue, doi (null if absent), abstract. "
            "If a field cannot be determined with confidence, return null. "
            "Do not invent information."
        )
        resp = await self._client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
        return self._build_usage_record(resp, model)

    async def tag_paper(
        self,
        *,
        metadata: dict[str, Any],
        taxonomy: dict[str, list[dict[str, Any]]],
        model: str = "gpt-4o-mini",
    ) -> UsageRecord:
        """Return `{tema: [...], metodo: [...]}` from the configured taxonomy."""
        self._check_budget()
        tema_ids = [entry["id"] for entry in taxonomy.get("tema", [])]
        metodo_ids = [entry["id"] for entry in taxonomy.get("metodo", [])]
        system = (
            "You tag academic papers using a fixed taxonomy. "
            "Choose 1-4 tags from TEMA and 1-2 from METODO. "
            "Only return tag ids from the lists provided. If nothing fits, "
            "use fewer tags rather than forcing. Return JSON: "
            '{"tema": [...], "metodo": [...]}.'
        )
        user_prompt = json.dumps(
            {
                "paper": metadata,
                "tema": tema_ids,
                "metodo": metodo_ids,
            },
            ensure_ascii=False,
        )
        resp = await self._client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
        )
        return self._build_usage_record(resp, model)

    async def embed_text(
        self, *, text: str, model: str = "text-embedding-3-large"
    ) -> tuple[list[float], UsageRecord]:
        """Return `(vector, usage)` for a single piece of text."""
        self._check_budget()
        resp = await self._client.embeddings.create(model=model, input=text)
        prompt_tokens = resp.usage.prompt_tokens if resp.usage else len(text) // 4
        cost = estimate_cost(model, prompt_tokens, 0)
        self._charge(cost)
        vector = cast(list[float], resp.data[0].embedding)
        return vector, UsageRecord(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=0,
            cost_usd=cost,
            response=resp,
        )

    def _build_usage_record(self, resp: Any, model: str) -> UsageRecord:
        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
        cost = estimate_cost(model, prompt_tokens, completion_tokens)
        self._charge(cost)
        return UsageRecord(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            response=resp,
        )


__all__ = ["BudgetExceededError", "OpenAIClient", "UsageRecord", "estimate_cost"]
