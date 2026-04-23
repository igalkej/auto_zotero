"""Tests for :mod:`zotai.s1.stage_05_tag`.

A fake ``OpenAIClient`` feeds scripted JSON responses (or
``BudgetExceededError`` instances) to ``tag_paper``; a fake
``ZoteroClient`` records every ``add_tags`` call. The taxonomy is
loaded from an in-tmp YAML file so we can flip ``status: template``
vs. ``status: customized`` per test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlmodel import Session, select

from zotai.api.openai_client import BudgetExceededError
from zotai.config import BudgetSettings, OpenAISettings, PathSettings, Settings, ZoteroSettings
from zotai.s1.handler import StageAbortedError
from zotai.s1.stage_05_tag import (
    Taxonomy,
    load_taxonomy,
    run_tag,
)
from zotai.state import Item, init_s1, make_s1_engine

# ─── Fakes ────────────────────────────────────────────────────────────────


class FakeOpenAIClient:
    def __init__(self, queue: list[str | Exception] | None = None) -> None:
        self._queue: list[str | Exception] = list(queue or [])
        self.tag_calls: list[dict[str, Any]] = []

    async def tag_paper(
        self,
        *,
        metadata: dict[str, Any],
        taxonomy: dict[str, list[dict[str, Any]]],
        model: str = "gpt-4o-mini",
    ) -> Any:
        self.tag_calls.append({"metadata": metadata, "taxonomy": taxonomy, "model": model})
        if not self._queue:
            raise AssertionError("tag_paper called more times than queued")
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _fake_usage(nxt, model)


def _fake_usage(content: str, model: str) -> Any:
    class _Msg:
        def __init__(self, c: str) -> None:
            self.content = c

    class _Choice:
        def __init__(self, c: str) -> None:
            self.message = _Msg(c)

    class _Response:
        def __init__(self, c: str) -> None:
            self.choices = [_Choice(c)]
            self.usage = type(
                "U", (), {"prompt_tokens": 50, "completion_tokens": 20}
            )()

    class _Usage:
        def __init__(self, r: Any) -> None:
            self.response = r
            self.model = model
            self.prompt_tokens = 50
            self.completion_tokens = 20
            self.cost_usd = 0.0005

    return _Usage(_Response(content))


class FakeZoteroClient:
    def __init__(self) -> None:
        self.dry_run = False
        self.add_tags_calls: list[tuple[str, list[str]]] = []
        self.should_raise: Exception | None = None

    def add_tags(self, item: dict[str, Any], tags: list[str]) -> bool:
        if self.should_raise is not None:
            raise self.should_raise
        self.add_tags_calls.append((str(item.get("key") or ""), list(tags)))
        return True


# ─── Fixtures ─────────────────────────────────────────────────────────────


_MIN_TAXONOMY = {
    "version": 1,
    "status": "customized",
    "tema": [
        {"id": "macro-fiscal", "description": "Fiscal", "synonyms": ["fiscal"]},
        {"id": "mercado-laboral", "description": "Labor", "synonyms": ["labor"]},
        {"id": "informalidad", "description": "Informality", "synonyms": []},
    ],
    "metodo": [
        {"id": "empirico-obs", "description": "Observational", "synonyms": []},
        {"id": "empirico-rct", "description": "RCT", "synonyms": []},
    ],
}


def _write_taxonomy(path: Path, *, status: str = "customized") -> Path:
    data = dict(_MIN_TAXONOMY)
    data["status"] = status
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        paths=PathSettings(
            state_db=tmp_path / "state.db",
            reports_folder=tmp_path / "reports",
            staging_folder=tmp_path / "staging",
            pdf_source_folders=[],
        ),
        zotero=ZoteroSettings(library_id="123", library_type="user", local_api=True),
        openai=OpenAISettings(),
        budgets=BudgetSettings(max_cost_usd_stage_05=0.10),
    )


def _seed_item(
    settings: Settings,
    *,
    sha: str,
    zotero_key: str,
    metadata: dict[str, Any] | None,
    stage_completed: int = 4,
    in_quarantine: bool = False,
    tags_json: str | None = None,
) -> None:
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    with Session(engine) as session:
        session.add(
            Item(
                id=sha,
                source_path=f"/data/{sha}.pdf",
                size_bytes=4096,
                has_text=True,
                classification="academic",
                stage_completed=stage_completed,
                import_route="A",
                zotero_item_key=zotero_key,
                in_quarantine=in_quarantine,
                metadata_json=json.dumps(metadata) if metadata is not None else None,
                tags_json=tags_json,
            )
        )
        session.commit()


# ─── Taxonomy loader ─────────────────────────────────────────────────────


def test_load_taxonomy_template_is_accepted_by_loader(tmp_path: Path) -> None:
    """The loader itself doesn't gate on status — the stage does.

    Enables tests / tools that inspect taxonomy structure to run on the
    shipped template without flipping the marker.
    """
    path = _write_taxonomy(tmp_path / "taxonomy.yaml", status="template")
    tax = load_taxonomy(path)
    assert isinstance(tax, Taxonomy)
    assert tax.status == "template"
    assert "macro-fiscal" in tax.tema_ids


def test_load_taxonomy_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(StageAbortedError, match="not found"):
        load_taxonomy(tmp_path / "nope.yaml")


def test_load_taxonomy_raises_on_bad_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(": : :\n", encoding="utf-8")
    with pytest.raises(StageAbortedError, match="parse error"):
        load_taxonomy(bad)


def test_load_taxonomy_raises_on_unknown_status(tmp_path: Path) -> None:
    path = tmp_path / "taxo.yaml"
    data = dict(_MIN_TAXONOMY)
    data["status"] = "draft"  # not in the Literal
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(StageAbortedError, match="schema error"):
        load_taxonomy(path)


# ─── Stage 05: template refusal + override ───────────────────────────────


def test_stage_refuses_template_without_override(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="template")
    with pytest.raises(StageAbortedError, match="status=template"):
        run_tag(
            preview=True,
            apply=False,
            settings=settings,
            taxonomy_path=path,
            openai_client=FakeOpenAIClient(queue=[]),
            zotero_client=FakeZoteroClient(),  # type: ignore[arg-type]
        )


def test_stage_preview_runs_with_template_override(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="template")
    _seed_item(
        settings,
        sha="a" * 64,
        zotero_key="ZKEY01",
        metadata={"title": "Fiscal multipliers"},
    )
    llm = FakeOpenAIClient(
        queue=[json.dumps({"tema": ["macro-fiscal"], "metodo": ["empirico-obs"]})]
    )
    zot = FakeZoteroClient()
    result = run_tag(
        preview=True,
        apply=False,
        allow_template_taxonomy=True,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert result.items_previewed == 1
    # Preview mode: CSV exists, Zotero untouched, DB unchanged.
    assert result.csv_path.exists()
    assert zot.add_tags_calls == []
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.tags_json is None
    assert item.stage_completed == 4


# ─── Stage 05: apply happy path ──────────────────────────────────────────


def test_stage_apply_tags_item_in_zotero_and_db(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    _seed_item(
        settings,
        sha="b" * 64,
        zotero_key="ZKEY02",
        metadata={"title": "Informal economy in LATAM"},
    )
    llm = FakeOpenAIClient(
        queue=[
            json.dumps(
                {"tema": ["informalidad", "mercado-laboral"], "metodo": ["empirico-obs"]}
            )
        ]
    )
    zot = FakeZoteroClient()
    result = run_tag(
        preview=False,
        apply=True,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert result.items_tagged == 1
    assert zot.add_tags_calls == [
        ("ZKEY02", ["informalidad", "mercado-laboral", "empirico-obs"])
    ]
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 5
    assert item.tags_json is not None
    persisted = json.loads(item.tags_json)
    assert persisted["tema"] == ["informalidad", "mercado-laboral"]
    assert persisted["metodo"] == ["empirico-obs"]


# ─── Stage 05: dry-run short-circuits writes ─────────────────────────────


def test_stage_apply_with_dry_run_writes_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    _seed_item(settings, sha="c" * 64, zotero_key="ZKEY03", metadata={"title": "X"})
    llm = FakeOpenAIClient(
        queue=[json.dumps({"tema": ["macro-fiscal"], "metodo": ["empirico-obs"]})]
    )
    zot = FakeZoteroClient()
    result = run_tag(
        preview=False,
        apply=True,
        dry_run=True,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert [r.status for r in result.rows] == ["dry_run"]
    assert zot.add_tags_calls == []
    assert "_dryrun" in result.csv_path.name
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 4
    assert item.tags_json is None


# ─── Stage 05: strict validation drops unknown ids ───────────────────────


def test_stage_drops_unknown_tags(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    _seed_item(settings, sha="d" * 64, zotero_key="ZKEY04", metadata={"title": "X"})
    llm = FakeOpenAIClient(
        queue=[
            json.dumps(
                {
                    "tema": ["macro-fiscal", "hallucinated-tag"],
                    "metodo": ["empirico-obs", "another-ghost"],
                }
            )
        ]
    )
    zot = FakeZoteroClient()
    result = run_tag(
        preview=False,
        apply=True,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert result.items_tagged == 1
    # Only the taxonomy-matching tags reached Zotero.
    assert zot.add_tags_calls == [("ZKEY04", ["macro-fiscal", "empirico-obs"])]
    # Rejected ids still land in the CSV so the user can spot them.
    assert result.rows[0].tema_rejected == ["hallucinated-tag"]
    assert result.rows[0].metodo_rejected == ["another-ghost"]


# ─── Stage 05: malformed JSON → retry once ───────────────────────────────


def test_stage_retries_once_on_malformed_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    _seed_item(settings, sha="e" * 64, zotero_key="ZKEY05", metadata={"title": "X"})
    llm = FakeOpenAIClient(
        queue=[
            "garbage {{ not json",
            json.dumps({"tema": ["macro-fiscal"], "metodo": ["empirico-rct"]}),
        ]
    )
    zot = FakeZoteroClient()
    result = run_tag(
        preview=False,
        apply=True,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert result.items_tagged == 1
    assert len(llm.tag_calls) == 2, "Second attempt should have fired"


def test_stage_llm_failure_after_retries(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    _seed_item(settings, sha="f" * 64, zotero_key="ZKEY06", metadata={"title": "X"})
    llm = FakeOpenAIClient(queue=["bad", "also bad"])
    zot = FakeZoteroClient()
    result = run_tag(
        preview=False,
        apply=True,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert result.items_llm_failed == 1
    assert result.items_tagged == 0
    assert zot.add_tags_calls == []


# ─── Stage 05: eligibility filters ───────────────────────────────────────


def test_stage_skips_quarantined_items(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    _seed_item(
        settings,
        sha="g" * 64,
        zotero_key="ZKEY07",
        metadata={"title": "Quarantined paper"},
        in_quarantine=True,
    )
    llm = FakeOpenAIClient()  # no queue
    zot = FakeZoteroClient()
    result = run_tag(
        preview=True,
        apply=False,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert result.items_processed == 0
    assert len(result.rows) == 0
    assert llm.tag_calls == []


def test_stage_skips_already_tagged_without_re_tag_flag(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    _seed_item(
        settings,
        sha="h" * 64,
        zotero_key="ZKEY08",
        metadata={"title": "Already tagged"},
        tags_json=json.dumps({"tema": ["x"], "metodo": ["y"]}),
    )
    llm = FakeOpenAIClient()  # no queue
    zot = FakeZoteroClient()
    result = run_tag(
        preview=True,
        apply=False,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert result.items_processed == 0
    assert llm.tag_calls == []


def test_stage_re_tag_includes_already_tagged(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    _seed_item(
        settings,
        sha="i" * 64,
        zotero_key="ZKEY09",
        metadata={"title": "Already tagged"},
        tags_json=json.dumps({"tema": ["old"], "metodo": ["old"]}),
    )
    llm = FakeOpenAIClient(
        queue=[json.dumps({"tema": ["macro-fiscal"], "metodo": ["empirico-obs"]})]
    )
    zot = FakeZoteroClient()
    result = run_tag(
        preview=False,
        apply=True,
        re_tag=True,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert result.items_tagged == 1


# ─── Stage 05: no metadata short-circuit ─────────────────────────────────


def test_stage_skips_items_without_metadata(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    _seed_item(settings, sha="j" * 64, zotero_key="ZKEY10", metadata=None)
    llm = FakeOpenAIClient()
    zot = FakeZoteroClient()
    result = run_tag(
        preview=False,
        apply=True,
        settings=settings,
        taxonomy_path=path,
        openai_client=llm,  # type: ignore[arg-type]
        zotero_client=zot,  # type: ignore[arg-type]
    )
    assert result.items_no_metadata == 1
    assert llm.tag_calls == []
    assert zot.add_tags_calls == []


# ─── Stage 05: budget exceeded aborts cleanly ────────────────────────────


def test_stage_budget_exceeded_aborts_with_partial_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _write_taxonomy(tmp_path / "taxo.yaml", status="customized")
    # Two eligible items; the second trips the budget.
    _seed_item(settings, sha="k" * 64, zotero_key="ZKEY11", metadata={"title": "A"})
    _seed_item(settings, sha="l" * 64, zotero_key="ZKEY12", metadata={"title": "B"})
    llm = FakeOpenAIClient(
        queue=[
            json.dumps({"tema": ["macro-fiscal"], "metodo": ["empirico-obs"]}),
            BudgetExceededError("cap tripped"),
        ]
    )
    zot = FakeZoteroClient()
    with pytest.raises(StageAbortedError, match="budget exceeded"):
        run_tag(
            preview=False,
            apply=True,
            settings=settings,
            taxonomy_path=path,
            openai_client=llm,  # type: ignore[arg-type]
            zotero_client=zot,  # type: ignore[arg-type]
        )
    # First item was committed before the second tripped; enforced by the
    # commit-in-finally pattern so partial state is durable.
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        items = {item.id: item for item in session.exec(select(Item))}
    assert items["k" * 64].stage_completed == 5
    assert items["k" * 64].tags_json is not None
    assert items["l" * 64].stage_completed == 4
    assert items["l" * 64].tags_json is None


# ─── Stage 05: --preview and --apply together is rejected ────────────────


def test_stage_rejects_both_preview_and_apply(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(StageAbortedError, match="exactly one"):
        run_tag(preview=True, apply=True, settings=settings)
