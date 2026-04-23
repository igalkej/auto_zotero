#!/usr/bin/env python3
"""Populate a ChromaDB with the schema ADR 015 §6 prescribes and print
the resulting state for manual validation against `zotero-mcp serve`.

This script is Step 2.1 of the Fase 2 validation required by ADR 015.
It does NOT run `zotero-mcp serve` itself; that step is manual and is
tracked in `docs/decisions/015-validation-checklist.md`. What this
script does is write a ChromaDB that the user can then point
`zotero-mcp serve` at to verify that the server tolerates the schema
S2 will produce in Fase 3.

Usage:

    python scripts/validate_chromadb_schema.py [--path DIR] \
        [--collection-name zotero_library] [--num-items 5]

The default path is a fresh temp directory; pass `--path` to write
into `~/.config/zotero-mcp/chroma_db/` (after backing that up) and
then run `zotero-mcp serve` against the same location.

**Requires** `OPENAI_API_KEY` in the environment (real embeddings are
requested so the store is equivalent to what S2 will produce).

**Collection name assumption.** ADR 015 §6 prescribes `zotero_library`
as the collection name. This matches `zotero-mcp`'s likely default,
but the ADR §6 itself flags this as "verificar contra el default de
zotero-mcp". If the checklist (`docs/decisions/015-validation-checklist.md`)
finds a different name in a real `zotero-mcp` install, override via
`--collection-name` and update ADR 015 §6 accordingly.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


# ─── Synthetic items ───────────────────────────────────────────────────────

_SYNTHETIC_ITEMS: list[dict[str, Any]] = [
    {
        "title": "Fiscal multipliers in emerging economies",
        "abstract": (
            "We estimate the effect of government spending shocks on output "
            "in a panel of emerging-market economies using local projections "
            "and quarterly data from 1990-2019."
        ),
        "year": 2020,
        "item_type": "journalArticle",
        "doi": "10.1111/fake-multipliers-em-2020",
    },
    {
        "title": "Informalidad laboral y productividad en América Latina",
        "abstract": (
            "Este trabajo examina la relación entre la tasa de informalidad "
            "y la productividad total de los factores usando datos de hogares "
            "de diez países latinoamericanos entre 2000 y 2018."
        ),
        "year": 2019,
        "item_type": "journalArticle",
        "doi": "10.1111/fake-informalidad-latam-2019",
    },
    {
        "title": "Climate change mitigation in tropical forest economies",
        "abstract": (
            "We compare policy instruments — carbon taxes, REDD+ payments, "
            "and direct land-use regulation — in a small open economy model "
            "calibrated to three Amazon-basin countries."
        ),
        "year": 2022,
        "item_type": "journalArticle",
        "doi": "10.1111/fake-climate-tropical-2022",
    },
    {
        "title": "Monetary policy and inflation expectations: evidence from Argentina",
        "abstract": (
            "Using high-frequency survey data, we document how households "
            "update their inflation expectations in response to monetary "
            "policy announcements in a high-inflation regime."
        ),
        "year": 2021,
        "item_type": "journalArticle",
        "doi": "10.1111/fake-inflation-argentina-2021",
    },
    {
        "title": "Trade agreements and firm dynamics",
        "abstract": (
            "We study how preferential trade agreements affect firm entry, "
            "exit, and productivity using administrative firm-level data "
            "from Mexico and Colombia."
        ),
        "year": 2018,
        "item_type": "journalArticle",
        "doi": "10.1111/fake-trade-firms-2018",
    },
]


# ─── Zotero-style key generator ────────────────────────────────────────────


def _zotero_key(rng: random.Random) -> str:
    """Generate an 8-char alphanumeric string matching Zotero's item-key shape.

    Zotero keys use uppercase letters and digits in a base-36 encoding of an
    incremented counter; we just need something shaped the same for a
    schema compatibility test.
    """
    alphabet = string.ascii_uppercase + string.digits
    return "".join(rng.choice(alphabet) for _ in range(8))


# ─── OpenAI embedding ──────────────────────────────────────────────────────


def _embed(texts: list[str], *, model: str) -> list[list[float]]:
    """Call OpenAI's embeddings API and return one vector per input text."""
    from openai import OpenAI  # local import keeps the script lazy

    client = OpenAI()
    response = client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in response.data]


