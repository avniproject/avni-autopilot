"""Unit tests for what the chat is told about uploaded spreadsheets and the
org's bundle, the bundle card after an in-chat edit, and a failed download of
the org's current setup.

No LLM is called: the agent runs on a fake chat model that records the
system prompt it receives.

Usage:
    uv run --extra dev pytest tests/test_web_bundle_context.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import chat.agent as agent_module
import chat.tools as tools
import web.chat_service as chat_service_module
import web.sessions as sessions_module
from config import Settings
from web.chat_service import ChatService, _build_org_context
from domain.bundle_editor import BundleOrigin, classify_bundle
from domain.generators import form_uuid
from web.sessions import ChatSession, OrgSetupStatus, download_org_bundle

EKAM_BUNDLE = Path(__file__).resolve().parents[1] / "requirements" / "Ekam.zip"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def input_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "input"
    root.mkdir()
    patched = Settings(input_root=str(root), output_root=str(tmp_path / "output"))
    monkeypatch.setattr(chat_service_module, "settings", patched)
    monkeypatch.setattr(tools, "settings", patched)
    return root


def _session(tmp_path: Path, org_name: str = "Durga India") -> ChatSession:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    return ChatSession(session_id="sid-1", org_name=org_name, username="u", workdir=workdir)


def _loaded_session(tmp_path: Path, bundle: Path, origin: BundleOrigin) -> ChatSession:
    session = _session(tmp_path)
    session.bundle_path = bundle
    session.org_setup_status = OrgSetupStatus.LOADED
    session.bundle_origin = origin
    return session


def _session_config(session: ChatSession) -> dict[str, Any]:
    return {"configurable": {"session": session}}


def _ekam_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "work" / "bundle.zip"
    dest.parent.mkdir(exist_ok=True)
    shutil.copy(EKAM_BUNDLE, dest)
    return dest


def _rezip(source: Path, dest: Path, rewrite) -> Path:  # noqa: ANN001
    """Copy a bundle ZIP, passing each form JSON through `rewrite` (None drops it)."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(dest, "w") as out:
        for item in src.infolist():
            data = src.read(item)
            if item.filename.startswith("forms/") and item.filename.endswith(".json"):
                form = rewrite(json.loads(data))
                if form is None:
                    continue
                data = json.dumps(form).encode()
            out.writestr(item, data)
    return dest


def _autopilot_bundle(tmp_path: Path) -> Path:
    """Ekam export whose 'ANC Cancellation' form carries the generator's UUID."""
    def rewrite(form: dict) -> dict:
        if form.get("name") == "ANC Cancellation":
            form["uuid"] = form_uuid("ANC Cancellation")
        return form
    dest = tmp_path / "work" / "bundle.zip"
    dest.parent.mkdir(exist_ok=True)
    return _rezip(EKAM_BUNDLE, dest, rewrite)


def _add_field_op(op_id: str = "op-1", form: str = "ANC Cancellation") -> dict:
    return {
        "op_id": op_id,
        "kind": "field.add",
        "target": {"form": form, "section": "Details"},
        "payload": {"name": "QA17 Remarks", "dataType": "Text"},
    }


# ── Session context: spreadsheets ────────────────────────────────────────────


def test_context_says_no_spreadsheet_when_none_uploaded(input_root: Path, tmp_path: Path) -> None:
    context = _build_org_context(_session(tmp_path))
    assert "No spreadsheet has been uploaded in this chat" in context
    assert "have already been placed" not in context


def test_context_lists_uploaded_spreadsheets_only(input_root: Path, tmp_path: Path) -> None:
    org_dir = input_root / "durga_india"
    org_dir.mkdir()
    (org_dir / "Modelling.xlsx").write_bytes(b"x")
    (org_dir / "notes.txt").write_text("not a spreadsheet")
    context = _build_org_context(_session(tmp_path))
    assert "`Modelling.xlsx`" in context
    assert "notes.txt" not in context
    assert "No spreadsheet has been uploaded" not in context


