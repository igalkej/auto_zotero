# ADR 009 — `zotero-mcp` is consumed by S3 only; S1 and S2 use `pyzotero` directly

**Status**: Accepted
**Date**: 2026-04-22
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

The project has two conceptual clients for Zotero:

- **`pyzotero`** — a Python SDK around Zotero's data API
  (`/users/<id>/items`, `/users/<id>/collections`, etc.). Synchronous,
  documented, exhaustive over the data surface. Already the choice in
  `src/zotai/api/zotero.py` (thin wrapper that respects `--dry-run`).
- **`zotero-mcp`** — an MCP server that speaks to Zotero internally and
  exposes `zotero_search`, `zotero_semantic_search`, `zotero_fulltext`,
  `zotero_item_details`, `zotero_pdf_annotations` as tool calls over
  stdio (ADR 006). Primarily designed for Claude Desktop.

All three subsystems could, in principle, use either client. S1
(retrospective capture) and S2 (prospective capture) are batch pipelines
that write and update Zotero items en masse. S3 is a conversational
interface that reads Zotero during a user's chat session.

The question is whether S1 and S2 should also go through `zotero-mcp` —
for example, using its `zotero_search` tool to check whether a DOI is
already in the library before Stage 03 creates a duplicate — or keep
using `pyzotero` directly.

## Decision

**S1 and S2 use `pyzotero` directly. `zotero-mcp` is the S3-only
surface. No subsystem other than S3 imports or invokes `zotero-mcp`.**

Concretely:

- `src/zotai/api/zotero.py` (`ZoteroClient`) wraps `pyzotero.Zotero` and
  is the single Zotero client for S1 and S2.
- `zotero-mcp` is installed on the host via `uv tool install
  "zotero-mcp-server[semantic]"` (plan_03 §5) and only Claude Desktop
  talks to it, over stdio.
- If a future S1/S2 feature would benefit from semantic search against
  the library (e.g. enrichment step that asks "is a paper on this topic
  already in the library?"), the implementation reads ChromaDB
  directly via the bind mount in ADR 011 — not through `zotero-mcp`.
  The bind mount is the agreed shared surface, not the MCP tool set.

## Consequences

### Positive

- **Right tool for the job on each side.** Batch pipelines want
  imperative calls with predictable types, not tool-call round-trips
  through an MCP transport. Conversational discovery wants tool calls
  with semantic search baked in. Splitting on that line keeps each
  side idiomatic.
- **No MCP transport in the hot path of S1/S2.** MCP is stdio-framed
  JSON-RPC; going through it adds a Python subprocess per request, a
  tool-schema validation, and a message framer. For 1000 PDFs in S1
  Stage 03 that is measurable latency and a new failure mode (MCP
  server crash in the middle of an import). `pyzotero` is just HTTP.
- **S3's evolution does not affect S1/S2.** If `zotero-mcp` renames a
  tool, changes a return shape, or bumps its MCP protocol version, S1
  and S2 are untouched. The blast radius of an upstream change is
  exactly what the change targets: Claude Desktop.
- **Auth / key paths stay uniform.** `pyzotero` reads
  `ZOTERO_API_KEY` + `ZOTERO_LIBRARY_ID` from our
  `pydantic-settings` group (see `src/zotai/config.py`). Routing S1/S2
  through `zotero-mcp` would mean a second set of credentials lives in
  `zotero-mcp`'s own config, or would require us to relay credentials
  through MCP — both worse than today.
- **Testing is easier.** `pyzotero` mocks via `respx` / `httpx_mock`
  on the HTTP layer. Mocking an MCP server in tests would require a
  fixture process or a hand-rolled transport stub.

### Negative

- **Two clients of Zotero to reason about.** The team has to remember
  which uses which. Mitigation: this ADR + a one-line reminder in
  `docs/plan_00_overview.md` §5 table row 9.
- **Slightly redundant feature surfaces.** Both clients can "search
  Zotero for a DOI"; we implement that in S1's `_find_existing_doi`
  via pyzotero rather than invoking `zotero_search`. That is a
  duplicated concept in the codebase, even though the implementations
  differ. Worth it for the reasons above.

### Neutral

- **Reversible for a specific feature.** If S2 ever needs a feature
  that `zotero-mcp` uniquely offers (say, an annotation-aware read
  path), the boundary can move for that one feature. The ADR is not
  "pyzotero forever"; it is "MCP is not a pipeline transport for us".

## Alternatives considered

**A. Use `zotero-mcp` everywhere.**
Rejected. Adds a subprocess, a transport, and a tool-schema layer to
the middle of a batch pipeline that already has five API clients and a
SQLite DB. For 1000 PDFs in Stage 03 the overhead is noticeable and
the new failure modes (MCP process crash, tool schema drift) add work
we do not get value back from.

**B. Use `pyzotero` everywhere, including inside a custom S3 server.**
Rejected via ADR 006 already: building a custom S3 server is 5-10
days of work we actively decline. `pyzotero` in S3's role would also
lack semantic search and fulltext indexing, which are table-stakes
features for discovery mode.

**C. Abstract both behind a project-internal `ZoteroBackend` interface
that each subsystem picks from.**
Rejected. A second abstraction layer with no consumers that need to
swap at runtime. YAGNI. When a concrete need shows up (say, a mode
that runs without Claude Desktop), a narrower abstraction beats a
speculative one.

**D. Use `zotero-mcp`'s `zotero_semantic_search` from S2 instead of
reading ChromaDB via the bind mount (ADR 011).**
Rejected. The bind mount gives S2 read access to the *data* the MCP
tool is backed by. Going through the MCP tool adds a tool-schema
validation + transport, and couples S2 to the MCP tool's return shape
(which can change). Reading the store directly gives S2 a stable,
documented ChromaDB Python API, and is precisely what ADR 011
specifies.

## References

- `docs/plan_00_overview.md` §5 — ADR 006, 009 row in decisions table
- `docs/decisions/006-zotero-mcp-external-dependency.md` — the
  decision that this ADR scopes
- `docs/decisions/011-chromadb-bind-mount.md` — how S2 reaches S3's
  index without going through `zotero-mcp`
- `src/zotai/api/zotero.py` — the S1/S2 client: `pyzotero`-only
- `docs/plan_03_subsystem3.md` §4.1 — `zotero-mcp` tools, scoped to S3
