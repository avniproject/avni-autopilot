# Rule Suggestion in Chat — Software Design Document

Supersedes `specs/RULE_PREVIEW_AND_AUTO_UPLOAD_SDD.md` (unimplemented).

---

## 1. Objective

When a user asks the chat assistant for a form-level or field-level rule
suggestion via the webapp, the assistant must reply in chat with the generated
rule JS as a fenced code block. Currently `set_form_rule` and
`set_form_element_rule` return a 400-char truncated `js_preview` in the tool
result and never surface the full JS as a readable message — the user sees
nothing.

The fix has two parts:

1. **Bundle fetch at session start.** When the user opens the AI assistant,
   download the live bundle from the Avni server (`GET
   /implementation/export/false`) using the session auth token and store it in
   the session workdir. The grounding context for rule generation always comes
   from the user's actual org — regardless of whether a bundle was ever built
   on the EC2 host via the pipeline.

2. **Suggest-only tools.** Replace both write-on-call tools with suggest-only
   equivalents that return the full JS in the tool result, letting the agent
   render it in chat. No bundle write. No upload.

---

## 2. Scope

### In scope

- `GET /implementation/export/false` called at session creation to download the
  org's current bundle ZIP into `session.workdir/bundle.zip`.
- Replace `set_form_rule` with `suggest_form_rule` — generates rule JS, returns
  it in full, never writes to any file.
- Replace `set_form_element_rule` with `suggest_form_element_rule` — same for
  field-level rules.
- Both suggest tools read the session bundle from `session.workdir/bundle.zip`
  (injected via LangChain `RunnableConfig`); they do not accept `bundle_path`
  as a parameter.
- `list_bundle_fields` similarly falls back to the session bundle when no
  explicit `bundle_path` is given.
- Agent system-prompt update: instructions to render `js` as a fenced code
  block, and plain-English phrasing for both tools.
- Remove `set_form_rule`, `set_form_element_rule` from `TOOLS`.

### Out of scope

- Writing the suggested JS to any bundle or uploading it. The suggestion is the
  deliverable; the user copies or applies it via the Avni admin interface.
- An interactive accept / reject / edit UI widget. The user asks for a revision
  in plain chat and the agent re-suggests with a new intent.
- New rule kinds. `RuleKind` is unchanged.
- The REPL path (`avni-chat`). Both tools keep their existing `bundle_path`
  parameter when invoked from the REPL; the session-bundle fallback applies
  only in the web context.

### Precondition

- `AVNI_WEBAPP_INTEGRATION_SDD.md` is implemented. `ChatSession` holds
  `auth_token`, `org_name`, and `workdir`.
- The Avni server exposes `GET /implementation/export/false` and returns a
  bundle ZIP for the authenticated user's org.

---

## 3. Bundle download at session creation

### 3.1 Where it happens

`src/web/sessions.py` — immediately after `create_session()` returns, a
background `asyncio.Task` is spawned to download the bundle. `POST /sessions`
returns 201 immediately; the download proceeds in parallel. Every org —
including a newly created one — has at least a minimal bundle on avni-server,
so the download always returns a valid ZIP.

```python
async def _download_org_bundle(session: ChatSession) -> None:
    """Fetch the org's current bundle from avni-server into session workdir.

    Runs as a background task — does not block session creation. Emits
    `session.loading` SSE at start and `session.ready` on completion so
    the webapp can show / hide the "Checking the current setup" indicator.
    """
    session.bus.emit("session.loading", {"message": "Checking the current setup"})
    url = f"{settings.avni_server_base_url}/implementation/export/false"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"AUTH-TOKEN": session.auth_token},
                timeout=60,
                follow_redirects=True,
            )
        resp.raise_for_status()
        dest = session.workdir / "bundle.zip"
        dest.write_bytes(resp.content)
        session.bundle_path = dest
    except Exception as exc:  # noqa: BLE001
        log.warning(f"bundle download failed session={session.session_id}: {exc}")
    finally:
        session.bus.emit("session.ready", {})
```