def test_context_never_names_a_server_path(input_root: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    for status in OrgSetupStatus:
        session.org_setup_status = status
        session.org_setup_error = "you do not have the 'Download bundle' privilege"
        context = _build_org_context(session)
        assert "resources/" not in context
        assert ".zip" not in context


# ── Session context: org setup ───────────────────────────────────────────────


def test_context_says_an_autopilot_bundle_is_updated(input_root: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.org_setup_status = OrgSetupStatus.LOADED
    session.bundle_origin = BundleOrigin.AUTOPILOT
    context = _build_org_context(session)
    assert "Its forms were built by Autopilot" in context
    assert "edit_bundle_from_spec from spreadsheets" in context


def test_context_gives_the_reason_the_setup_failed(input_root: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.org_setup_status = OrgSetupStatus.FAILED
    session.org_setup_error = "you do not have the 'Download bundle' privilege"
    context = _build_org_context(session)
    assert "could NOT be loaded because you do not have the 'Download bundle' privilege" in context
    assert "Do not act as if the organisation has no setup" in context


def test_context_says_setup_is_still_loading(input_root: Path, tmp_path: Path) -> None:
    context = _build_org_context(_session(tmp_path))
    assert "still being loaded" in context


# ── Agent: the context is rebuilt before every model call ────────────────────


class _RecordingChatModel(GenericFakeChatModel):
    """Fake chat model that records the system prompt of every call."""

    system_prompts: list[str] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_RecordingChatModel":  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        self.system_prompts.append(messages[0].content)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_agent_sees_a_spreadsheet_uploaded_after_the_chat_started(
    monkeypatch: pytest.MonkeyPatch, input_root: Path, tmp_path: Path,
) -> None:
    model = _RecordingChatModel(messages=iter([AIMessage(content="a"), AIMessage(content="b")]))
    model.system_prompts = []
    monkeypatch.setattr(agent_module, "ChatAnthropic", lambda **_kwargs: model)
    session = _session(tmp_path)
    agent = agent_module.build_agent(session_context=lambda: _build_org_context(session))
    config = {"configurable": {"thread_id": "t1"}}

    agent.invoke({"messages": [HumanMessage(content="build my app")]}, config=config)
    org_dir = input_root / "durga_india"
    org_dir.mkdir()
    (org_dir / "Scoping.xlsx").write_bytes(b"x")
    session.org_setup_status = OrgSetupStatus.LOADED
    session.bundle_origin = BundleOrigin.AUTOPILOT
    agent.invoke({"messages": [HumanMessage(content="now build it")]}, config=config)

    first, second = model.system_prompts
    assert "No spreadsheet has been uploaded in this chat" in first
    assert "still being loaded" in first
    assert "`Scoping.xlsx`" in second
    assert "Its forms were built by Autopilot" in second
    assert first.startswith(agent_module.SYSTEM_PROMPT)


def test_repl_agent_keeps_the_static_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _RecordingChatModel(messages=iter([AIMessage(content="a")]))
    model.system_prompts = []
    monkeypatch.setattr(agent_module, "ChatAnthropic", lambda **_kwargs: model)
    agent = agent_module.build_agent()
    agent.invoke({"messages": [HumanMessage(content="hi")]}, config={"configurable": {"thread_id": "t"}})
    assert model.system_prompts == [agent_module.SYSTEM_PROMPT]


# ── Tools: no spreadsheet / no bundle ────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_bundle_refuses_without_a_spreadsheet(input_root: Path) -> None:
    (input_root / "durga_india").mkdir()
    result = await tools.generate_bundle.ainvoke({"org": "durga_india"})
    assert result["status"] == "error"
    assert "No spreadsheet has been uploaded" in result["error"]
    assert str(input_root) not in result["error"]


@pytest.mark.asyncio
async def test_edit_from_spec_refuses_without_a_spreadsheet(input_root: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.bundle_path = _ekam_copy(tmp_path)
    result = await tools.edit_bundle_from_spec.ainvoke(
        {"org": "durga_india"}, config=_session_config(session),
    )
    assert result["status"] == "error"
    assert "No spreadsheet has been uploaded" in result["error"]


@pytest.mark.asyncio
async def test_edit_from_spec_without_a_session_bundle_names_no_path(
    input_root: Path, tmp_path: Path,
) -> None:
    org_dir = input_root / "durga_india"
    org_dir.mkdir()
    (org_dir / "Modelling.xlsx").write_bytes(b"x")
    session = _session(tmp_path)
    session.org_setup_status = OrgSetupStatus.FAILED
    result = await tools.edit_bundle_from_spec.ainvoke(
        {"org": "durga_india"}, config=_session_config(session),
    )
    assert result["status"] == "error"
    assert "no bundle to update" in result["error"]
    assert "resources/" not in result["error"] and ".zip" not in result["error"]


@pytest.mark.asyncio
async def test_edit_from_spec_edits_the_session_bundle(
    monkeypatch: pytest.MonkeyPatch, input_root: Path, tmp_path: Path,
) -> None:
    org_dir = input_root / "durga_india"
    org_dir.mkdir()
    (org_dir / "Modelling.xlsx").write_bytes(b"x")
    session = _loaded_session(tmp_path, _autopilot_bundle(tmp_path), BundleOrigin.AUTOPILOT)
    captured: dict[str, Any] = {}

    async def fake_run(initial: dict, thread_id: str) -> dict:
        captured.update(initial)
        return {"status": "done", "zip_path": initial["bundle_path"]}

    monkeypatch.setattr(tools, "_run_phase1_then_pause_or_finish", fake_run)
    result = await tools.edit_bundle_from_spec.ainvoke(
        {"org": "durga_india"}, config=_session_config(session),
    )
    assert captured["mode"] == "edit_from_spec"
    assert captured["bundle_path"] == str(session.bundle_path)
    assert result == {"status": "done", "bundle_updated": True}


# ── Tools: results flag a changed bundle and carry no server path ───────────


def _assert_no_server_path(result: dict, *paths: Path) -> None:
    text = json.dumps(result)
    assert "zip_path" not in result and "bundle_path" not in text
    for path in paths:
        assert str(path) not in text


def test_edit_bundle_fields_flags_a_change_without_a_path(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.bundle_path = _ekam_copy(tmp_path)
    result = tools.edit_bundle_fields.invoke(
        {"operations": [_add_field_op()]}, config=_session_config(session),
    )
    assert result["status"] == "done"
    assert result["result"]["form_elements_added"] == 1
    assert result["bundle_updated"] is True
    _assert_no_server_path(result, session.bundle_path, tmp_path)


def test_edit_bundle_fields_in_the_repl_returns_the_zip_path(tmp_path: Path) -> None:
    bundle = _ekam_copy(tmp_path)
    result = tools.edit_bundle_fields.invoke(
        {"operations": [_add_field_op()], "bundle_path": str(bundle)},
    )
    assert result["zip_path"] == str(bundle)
    assert "bundle_updated" not in result


def test_list_bundle_fields_carries_no_server_path(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.bundle_path = _ekam_copy(tmp_path)
    result = tools.list_bundle_fields.invoke({}, config=_session_config(session))
    assert result["status"] == "done" and result["result"]["forms"]
    _assert_no_server_path(result, session.bundle_path, tmp_path)


@pytest.mark.parametrize("tool_name", ["list_bundle_fields", "edit_bundle_fields"])
def test_an_unreadable_bundle_is_reported_without_its_path(tmp_path: Path, tool_name: str) -> None:
    session = _session(tmp_path)
    session.bundle_path = tmp_path / "broken.zip"
    session.bundle_path.write_bytes(b"not a zip")
    args = {"operations": [_add_field_op()]} if tool_name == "edit_bundle_fields" else {}
    result = getattr(tools, tool_name).invoke(args, config=_session_config(session))
    assert result == {"status": "error", "error": tools._UNREADABLE_BUNDLE_ERROR}


@pytest.mark.asyncio
async def test_edit_from_spec_reports_an_unreadable_bundle_without_its_path(
    input_root: Path, tmp_path: Path,
) -> None:
    org_dir = input_root / "durga_india"
    org_dir.mkdir()
    (org_dir / "Modelling.xlsx").write_bytes(b"x")
    session = _session(tmp_path)
    session.org_setup_status = OrgSetupStatus.LOADED
    session.bundle_path = tmp_path / "broken.zip"
    session.bundle_path.write_bytes(b"not a zip")
    result = await tools.edit_bundle_from_spec.ainvoke(
        {"org": "durga_india"}, config=_session_config(session),
    )
    assert result == {"status": "error", "error": tools._UNREADABLE_BUNDLE_ERROR}


@pytest.mark.asyncio
async def test_a_build_hands_its_bundle_to_the_session(
    monkeypatch: pytest.MonkeyPatch, input_root: Path, tmp_path: Path,
) -> None:
    org_dir = input_root / "durga_india"
    org_dir.mkdir()
    (org_dir / "Modelling.xlsx").write_bytes(b"x")
    session = _loaded_session(tmp_path, _empty_bundle(tmp_path), BundleOrigin.NO_FORMS)
    built = tmp_path / "output" / "Durga_india.zip"

    async def fake_run(initial: dict, thread_id: str) -> dict:  # noqa: ARG001
        return {"status": "done", "main_forms": 3, "zip_path": str(built)}

    monkeypatch.setattr(tools, "_run_phase1_then_pause_or_finish", fake_run)
    result = await tools.generate_bundle.ainvoke(
        {"org": "durga_india"}, config=_session_config(session),
    )
    assert result == {"status": "done", "main_forms": 3, "bundle_updated": True}
    assert session.bundle_path == built


def test_edit_bundle_fields_flags_nothing_when_nothing_changed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.bundle_path = _ekam_copy(tmp_path)
    result = tools.edit_bundle_fields.invoke(
        {"operations": [_add_field_op(form="No Such Form")]}, config=_session_config(session),
    )
    assert result["status"] == "done"
    assert result["result"]["rejected"]
    assert "bundle_updated" not in result


def test_edit_bundle_fields_in_a_session_ignores_a_passed_path(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.bundle_path = _ekam_copy(tmp_path)
    other = tmp_path / "elsewhere.zip"
    shutil.copy(EKAM_BUNDLE, other)
    before = other.read_bytes()
    result = tools.edit_bundle_fields.invoke(
        {"operations": [_add_field_op()], "bundle_path": str(other)},
        config=_session_config(session),
    )
    assert result["bundle_updated"] is True
    assert other.read_bytes() == before


def test_edit_bundle_fields_without_a_session_bundle_errors(tmp_path: Path) -> None:
    result = tools.edit_bundle_fields.invoke(
        {"operations": [_add_field_op()]}, config=_session_config(_session(tmp_path)),
    )
    assert result["status"] == "error"
    assert "No bundle available" in result["error"]


# ── Chat service: bundle card after an in-chat edit ──────────────────────────


class _StubAgent:
    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self._updates = updates

    async def astream(self, _input, *, config, stream_mode):  # noqa: ARG002
        for update in self._updates:
            yield update


@pytest.mark.asyncio
async def test_field_edit_in_chat_shows_the_bundle_card(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.bundle_path = _ekam_copy(tmp_path)
    edit_result = tools.edit_bundle_fields.invoke(
        {"operations": [_add_field_op()]}, config=_session_config(session),
    )
    updates = [{"tools": {"messages": [ToolMessage(content=json.dumps(edit_result), tool_call_id="c1")]}}]
    svc = ChatService(session, agent_factory=lambda: _StubAgent(updates))
    await svc.send_message("add a remarks field")
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    ready = [e for e in session.bus.replay_since(0) if e.type == "bundle.ready"]
    assert len(ready) == 1
    assert ready[0].data["path"] == str(session.bundle_path)
    assert session.bundle_changed_in_chat is True


# ── Org setup download ───────────────────────────────────────────────────────


def _patch_export(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # noqa: ANN001
    real_client = httpx.AsyncClient

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(sessions_module.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(sessions_module, "settings", Settings(avni_server_base_url="http://avni.test"))


@pytest.mark.asyncio
async def test_download_without_privilege_tells_user_and_chat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_export(monkeypatch, lambda _req: httpx.Response(403, json={"message": "denied"}))
    session = _session(tmp_path)
    await download_org_bundle(session, "token")

    assert session.org_setup_status is OrgSetupStatus.FAILED
    assert "'Download bundle' privilege" in session.org_setup_error
    assert session.bundle_path is None
    events = [(e.type, e.data) for e in session.bus.replay_since(0)]
    assert [t for t, _ in events] == ["session.loading", "error", "session.ready"]
    error = events[1][1]
    assert error["code"] == "E_SETUP_NOT_LOADED"
    assert error["recoverable"] is True
    assert "'Download bundle' privilege" in error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (lambda _req: httpx.Response(401), "sign-in has expired"),
        (lambda _req: httpx.Response(500), "HTTP 500"),
        (lambda req: (_ for _ in ()).throw(httpx.ConnectError("down", request=req)), "could not be reached"),
    ],
)
async def test_download_failure_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, handler, expected: str,  # noqa: ANN001
) -> None:
    _patch_export(monkeypatch, handler)
    session = _session(tmp_path)
    await download_org_bundle(session, "token")
    assert session.org_setup_status is OrgSetupStatus.FAILED
    assert expected in session.org_setup_error


@pytest.mark.asyncio
async def test_download_success_marks_setup_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_export(monkeypatch, lambda _req: httpx.Response(200, content=EKAM_BUNDLE.read_bytes()))
    session = _session(tmp_path)
    await download_org_bundle(session, "token")
    assert session.org_setup_status is OrgSetupStatus.LOADED
    assert session.bundle_path == session.workdir / "bundle.zip"
    assert session.org_setup_error is None
    assert [e.type for e in session.bus.replay_since(0)] == ["session.loading", "session.ready"]


# ── Bundle origin ────────────────────────────────────────────────────────────


def _empty_bundle(tmp_path: Path) -> Path:
    return _rezip(EKAM_BUNDLE, tmp_path / "empty.zip", lambda _form: None)


def test_classify_bundle(tmp_path: Path) -> None:
    assert classify_bundle(str(EKAM_BUNDLE)) is BundleOrigin.OTHER
    assert classify_bundle(str(_autopilot_bundle(tmp_path))) is BundleOrigin.AUTOPILOT
    assert classify_bundle(str(_empty_bundle(tmp_path))) is BundleOrigin.NO_FORMS
    voided = _rezip(EKAM_BUNDLE, tmp_path / "voided.zip", lambda form: {**form, "voided": True})
    assert classify_bundle(str(voided)) is BundleOrigin.NO_FORMS
    not_a_bundle = tmp_path / "page.html"
    not_a_bundle.write_text("<html>")
    with pytest.raises(FileNotFoundError):
        classify_bundle(str(not_a_bundle))


# ── Session context by bundle origin ─────────────────────────────────────────


def test_context_for_forms_set_up_outside_autopilot(input_root: Path, tmp_path: Path) -> None:
    session = _loaded_session(tmp_path, EKAM_BUNDLE, BundleOrigin.OTHER)
    context = _build_org_context(session)
    assert "set up outside Autopilot" in context
    assert "never offer or call edit_bundle_from_spec" in context
    assert "confirmed_separate_build=true" in context
    assert "built by Autopilot, so" not in context


def test_context_for_an_org_with_no_forms(input_root: Path, tmp_path: Path) -> None:
    session = _loaded_session(tmp_path, _empty_bundle(tmp_path), BundleOrigin.NO_FORMS)
    context = _build_org_context(session)
    assert "It has no forms yet" in context
    assert "generate_bundle" in context


def test_context_after_a_build_in_chat_describes_that_bundle(input_root: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.org_setup_status = OrgSetupStatus.FAILED
    session.org_setup_error = "you do not have the 'Download bundle' privilege"
    session.bundle_changed_in_chat = True
    session.bundle_origin = BundleOrigin.AUTOPILOT
    context = _build_org_context(session)
    assert "could NOT be loaded because you do not have the 'Download bundle' privilege" in context
    assert "A bundle was built or edited in this chat" in context
    assert "Its forms were built by Autopilot" in context
    assert "No bundle has been built or edited in this chat yet" not in context


# ── Tools: update and build guards ───────────────────────────────────────────


def _upload_sheet(input_root: Path, name: str = "Modelling.xlsx") -> None:
    org_dir = input_root / "durga_india"
    org_dir.mkdir(exist_ok=True)
    (org_dir / name).write_bytes(b"x")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bundle_factory", "origin", "expected"),
    [
        (lambda tmp: EKAM_BUNDLE, BundleOrigin.OTHER, "set up outside Autopilot"),
        (_empty_bundle, BundleOrigin.NO_FORMS, "has no forms yet"),
    ],
)
async def test_edit_from_spec_refuses_bundles_it_cannot_update(
    monkeypatch: pytest.MonkeyPatch, input_root: Path, tmp_path: Path,
    bundle_factory, origin: BundleOrigin, expected: str,  # noqa: ANN001
) -> None:
    _upload_sheet(input_root)
    session = _loaded_session(tmp_path, bundle_factory(tmp_path), origin)

    async def must_not_run(*_args: Any) -> dict:
        raise AssertionError("pipeline ran")

    monkeypatch.setattr(tools, "_run_phase1_then_pause_or_finish", must_not_run)
    result = await tools.edit_bundle_from_spec.ainvoke(
        {"org": "durga_india"}, config=_session_config(session),
    )
    assert result["status"] == "error"
    assert expected in result["error"]


@pytest.fixture
def recorded_builds(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    runs: list[dict] = []

    async def fake_run(initial: dict, thread_id: str) -> dict:  # noqa: ARG001
        runs.append(initial)
        return {"status": "done", "zip_path": ""}

    monkeypatch.setattr(tools, "_run_phase1_then_pause_or_finish", fake_run)
    return runs


def _failed_session(tmp_path: Path) -> ChatSession:
    session = _session(tmp_path)
    session.org_setup_status = OrgSetupStatus.FAILED
    session.org_setup_error = "of an error"
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_factory",
    [lambda tmp: _loaded_session(tmp, EKAM_BUNDLE, BundleOrigin.OTHER), _failed_session],
)
async def test_build_needs_confirmation_when_it_may_duplicate(
    input_root: Path, tmp_path: Path, recorded_builds: list[dict], session_factory,  # noqa: ANN001
) -> None:
    _upload_sheet(input_root)
    config = _session_config(session_factory(tmp_path))
    refused = await tools.generate_bundle.ainvoke({"org": "durga_india"}, config=config)
    assert refused["status"] == "error"
    assert "confirmed_separate_build=true" in refused["error"]
    assert recorded_builds == []

    await tools.generate_bundle.ainvoke(
        {"org": "durga_india", "confirmed_separate_build": True}, config=config,
    )
    assert len(recorded_builds) == 1


@pytest.mark.asyncio
async def test_build_runs_without_confirmation_for_an_org_with_no_forms(
    input_root: Path, tmp_path: Path, recorded_builds: list[dict],
) -> None:
    _upload_sheet(input_root)
    session = _loaded_session(tmp_path, _empty_bundle(tmp_path), BundleOrigin.NO_FORMS)
    await tools.generate_bundle.ainvoke({"org": "durga_india"}, config=_session_config(session))
    assert len(recorded_builds) == 1


@pytest.mark.asyncio
async def test_build_waits_while_setup_is_loading(
    input_root: Path, tmp_path: Path, recorded_builds: list[dict],
) -> None:
    _upload_sheet(input_root)
    result = await tools.generate_bundle.ainvoke(
        {"org": "durga_india", "confirmed_separate_build": True},
        config=_session_config(_session(tmp_path)),
    )
    assert result["status"] == "error"
    assert "still loading" in result["error"]
    assert recorded_builds == []


# ── Spreadsheet discovery ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("file_name", ["Modelling.xls", "~$Modelling.xlsx"])
async def test_unreadable_workbooks_do_not_count_as_uploaded(
    input_root: Path, tmp_path: Path, recorded_builds: list[dict], file_name: str,
) -> None:
    _upload_sheet(input_root, file_name)
    assert "No spreadsheet has been uploaded" in _build_org_context(_session(tmp_path))
    result = await tools.generate_bundle.ainvoke({"org": "durga_india"})
    assert result["status"] == "error"
    assert "No spreadsheet has been uploaded" in result["error"]


def test_upper_case_xlsx_is_listed_and_discovered(input_root: Path, tmp_path: Path) -> None:
    from pipeline import initial_state
    from pipeline.nodes import discover_files

    _upload_sheet(input_root, "MODELLING.XLSX")
    assert "`MODELLING.XLSX`" in _build_org_context(_session(tmp_path))
    state = initial_state("durga_india", str(input_root / "durga_india"), str(tmp_path / "out"))
    found = discover_files(state)
    assert [Path(p).name for p in found["file_paths"]] == ["MODELLING.XLSX"]
    assert found["errors"] == []


# ── Org setup download: bundle origin ────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_records_the_bundle_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_export(monkeypatch, lambda _req: httpx.Response(200, content=EKAM_BUNDLE.read_bytes()))
    session = _session(tmp_path)
    await download_org_bundle(session, "token")
    assert session.bundle_origin is BundleOrigin.OTHER
    assert session.build_needs_confirmation is True


@pytest.mark.asyncio
async def test_download_of_a_non_bundle_reply_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_export(monkeypatch, lambda _req: httpx.Response(200, text="<html>login</html>"))
    session = _session(tmp_path)
    await download_org_bundle(session, "token")
    assert session.org_setup_status is OrgSetupStatus.FAILED
    assert "reply was not a bundle" in session.org_setup_error
    assert session.bundle_path is None
