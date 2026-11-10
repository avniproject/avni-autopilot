"""An uploaded .xlsx that is not a readable workbook ends the build with an
error naming the file, instead of raising out of the chat tool.

LLM enrichment and rule generation are switched off and every network
connection fails, so no API is called.

Usage:
    uv run --extra dev pytest tests/pipeline/test_unreadable_spreadsheets.py
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import chat.tools as tools
from config import Settings
from domain.llm import LLMHelper
from domain.parser import UnreadableSpreadsheetsError, parse_scoping_docs
from domain.rules.generator import RuleGenerator
from test_spreadsheet_format_prompt import _write_workbook

ORG = "broken_upload"


def _renamed_text_file(path: Path) -> Path:
    path.write_text("Name,Age\nAsha,30\n")
    return path


def _truncated_workbook(path: Path, source: Path) -> Path:
    path.write_bytes(source.read_bytes()[:2000])
    return path


@pytest.fixture
def org_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    def no_network(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(LLMHelper, "is_available", lambda _self: False)
    monkeypatch.setattr(RuleGenerator, "is_available", lambda _self: False)
    directory = tmp_path / "input" / ORG
    directory.mkdir(parents=True)
    _write_workbook(directory / "Modelling.xlsx")
    monkeypatch.setattr(
        tools, "settings",
        Settings(input_root=str(tmp_path / "input"), output_root=str(tmp_path / "output")),
    )
    return directory


def test_the_parser_names_every_unreadable_file(tmp_path: Path) -> None:
    good = _write_workbook(tmp_path / "Good.xlsx")
    renamed = _renamed_text_file(tmp_path / "Renamed.xlsx")
    truncated = _truncated_workbook(tmp_path / "Damaged.xlsx", good)
    with pytest.raises(UnreadableSpreadsheetsError) as caught:
        parse_scoping_docs([good, renamed, truncated])
    assert caught.value.file_names == ["Renamed.xlsx", "Damaged.xlsx"]
    assert "save it as .xlsx" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("make_broken", ["renamed", "truncated"])
def test_a_build_with_an_unreadable_file_reports_it(org_dir: Path, make_broken: str) -> None:
    if make_broken == "renamed":
        _renamed_text_file(org_dir / "Forms.xlsx")
    else:
        _truncated_workbook(org_dir / "Forms.xlsx", org_dir / "Modelling.xlsx")

    result = asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}))

    assert result["status"] == "error"
    assert result["error"] == str(UnreadableSpreadsheetsError(["Forms.xlsx"]))
    assert "NoneType" not in json.dumps(result)
    assert str(org_dir) not in json.dumps(result)
    assert "zip_path" not in result


def test_an_update_with_an_unreadable_file_reports_it(
    org_dir: Path, scripted_form_linking: None,
) -> None:
    built = asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}))
    assert built["status"] == "done", built.get("error")
    bundle = Path(built["zip_path"])
    before = bundle.read_bytes()
    _renamed_text_file(org_dir / "Forms.xlsx")
    session = SimpleNamespace(bundle_path=bundle, org_setup_loading=False)

    result = asyncio.run(tools.edit_bundle_from_spec.ainvoke(
        {"org": ORG}, config={"configurable": {"session": session}},
    ))

    assert result["status"] == "error"
    assert "Forms.xlsx" in result["error"]
    assert "bundle_updated" not in result
    assert bundle.read_bytes() == before
