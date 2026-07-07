"""CLI for maintaining the documentation knowledge base.

Commands:

  rebuild   Load resources/docs/entries/*.json, re-embed any entries whose
            content has changed, and write resources/docs/.embeddings.json.
            Run this after editing any entry JSON file.

Invoke via the project script (`avni-docs-kb rebuild`) or directly
(`python -m domain.docs.kb_cli rebuild`). See specs/AVNI_DOCS_KB_SDD.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import config  # noqa: F401 — imported for the .env-loading side effect
from domain.docs.knowledge_base import DocsKnowledgeBase

log = logging.getLogger("docs.kb")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS_ROOT = _REPO_ROOT / "resources" / "docs"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="avni-docs-kb", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rebuild = sub.add_parser("rebuild", help="Re-embed changed entries and write the cache.")
    p_rebuild.add_argument("--docs-root", type=Path, default=_DOCS_ROOT,
                           help="Override resources/docs/ root.")

    args = parser.parse_args(argv)

    if args.cmd == "rebuild":
        kb = DocsKnowledgeBase(root=args.docs_root)
        kb.rebuild()

    return 0


if __name__ == "__main__":
    sys.exit(main())
