"""Tests for :class:`zotai.api.zotero.ZoteroClient`.

The wrapper is thin; what matters here is the ``local_api_host``
override introduced with ADR 013 — pyzotero hardcodes
``http://localhost:23119/api`` when ``local=True``, and we need it to
point at ``host.docker.internal`` when running in a bridge-mode Docker
container.
"""

from __future__ import annotations

from zotai.api.zotero import ZoteroClient

_FAKE_KEY = "deadbeef"
_FAKE_LIB = "999"


def test_local_client_uses_pyzotero_default_when_host_empty() -> None:
    client = ZoteroClient(
        library_id=_FAKE_LIB,
        api_key=_FAKE_KEY,
        local=True,
        local_api_host=None,
    )
    # pyzotero's own hardcoded endpoint when local=True.
    assert client._client.endpoint == "http://localhost:23119/api"


def test_local_client_honours_host_override() -> None:
    client = ZoteroClient(
        library_id=_FAKE_LIB,
        api_key=_FAKE_KEY,
        local=True,
        local_api_host="http://host.docker.internal:23119",
    )
    assert client._client.endpoint == "http://host.docker.internal:23119/api"


def test_host_override_strips_trailing_slash() -> None:
    client = ZoteroClient(
        library_id=_FAKE_LIB,
        api_key=_FAKE_KEY,
        local=True,
        local_api_host="http://host.docker.internal:23119/",
    )
    assert client._client.endpoint == "http://host.docker.internal:23119/api"


def test_host_override_ignored_when_local_false() -> None:
    # When ``local=False`` pyzotero points at the web API; the override
    # must not fire (we'd be aiming at the wrong endpoint entirely).
    client = ZoteroClient(
        library_id=_FAKE_LIB,
        api_key=_FAKE_KEY,
        local=False,
        local_api_host="http://host.docker.internal:23119",
    )
    assert client._client.endpoint == "https://api.zotero.org"
