# ADR 006 — Use `zotero-mcp` (54yyyu) as the S3 MCP server

**Status**: Accepted
**Date**: 2026-04-22
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

S3's job is to expose the researcher's Zotero library to Claude Desktop
via the Model Context Protocol (MCP), covering three query modes named
in plan_03 §1: descubrimiento interno (~60%), cita de respaldo (~30%),
síntesis puntual (~10%).

To do that, we need a process that:

1. Speaks MCP (stdio transport, the flavour Claude Desktop uses).
2. Reads from Zotero — metadata, tags, collections, PDF text.
3. Supports semantic search (embeddings + a vector store) because
   keyword-only discovery fails the recall criterion in plan_03 §2.
4. Handles PDF annotations / highlights, which the user already has on
   papers they have read.
5. Runs on the host (not in our Docker container — Claude Desktop talks
   to a local MCP server via stdio, so the server lives alongside
   Claude Desktop on the user's machine).

The MCP ecosystem has existed for a short but not trivial time and a
Zotero-specific MCP server already exists:
[`54yyyu/zotero-mcp`](https://github.com/54yyyu/zotero-mcp). It
implements all five of the above, uses ChromaDB for the vector index,
supports configurable embedders (including OpenAI, ADR 004), indexes
fulltext via `zotero-mcp update-db --fulltext`, and surfaces
`zotero_search`, `zotero_semantic_search`, `zotero_fulltext`,
`zotero_item_details`, and `zotero_pdf_annotations` as MCP tools.

Two questions follow: do we adopt it as-is, fork it, or build our own?

## Decision

**Adopt `zotero-mcp` as the upstream S3 server. Do not fork. Do not
build a custom MCP server.** S3 becomes, in practice, a setup guide,
a small validation script, and the configuration glue between
Claude Desktop and the researcher's Zotero install.

Concretely:

- `plan_03_subsystem3.md` §5 (the install steps) is the canonical S3
  deliverable, not code.
- `scripts/validate-s3.py` (plan_03 §5.4) is our one piece of code in
  S3: runs 10 known-answer queries against the live MCP server and
  reports recall@20 / latency / errors. If it ever fails on a
  `zotero-mcp` upgrade, the failure shows up immediately.
- `docs/s3-setup.md`, `docs/s3-usage.md`, `docs/s3-troubleshooting.md`
  (Phase 10, #11) reference `zotero-mcp`'s commands verbatim rather
  than wrapping them in our own.
- When a deficiency shows up, the first move is to file an upstream
  issue or PR against `zotero-mcp`. Forking is reserved for the case
  where upstream is unresponsive *and* the feature is load-bearing for
  one of our criterios de éxito.

## Consequences

### Positive

- **Zero upstream development cost.** Building a comparable MCP server
  ourselves is an estimated 5-10 days: MCP transport, five tools, two
  client libraries (Zotero API + a vector store), fulltext extraction,
  annotations. All of that is already done.
- **Battle-tested semantic search.** `zotero-mcp`'s ChromaDB wiring,
  incremental `update-db`, `--fulltext` mode, and embedding-provider
  pluggability are features we would otherwise have to design. Copying
  them right would not save time; copying them slightly wrong would
  bleed recall silently.
- **MCP protocol tracking is free.** MCP is young and the transport /
  tool-schema spec is still moving. Being downstream of `zotero-mcp`
  means protocol updates land via `uv tool upgrade zotero-mcp-server`,
  not in our sprint plan.
- **Clear S3 scope for the project.** Because S3 is "install +
  configure + validate", plan_03's estimate (~4-6 h of development)
  is actually achievable. Without an external server, plan_03 would
  balloon to a multi-week subsystem that competes for attention with
  S1 and S2, both of which are higher-leverage for the researcher.
- **Embedder choice cleanly factored.** ADR 004 picks the embedder;
  `zotero-mcp` reads it from env. The decisions do not entangle.

### Negative

- **Roadmap dependency.** If `zotero-mcp` changes tool names,
  semantics, or default behavior in an upgrade, the researcher's
  prompts and our `validate-s3.py` can break. Mitigation: pin a
  specific version of `zotero-mcp-server` in the install doc
  (`uv tool install "zotero-mcp-server[semantic]==X.Y.Z"`), and
  upgrade deliberately after running `validate-s3.py` against the new
  version.
- **Cannot add Zotero-side features without upstream cooperation.**
  Example: if we later want an `zotero_tag_suggest` tool that uses the
  taxonomy in `config/taxonomy.yaml`, we either land it upstream or
  add a second MCP server alongside. We live with this trade-off —
  it has not come up in any criterion de éxito.
- **Documentation maintenance.** Our setup guide mirrors upstream's
  README. When upstream's commands change, ours must change too.
  Mitigation: `validate-s3.py` catches breakage; `docs/s3-setup.md` is
  short enough that diffing on upstream releases is cheap.
- **Host-side Python dependency.** `zotero-mcp` installs via
  `uv tool install`, which puts it on the host (not inside Docker).
  That is inherent to how Claude Desktop connects (stdio MCP server,
  local process). The researcher needs host Python 3.11 available. ADR
  001 already absorbs this: S3 is the one subsystem that is not
  Docker-first, precisely because Claude Desktop lives on the host.

### Neutral

- **Fork path is not closed.** If upstream goes silent for a quarter or
  refuses a change that our criterio de éxito genuinely needs, forking
  is a ~2-3 day project (Python package rename, CI, docs) and ADR 006
  would be superseded by an ADR documenting the fork.

## Alternatives considered

**A. Build a custom MCP server (pure Python, pyzotero + ChromaDB +
MCP SDK).**
Rejected. The 5-10 days it would take compete directly with S1 Stage
04 enrichment and the S2 sprint plan — both higher leverage. The MCP
protocol is still shifting; inheriting `zotero-mcp`'s tracking of
those changes is a free benefit we give up by building our own.

**B. Fork `zotero-mcp` now to customize behavior.**
Rejected. Premature — there is no specific feature gap yet. Forking
doubles the maintenance surface for no present benefit. Revisit only
when a concrete blocker lands.

**C. Use a generic HTTP-to-MCP bridge over Zotero's web API.**
Rejected. Generic bridges do not provide semantic search (no vector
store, no embedder pipeline), which is the whole point of discovery
mode for a Spanish-dominant corpus (see ADR 004). Recall@20 would
collapse.

**D. Skip S3 entirely; use Zotero's own search from Claude Desktop
manually.**
Rejected. Claude Desktop cannot "just use" Zotero without MCP — there
is no conversational surface on Zotero's API. The alternative is the
user copy-pasting between Zotero and Claude, which defeats the
"multiplicar por 3-5× las consultas bibliográficas" goal.

## References

- `docs/plan_03_subsystem3.md` — the subsystem this ADR anchors
- `docs/plan_00_overview.md` §4, §5 — S1 → S3 → S2 order and the
  "cierra MVP" argument that only works if S3 is cheap
- `docs/decisions/004-openai-text-embedding-3-large.md` — the
  embedder choice that this ADR lets us make cleanly
- `docs/decisions/009-zotero-mcp-not-used-by-s1-s2.md` — the
  companion decision that scopes where `zotero-mcp` is and is not used
- Upstream: https://github.com/54yyyu/zotero-mcp
