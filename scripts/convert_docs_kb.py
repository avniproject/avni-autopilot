"""One-time conversion of resources/faqs/merged_kb.md into per-topic JSON entries.

Split strategy (adaptive by size):
  - Each H1 section is collected as a single candidate entry.
  - If its token count is within --max-tokens (default 2000), it is written as
    one entry — keeping sequential guides (e.g. Quick Start steps) together.
  - If it exceeds --max-tokens, the section is split at H2 boundaries instead,
    with the H1 title prepended to each H2 title.
  - H3 content is always merged inline into its parent body.
  - Navigation stubs and image-only lines are dropped.
  - Entries with fewer than --min-tokens (default 60) tokens are dropped.

Usage:
    uv run python scripts/convert_docs_kb.py
    uv run python scripts/convert_docs_kb.py --source resources/faqs/merged_kb.md
    uv run python scripts/convert_docs_kb.py --out resources/docs/entries
    uv run python scripts/convert_docs_kb.py --max-tokens 1500

After running: review the output files, edit as needed, then commit them.
Run `avni-docs-kb rebuild` to regenerate the embedding cache.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SOURCE = _REPO_ROOT / "resources" / "faqs" / "merged_kb.md"
_DEFAULT_OUT = _REPO_ROOT / "resources" / "docs" / "entries"

_MIN_TOKENS = 60
_MAX_TOKENS = 2000

_JSX_INLINE_RE = re.compile(r"<(?:Image|br|Anchor)[^>]*/?>", re.IGNORECASE)
_CALLOUT_RE = re.compile(r"<CallOut[^>]*>(.*?)</CallOut>", re.DOTALL | re.IGNORECASE)
_IMAGE_LINE_RE = re.compile(r"^\s*!\[.*?\]\(.*?\)\s*$")
_URL_LINE_RE = re.compile(r"^\s*https?://\S+\s*$")
_HEADING_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:80]


def _clean_line(line: str) -> str:
    if _IMAGE_LINE_RE.match(line) or _URL_LINE_RE.match(line):
        return ""
    line = _CALLOUT_RE.sub(lambda m: m.group(1).strip(), line)
    line = _JSX_INLINE_RE.sub("", line)
    return line


def _token_count(text: str) -> int:
    return len(text.split())


def _heading_text(line: str) -> str:
    text = re.sub(r"^#+\s*", "", line).strip()
    text = _HEADING_LINK_RE.sub(r"\1", text)
    text = re.sub(r"[^\w\s\-—/()]", "", text).strip()
    return text


def _write_entry(
    title: str,
    body: str,
    out_dir: Path,
    seen_keys: dict[str, int],
    min_tokens: int,
) -> bool:
    """Write one JSON entry; return True if written, False if skipped."""
    body = body.strip()
    if _token_count(body) < min_tokens:
        log.debug(f"Dropping stub: {title!r} ({_token_count(body)} tokens)")
        return False

    base_key = _slugify(title)
    count = seen_keys.get(base_key, 0)
    seen_keys[base_key] = count + 1
    key = base_key if count == 0 else f"{base_key}-{count}"

    entry = {"key": key, "title": title, "body": body}
    path = out_dir / f"{key}.json"
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Wrote {path.name} ({_token_count(body)} tokens)")
    return True


def convert(source: Path, out_dir: Path, max_tokens: int = _MAX_TOKENS, min_tokens: int = _MIN_TOKENS) -> int:
    """Convert source Markdown to JSON entry files. Returns number written."""
    lines = source.read_text(encoding="utf-8").splitlines()
    out_dir.mkdir(parents=True, exist_ok=True)

    # First pass: collect H1 sections as (h1_title, [(h2_title_or_None, [body_lines])]).
    h1_sections: list[tuple[str, list[tuple[str, list[str]]]]] = []
    current_h1 = ""
    current_h2 = ""
    current_body: list[str] = []
    h2_chunks: list[tuple[str, list[str]]] = []

    def _save_h2_chunk() -> None:
        h2_chunks.append((current_h2, list(current_body)))

    def _save_h1_section() -> None:
        _save_h2_chunk()
        if current_h1 or h2_chunks:
            h1_sections.append((current_h1, list(h2_chunks)))

    for line in lines:
        if line.startswith("# "):
            _save_h1_section()
            current_h1 = _heading_text(line)
            current_h2 = ""
            current_body = []
            h2_chunks = []
        elif line.startswith("## "):
            _save_h2_chunk()
            current_h2 = _heading_text(line)
            current_body = []
        else:
            current_body.append(_clean_line(line))

    _save_h1_section()

    # Second pass: decide per H1 whether to write as one entry or split at H2.
    seen_keys: dict[str, int] = {}
    written = 0

    for h1_title, chunks in h1_sections:
        # Merge all H2 chunks into one body to measure total size.
        full_body_parts = []
        for h2_title, body_lines in chunks:
            if h2_title:
                full_body_parts.append(f"## {h2_title}")
            full_body_parts.extend(body_lines)
        full_body = "\n".join(full_body_parts)

        if _token_count(full_body) <= max_tokens:
            # Small enough — write the whole H1 as one entry.
            title = h1_title if h1_title else "Avni Documentation"
            if _write_entry(title, full_body, out_dir, seen_keys, min_tokens):
                written += 1
        else:
            # Too large — split at H2 boundaries.
            log.info(f"Splitting {h1_title!r} at H2 ({_token_count(full_body)} tokens > {max_tokens})")
            for h2_title, body_lines in chunks:
                body = "\n".join(body_lines)
                title = f"{h1_title} — {h2_title}" if h2_title else h1_title
                if not title:
                    continue
                if _write_entry(title, body, out_dir, seen_keys, min_tokens):
                    written += 1

    return written


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=_DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--max-tokens", type=int, default=_MAX_TOKENS,
                        help="H1 sections over this size are split at H2 (default: 2000)")
    parser.add_argument("--min-tokens", type=int, default=_MIN_TOKENS,
                        help="Entries below this size are dropped (default: 60)")
    args = parser.parse_args(argv)

    if not args.source.exists():
        log.error(f"Source not found: {args.source}")
        return 1

    log.info(f"Converting {args.source} → {args.out}")
    count = convert(args.source, args.out, max_tokens=args.max_tokens, min_tokens=args.min_tokens)
    log.info(f"Done — {count} entries written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