The downloaded ZIP is cleaned up automatically when the session expires or is
deleted — the background reaper in `sessions.py` deletes `session.workdir`
recursively (per `AVNI_WEBAPP_INTEGRATION_SDD.md` §8.1). No separate cleanup
is needed.

Every org has at least a minimal bundle, so the download is expected to always
succeed. A network or HTTP failure is logged and `session.bundle_path` stays
`None`; `suggest_*` tools return a clear error in that case. `session.ready`
is emitted regardless so the webapp loading indicator always resolves.

### 3.2 `ChatSession` change

Add `bundle_path: Path | None = None` to the `ChatSession` dataclass.

### 3.3 Passing the bundle path to tools

The session object is already injected into LangChain tool calls via
`config["configurable"]["session"]` (established in
`AVNI_WEBAPP_INTEGRATION_SDD.md` §5 for the upload flow). Both suggest tools
and `list_bundle_fields` read `bundle_path` from the session:

```python
from langchain_core.runnables import RunnableConfig

@tool
def suggest_form_rule(..., config: RunnableConfig = None) -> dict:
    session = (config or {}).get("configurable", {}).get("session")
    bundle_path = str(session.bundle_path) if session and session.bundle_path else None
    if not bundle_path:
        return {"status": "error", "error": "No bundle available for this session."}
    ...
```

When called from the REPL the session is absent; `bundle_path` falls back to
the explicit parameter already on the existing functions (backward compat, no
changes to the REPL path).

---

## 4. Tool surface

### 4.1 `suggest_form_rule`

```python
@tool
def suggest_form_rule(
    form_name: str,
    rule_kind: str,
    intent: str,
    config: RunnableConfig = None,
) -> dict:
    """Generate a form-level rule JS from a natural-language intent.

    Does NOT write to the bundle. Returns the full suggested JS in `js`.
    The agent must render `js` as a fenced ```javascript code block.

    `rule_kind` is one of:
      "visitScheduleRule"  — when the next visit should be scheduled
      "validationRule"     — block-save messages when the form has bad data
      "editFormRule"       — who/when the form can be edited
      "decisionRule"       — values written into concepts at submit time
    """
```

**Returns on success:**
```python
{
    "status": "suggested",
    "form_name": "Pregnancy Enrolment",
    "rule_kind": "visitScheduleRule",
    "js": "<full rule JS>",
    "confidence": 0.87,
    "used_helpers": [...],
    "referenced_concepts": [...],
    "referenced_encounter_types": [...],
    "warnings": [],
}
```

**Returns on validator rejection:**
```python
{
    "status": "rejected",
    "form_name": "...",
    "rule_kind": "...",
    "confidence": 0.42,
    "warnings": ["unbound helper foo", "unknown encounter type bar"],
    "js": "<the rejected JS — shown so the user can see what failed>",
}
```

**Returns on error:**
```python
{"status": "error", "error": "<reason>"}
```

Internally: today's `set_form_rule` body, with `write_form_rule` removed,
`js_preview` renamed to `js` (full, never truncated), and `bundle_path` read
from the session rather than taken as a parameter. Uses `RuleGenerator.generate(spec)`
— the single-rule path (one LLM call per suggestion). The pipeline's
`generate_field_batch` path is a bulk optimization for the `generate_rules`
node and is not used here.

---

### 4.2 `suggest_form_element_rule`

```python
@tool
def suggest_form_element_rule(
    form_name: str,
    page_name: str,
    field_name: str,
    intent: str,
    config: RunnableConfig = None,
) -> dict:
    """Generate a field-level rule JS from a natural-language intent.

    Does NOT write to the bundle. Returns the full suggested JS in `js`.
    The agent must render `js` as a fenced ```javascript code block.

    Targets `form.formElementGroups[i].formElements[j].rule`. Behaviour mix
    (visibility / value / validation / answer-filter) is dictated by intent.
    """
```

Return envelope mirrors `suggest_form_rule` with `page_name` and `field_name`
added.

---

### 4.3 `list_bundle_fields` update

