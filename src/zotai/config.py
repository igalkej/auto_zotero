"""Application settings — pydantic-settings sourced from `.env` + environment.

All settings groups inherit from `BaseSettings` with their own `env_prefix`, so
each group reads only the variables that logically belong to it. The outer
`Settings` composes them via `default_factory` so env reading happens at
instantiation time, not at import time.

Usage:

    from zotai.config import Settings

    settings = Settings()
    print(settings.zotero.library_id)
    print(settings.paths.state_db)

The entire object is immutable (``frozen=True``). For tests that need to
override values, build a fresh `Settings(**overrides)` rather than mutating.

See ``docs/plan_01_subsystem1.md`` §5 and ``docs/plan_02_subsystem2.md`` §12
for the full variable list, and ``.env.example`` at the repo root for a
copy-paste starter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = ".env"
_ENV_ENCODING = "utf-8"


class _GroupBase(BaseSettings):
    """Shared config for each prefixed group. Subclasses override ``env_prefix``."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )


class ZoteroSettings(_GroupBase):
    model_config = SettingsConfigDict(
        env_prefix="ZOTERO_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )
    api_key: SecretStr = SecretStr("")
    library_id: str = ""
    library_type: Literal["user", "group"] = "user"
    local_api: bool = True


class OpenAISettings(_GroupBase):
    model_config = SettingsConfigDict(
        env_prefix="OPENAI_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )
    api_key: SecretStr = SecretStr("")
    model_tag: str = "gpt-4o-mini"
    model_extract: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-large"


class SemanticScholarSettings(_GroupBase):
    model_config = SettingsConfigDict(
        env_prefix="SEMANTIC_SCHOLAR_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )
    api_key: SecretStr = SecretStr("")


class OcrSettings(_GroupBase):
    model_config = SettingsConfigDict(
        env_prefix="OCR_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )
    languages: str = "spa+eng"
    parallel_processes: int = 4

    @field_validator("parallel_processes")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("OCR_PARALLEL_PROCESSES must be >= 1")
        return v


class PathSettings(_GroupBase):
    """Filesystem locations. All resolved to absolute paths on load."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )
    pdf_source_folders: list[Path] = Field(default_factory=list)
    staging_folder: Path = Path("/workspace/staging")
    state_db: Path = Path("/workspace/state.db")
    reports_folder: Path = Path("/workspace/reports")

    @field_validator("pdf_source_folders", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [Path(s.strip()) for s in v.split(",") if s.strip()]
        return v


class BudgetSettings(_GroupBase):
    """Hard spending limits enforced by the API clients."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )
    max_cost_usd_total: float = 10.0
    max_cost_usd_stage_04: float = 2.0
    max_cost_usd_stage_05: float = 1.0


class BehaviorSettings(_GroupBase):
    """Cross-cutting runtime behaviour."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )
    dry_run: bool = False
    log_level: str = "INFO"
    user_email: str = ""


class S2Settings(_GroupBase):
    """Subsystem 2 — worker, dashboard, and PDF-cascade config."""

    model_config = SettingsConfigDict(
        env_prefix="S2_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )
    fetch_interval_hours: int = 6
    candidates_db: Path = Path("/workspace/candidates.db")
    chroma_path: Path = Path("/workspace/chroma_db")
    zotero_inbox_collection: str = "Inbox S2"
    pdf_sources: list[str] = Field(
        default_factory=lambda: ["openaccess", "doi", "annas", "libgen", "scihub", "rss"]
    )
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000
    max_cost_usd_daily: float = 0.50
    max_cost_usd_monthly: float = 10.0

    @field_validator("pdf_sources", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip().lower() for s in v.split(",") if s.strip()]
        return v

    @field_validator("fetch_interval_hours", "dashboard_port")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("value must be >= 1")
        return v


class Settings(BaseSettings):
    """Top-level settings aggregating all groups.

    Each group reads its own prefixed env vars at instantiation time.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    zotero: Annotated[ZoteroSettings, Field(default_factory=ZoteroSettings)]
    openai: Annotated[OpenAISettings, Field(default_factory=OpenAISettings)]
    semantic_scholar: Annotated[
        SemanticScholarSettings, Field(default_factory=SemanticScholarSettings)
    ]
    ocr: Annotated[OcrSettings, Field(default_factory=OcrSettings)]
    paths: Annotated[PathSettings, Field(default_factory=PathSettings)]
    budgets: Annotated[BudgetSettings, Field(default_factory=BudgetSettings)]
    behavior: Annotated[BehaviorSettings, Field(default_factory=BehaviorSettings)]
    s2: Annotated[S2Settings, Field(default_factory=S2Settings)]


__all__ = [
    "BehaviorSettings",
    "BudgetSettings",
    "OcrSettings",
    "OpenAISettings",
    "PathSettings",
    "S2Settings",
    "SemanticScholarSettings",
    "Settings",
    "ZoteroSettings",
]
