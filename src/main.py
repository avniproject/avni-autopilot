#!/usr/bin/env python3
"""
CLI entry point: generate a full Avni bundle ZIP for a single org.

Usage:
    python src/main.py --org srijan
    python src/main.py --org astitva
    python src/main.py --org gubbachi

Paths resolved automatically:
    input  → resources/input/<org>/
    output → resources/output/<org>/
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# src/ has no __init__.py; add it to sys.path so modules inside are importable directly.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from pipeline import run  # noqa: E402  (resolved via _SRC on sys.path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an Avni bundle ZIP from modelling + scoping documents."
    )
    parser.add_argument(
        "--org",
        required=True,
        help="Org subfolder name under resources/input/ (e.g. srijan, astitva, gubbachi)",
    )
    args = parser.parse_args()

    org = args.org.strip().lower()
    input_dir = os.path.join(_ROOT, "resources", "input", org)
    output_dir = os.path.join(_ROOT, "resources", "output", org)

    if not os.path.isdir(input_dir):
        print(f"ERROR: Input directory not found: {input_dir}")
        sys.exit(1)

    print(f"\nOrg        : {org}")
    print(f"Input dir  : {input_dir}")
    print(f"Output dir : {output_dir}\n")

    result = run(org, input_dir, output_dir)

    # ── Summary ────────────────────────────────────────────────────────────────
    if result["parse_warnings"]:
        print("Warnings:")
        for w in result["parse_warnings"]:
            print(f"  • {w}")
        print()

    if result["errors"]:
        print("Errors:")
        for e in result["errors"]:
            print(f"  ✗ {e}")
        sys.exit(1)

    cancel_count = sum(
        1 for f in result["mapping_specs"]
        if "Cancellation" in f["name"]
    )
    main_forms = len(result["forms_json"]) - cancel_count

    print(f"Subject types  : {len(result['subject_types_json'])}")
    print(f"Programs       : {len(result['programs_json'])}")
    print(f"Encounter types: {len(result['encounter_types_json'])}")
    print(f"Forms          : {main_forms} main + {cancel_count} cancellation")
    print(f"Concepts       : {len(result['concepts_json'])}")
    print(f"Form mappings  : {len(result['form_mappings_json'])}")
    print(f"\nBundle ZIP     : {result['zip_path']}")


if __name__ == "__main__":
    main()
