"""Thin wrapper around pyzotero that honours the global `--dry-run` flag.

Only the methods actually used by S1/S2 are surfaced. We avoid re-exporting
pyzotero's full API so the type surface stays small and mypy-friendly.
"""

from __future__ import annotations

from typing import Any, cast

from pyzotero import zotero

from zotai.utils.logging import get_logger

log = get_logger(__name__)


class ZoteroClient:
    """Facade over pyzotero's `Zotero` object.

    When `dry_run=True`, every mutating call is short-circuited: the wrapper
    logs what it *would* have done and returns a deterministic placeholder so
    callers don't need branchy code. Read calls are always real.
    """

    def __init__(
        self,
        *,
        library_id: str,
        library_type: str = "user",
        api_key: str,
        local: bool = True,
        local_api_host: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._client = zotero.Zotero(
            library_id=library_id,
            library_type=library_type,
            api_key=api_key,
            local=local,
        )
        if local and local_api_host:
            # pyzotero hardcodes ``self.endpoint = "http://localhost:23119/api"``
            # when ``local=True`` (see ``pyzotero.zotero.Zotero.__init__``).
            # Inside a bridge-mode Docker container, ``localhost`` resolves to
            # the container itself — Zotero Desktop's local API lives on the
            # host and must be reached via ``host.docker.internal`` (wired via
            # Compose's ``extra_hosts: host-gateway``). Override the endpoint
            # so the same pyzotero calls work transparently. See ADR 013.
            self._client.endpoint = f"{local_api_host.rstrip('/')}/api"
        self.dry_run = dry_run

    # ─── Reads ────────────────────────────────────────────────────────────

    def items(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Proxy to `pyzotero.Zotero.items(**kwargs)`."""
        return cast(list[dict[str, Any]], self._client.items(**kwargs))

    def collections(self, **kwargs: Any) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._client.collections(**kwargs))

    def item(self, item_key: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._client.item(item_key))

    def children(
        self, item_key: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Proxy to ``pyzotero.Zotero.children(item_key, **kwargs)``.

        Used by Stage 03's dedup path to decide whether an existing
        Zotero item already has a PDF attachment before we add ours
        (ADR 014).
        """
        return cast(
            list[dict[str, Any]], self._client.children(item_key, **kwargs)
        )

    # ─── Writes — all respect dry_run ─────────────────────────────────────

    def create_items(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Create items; returns pyzotero's `{success, unchanged, failed}` shape."""
        if self.dry_run:
            log.info("zotero.create_items.dry_run", count=len(items))
            return {"success": {}, "unchanged": {}, "failed": {}}
        return cast(dict[str, Any], self._client.create_items(items))

    def update_item(self, item: dict[str, Any]) -> bool:
        if self.dry_run:
            log.info("zotero.update_item.dry_run", item_key=item.get("key"))
            return True
        return bool(self._client.update_item(item))

    def attachment_simple(
        self, paths: list[str], parent_key: str | None = None
    ) -> dict[str, Any]:
        if self.dry_run:
            log.info(
                "zotero.attachment_simple.dry_run", paths=paths, parent=parent_key
            )
            return {"success": {}, "unchanged": {}, "failed": {}}
        if parent_key is not None:
            return cast(
                dict[str, Any],
                self._client.attachment_simple(paths, parent_key),
            )
        return cast(dict[str, Any], self._client.attachment_simple(paths))

    def add_tags(self, item: dict[str, Any], tags: list[str]) -> bool:
        if self.dry_run:
            log.info(
                "zotero.add_tags.dry_run", item_key=item.get("key"), tags=tags
            )
            return True
        return bool(self._client.add_tags(item, *tags))


__all__ = ["ZoteroClient"]