Add an optional `bundle_path` parameter that, when absent, falls back to
`session.bundle_path`. The existing explicit-path behaviour is unchanged.

```python
@tool
def list_bundle_fields(
    bundle_path: str | None = None,
    config: RunnableConfig = None,
) -> dict:
    ...
    session = (config or {}).get("configurable", {}).get("session")
    resolved = bundle_path or (str(session.bundle_path) if session and session.bundle_path else None)
    if not resolved:
        return {"status": "error", "error": "No bundle available."}
    ...
```

---

## 5. Agent system-prompt changes (`src/chat/prompts.py`)

Replace the `set_form_rule` and `set_form_element_rule` entries with:

```
  - suggest_form_rule(form_name, rule_kind, intent) — generate a JS rule
    suggestion for a form and show it in chat. Does NOT write anything.
    `rule_kind` is one of:
      * "visitScheduleRule"  — scheduling the next visit
      * "validationRule"     — block-save error messages
      * "editFormRule"       — who/when the form can be edited
      * "decisionRule"       — computed/derived concept values at submit time
    Call `list_bundle_fields` first to resolve the exact `form_name` and any
    referenced concept or encounter type names.

  - suggest_form_element_rule(form_name, page_name, field_name, intent) —
    generate a JS rule suggestion for a single field and show it in chat.
    Does NOT write anything. Use when the user asks to show/hide, pre-fill,
    validate, or filter coded-answer options for a specific field.
    Call `list_bundle_fields` first to resolve exact names.
```

Add to the Behavior section:

```
  Rule suggestions:
  - When a user asks for a rule, call the appropriate `suggest_*` tool.
  - Always render the `js` field from the result as a fenced ```javascript
    code block in your reply, followed by one sentence explaining what the
    rule does.
  - If the result is "rejected", show the warnings and ask the user to
    reword their intent.
  - If the user asks to change the suggestion, call the same tool again with
    a revised intent.
  - Do NOT describe these as "writing" or "applying" — say "suggesting" or
    "generating a rule".
  - Narration: suggest_form_rule / suggest_form_element_rule →
    "Generating a rule suggestion…"
```

---

## 6. Failure modes

| Failure | Surfaced as | User path |
|---|---|---|
| Bundle download fails at session start (network error) | `session.bundle_path = None`; `suggest_*` returns `status: "error"` | Agent notifies user to retry opening the assistant. |
| Form / field not found in bundle | `status: "error"` | Call `list_bundle_fields` (uses session bundle) to get exact names. |
| Validator rejects JS | `status: "rejected"` with `warnings` | Agent shows warnings and asks user to reword intent. |
| LLM / KB failure | `status: "error"` | Agent surfaces error and suggests retry. |

---

## 7. Files to change

| File | Change |
|---|---|
| `src/web/sessions.py` | Add `_download_org_bundle(session)` coroutine; spawn it as `asyncio.create_task(...)` after `create_session()` returns. `bundle_path` field already exists on `ChatSession`. |
| `src/web/routes/sessions.py` | `POST /sessions` returns 201 immediately (no change). Pass `session` into graph `config["configurable"]`. |
| `src/chat/tools.py` | Replace `set_form_rule` → `suggest_form_rule` (no write, full JS, bundle from session). Replace `set_form_element_rule` → `suggest_form_element_rule`. Make `list_bundle_fields` fall back to session bundle. Update `TOOLS`. |
| `src/chat/prompts.py` | Replace both tool entries; add §5 Behavior block. |
| `avni-webapp: src/.../useChatSession.js` | On `session.loading` SSE event, show a "Checking the current setup" loading indicator in the chat. On `session.ready`, hide it. If the SSE stream connects after the background task has already completed, both events will have been missed and no indicator is shown — this is fine since the bundle state is already settled. |
| `avni-webapp: src/.../types.js` | Add `session.loading` and `session.ready` event types. |
| `specs/AVNI_WEBAPP_INTEGRATION_SDD.md` §4.2 | Add `session.loading` `{message}` and `session.ready` `{}` to the SSE event table. |