# ─── Main ──────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Populate a ChromaDB with ADR 015 §6 schema for Fase 2 validation.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Directory for the ChromaDB PersistentClient. Default: fresh tempdir.",
    )
    parser.add_argument(
        "--collection-name",
        default="zotero_library",
        help="Collection name (ADR 015 §6 prescribes `zotero_library`; verify "
        "against real zotero-mcp and override here if different).",
    )
    parser.add_argument(
        "--num-items",
        type=int,
        default=5,
        help="How many synthetic items to insert (3-5, capped at the built-in "
        "dataset size).",
    )
    parser.add_argument(
        "--embedding-model",
        default="text-embedding-3-large",
        help="OpenAI model (per ADR 004). Default: text-embedding-3-large.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="RNG seed for reproducible Zotero keys.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "ERROR: OPENAI_API_KEY not set. Export it before running this "
            "script; real embeddings are required for the store to be "
            "equivalent to what S2 will produce.",
            file=sys.stderr,
        )
        return 2

    if args.num_items < 1 or args.num_items > len(_SYNTHETIC_ITEMS):
        print(
            f"ERROR: --num-items must be between 1 and {len(_SYNTHETIC_ITEMS)}.",
            file=sys.stderr,
        )
        return 2

    path = args.path
    owns_tempdir = False
    if path is None:
        tmp = tempfile.mkdtemp(prefix="chroma-adr015-")
        path = Path(tmp)
        owns_tempdir = True
    path.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] Opening ChromaDB at {path}")
    import chromadb  # lazy — optional dep (`pip install 'zotai[s2]'`)

    client = chromadb.PersistentClient(path=str(path))

    print(f"[2/5] Getting-or-creating collection: {args.collection_name!r}")
    collection = client.get_or_create_collection(name=args.collection_name)

    rng = random.Random(args.seed)
    items = _SYNTHETIC_ITEMS[: args.num_items]
    keys = [_zotero_key(rng) for _ in items]

    print(f"[3/5] Embedding {len(items)} texts with {args.embedding_model}")
    texts = [f"{item['title']}. {item['abstract']}" for item in items]
    embeddings = _embed(texts, model=args.embedding_model)

    now_iso = datetime.now(tz=UTC).isoformat()
    metadatas: list[dict[str, Any]] = []
    for item in items:
        metadatas.append(
            {
                "title": item["title"],
                "year": item["year"],
                "item_type": item["item_type"],
                "doi": item["doi"],
                # source = which text fueled the embedding; ADR 015 §6.
                "source": "s2_fulltext",
                "indexed_at": now_iso,
                "source_subsystem": "s2",
            }
        )

    print(f"[4/5] Upserting {len(items)} documents into {args.collection_name!r}")
    collection.upsert(ids=keys, embeddings=embeddings, metadatas=metadatas, documents=texts)

    print("[5/5] Final state:")
    count = collection.count()
    sample = collection.get(limit=1, include=["metadatas", "embeddings"])
    # Chroma returns numpy arrays for embeddings; use len() to avoid serialising.
    sample_meta = sample["metadatas"][0] if sample["metadatas"] else None
    sample_embed = sample["embeddings"][0] if sample["embeddings"] else None
    embed_dim = len(sample_embed) if sample_embed is not None else None

    report = {
        "path": str(path),
        "collection_name": args.collection_name,
        "documents_count": count,
        "embedding_model": args.embedding_model,
        "embedding_dimension": embed_dim,
        "document_ids": keys,
        "sample_metadata": sample_meta,
    }
    print(json.dumps(report, indent=2, default=str))

    if owns_tempdir:
        print(
            f"\nNote: ChromaDB lives in a temp dir at {path}.\n"
            "To test with `zotero-mcp serve`, either point its config at "
            "this path or re-run with `--path ~/.config/zotero-mcp/chroma_db` "
            "(back up any existing store first)."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
