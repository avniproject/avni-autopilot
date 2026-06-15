# Avni Webapp Integration — Software Design Document

## 1. Objective

Expose the bundle-generation pipeline currently driven by the `avni-chat` REPL
through `avni-webapp` so an org admin, signed into their own organisation, can:

1. Upload one or more scoping `.xlsx` files from the browser.
2. Watch the LangGraph pipeline run live, including the human-in-the-loop
   confirmations the REPL surfaces today (long-name renames, concept-answer
   grounding, set-rule confirmations).
3. Respond to those confirmations inline from a chat panel.
4. On success, have the generated bundle auto-uploaded to `avni-server`'s
   existing Metadata-Zip import endpoint against the admin's own org.

No new generation logic. The webapp is a transport + UI shell over the existing
pipeline, chat agent, and `KnowledgeBase`.

---

## 2. Scope

### In scope

- A new FastAPI service (`src/web/`) that wraps the existing chat ReAct agent
  (`src/chat/agent.py`) behind HTTP + Server-Sent Events.
- Session-scoped persistence so the LangGraph `MemorySaver` checkpoint survives
  HTTP round-trips and `interrupt()` resume cycles.
- A new `avni-webapp` feature under `src/adminApp/aiAssistant/` (or
  `src/dataEntryApp/aiAssistant/` — see §11) that mounts a chat panel, file
  upload, and confirmation cards.
- Auto-upload of the produced ZIP to the existing
  `POST /import/new?type=Metadata Zip` endpoint on `avni-server`, reusing the
  admin's session cookie / auth header.
- Org scoping: the service trusts the org the admin is authenticated against
  (read from the validated token); the admin never picks an org.

### Out of scope

- A new generation pipeline, model, or rule kind. `generate_bundle`,
  `set_visit_schedule_rule`, `edit_bundle_fields`, `edit_bundle_from_spec`, and
  `resume_bundle` ship as-is and are reused verbatim.
- Cross-org operator UX (operator selecting an org from a dropdown). Deferred
  to a follow-up SDD.
- Persistent conversation history across server restarts. Sessions are
  process-local (see §8); the user re-uploads the xlsx on reconnect.
- Hosting the FastAPI service inside `avni-server` (Java). It is a sibling
  Python process. Co-deployment options are documented but not implemented
  here.
- Editing the catalog (`resources/rules/`) from the browser. The
  `avni-rules-kb` CLI remains the only catalog editor.
- Streaming token-level LLM output. Streaming is at the message / tool-call
  granularity (one SSE event per agent message, tool call, tool result).

### Precondition

`avni-chat` works end-to-end on the same machine for the target org (i.e. the
chat REPL can generate, edit, and set rules against the org's xlsx). The
integration adds a transport, not a capability.

---

## 3. Architecture

### 3.1 Component map

```
┌──────────────────────────┐        ┌──────────────────────────────┐
│  avni-webapp (React)     │        │  avni-server (Java)          │
│                          │        │                              │
│  src/adminApp/           │        │  /import/new?type=Metadata.. │
│    aiAssistant/          │  ◀────▶│  /me, /idp-details, …        │
│      ChatPanel.jsx       │        │                              │
│      UploadDropzone.jsx  │        └──────────────▲───────────────┘
│      ConfirmationCard.jsx│                       │
│      api.js (axios + SSE)│        Bundle ZIP     │ admin's auth
└────────────┬─────────────┘        relayed by     │ relayed
             │ HTTPS                FastAPI ──────┘
             │
             │ SSE / multipart
             ▼
┌──────────────────────────────────────────────────┐
│  avni-ai-tools FastAPI (new — src/web/)          │
│                                                  │
│  POST  /sessions                  start agent    │
│  POST  /sessions/{id}/upload      multipart xlsx │
│  POST  /sessions/{id}/message     user turn      │
│  POST  /sessions/{id}/resolve     HITL response  │
│  GET   /sessions/{id}/events      SSE stream     │
│  GET   /sessions/{id}/bundle      download ZIP   │
│  DELETE /sessions/{id}            cleanup        │
│                                                  │
│  ┌─ChatService──────────────────────────────────┐│
│  │ wraps build_chat_graph() from src/chat/      ││
│  │ holds MemorySaver per session_id             ││
│  │ surfaces interrupts as SSE events            ││
│  │ accepts Command(resume=…) over POST /resolve ││
│  └──────────────────────────────────────────────┘│
└──────────────────────────────────────────────────┘
             │
             │ tools call existing modules
             ▼
   src/pipeline/, src/domain/, src/chat/tools.py  (unchanged)
```

The FastAPI service owns:

- HTTP routing, multipart parsing, SSE encoding.
- Session lifecycle and the in-process LangGraph checkpoint store.
- Auth verification against `avni-server` (token introspection).
- Relaying the final bundle to `avni-server`'s import endpoint using the
  admin's own auth header — the service never holds long-lived credentials.

It does **not** own: parsing, generation, rule generation, validation, KB
retrieval. Those are imported from existing modules.

### 3.2 Session lifecycle

```
Browser                      FastAPI                       LangGraph
   │  POST /sessions          │                              │
   │ ───────────────────────▶ │  build_chat_graph()          │
   │                          │  MemorySaver()               │
   │ ◀─── 201 {session_id} ── │                              │
   │                          │                              │
   │  POST /upload (xlsx)     │                              │
   │ ───────────────────────▶ │  write to /tmp/<sid>/input/  │
   │ ◀─── 200 {paths} ─────── │                              │
   │                          │                              │
   │  GET /events (SSE) ◀─────│  EventStream(session_id)     │
   │  POST /message {text}    │                              │
   │ ───────────────────────▶ │  graph.invoke(state)         │
   │                          │  ────────────────────────────│
   │                          │  agent → tool_call (generate_bundle)
   │                          │  ◀── tool_result ────────────│
   │  ◀ SSE: agent.message ───│                              │
   │  ◀ SSE: tool.call ───────│                              │
   │  ◀ SSE: tool.result ─────│                              │
   │                          │                              │
   │                          │  pipeline interrupt() ──────▶│
   │  ◀ SSE: hitl.pending ────│ (pending changes payload)    │
   │  POST /resolve {…}       │                              │
   │ ───────────────────────▶ │  graph.invoke(Command(resume=…))
   │  ◀ SSE: agent.message ───│                              │
   │  ◀ SSE: bundle.ready ────│ (zip path on disk)           │
   │                          │                              │
   │  POST /upload-to-avni    │  http.post(/import/new) ────▶│ avni-server
   │  ◀ SSE: upload.done ─────│                              │
```

`interrupt()` is the same mechanism the REPL handles today
(`apply_user_decisions` in `src/pipeline/nodes.py`). The FastAPI service
translates it into a structured SSE event and waits for `POST /resolve` before
calling `graph.invoke(Command(resume=…))`.

---

## 4. HTTP API surface

All endpoints require a valid Avni auth token (Cognito JWT or Keycloak access
token, matching whichever IDP the org uses), passed as `Authorization: Bearer
<token>` or via the existing `auth-token` cookie. The service verifies the
token by calling `GET /me` on `avni-server` once at session creation; the org
returned there is stored on the session and used for the auto-upload.

### 4.1 Endpoints

| Method | Path | Body | 200 response |
|---|---|---|---|
| POST | `/sessions` | `{}` | `{session_id, org_name, expires_at}` |
| POST | `/sessions/{id}/upload` | multipart `.xlsx` files | `{paths: [...]}` |
| POST | `/sessions/{id}/message` | `{text: "..."}` | `{accepted: true}` |
| POST | `/sessions/{id}/resolve` | `{interrupt_id, resolutions: [...]}` | `{accepted: true}` |
| GET | `/sessions/{id}/events` | — (SSE) | `text/event-stream` |
| GET | `/sessions/{id}/bundle` | — | `application/zip` |
| POST | `/sessions/{id}/upload-to-avni` | `{}` | `{job_id}` |
| DELETE | `/sessions/{id}` | — | `204` |

All non-SSE responses are JSON. Errors follow `{error: str, code: str,
details?: …}` with HTTP 4xx/5xx as appropriate.

### 4.2 SSE event types

Each event is a JSON object on the SSE `data:` line, with `event:` set to the
type name:

| `event:` | `data` payload | Emitted when |
|---|---|---|
| `agent.message` | `{role, content, ts}` | Agent produces a chat message |
| `tool.call` | `{tool, args, call_id, ts}` | Agent invokes a tool |
| `tool.result` | `{call_id, ok, summary, ts}` | Tool returns |
| `hitl.pending` | `{interrupt_id, changes: [...]}` | `interrupt()` fires |
| `pipeline.progress` | `{node, status}` | LangGraph node start/end (best-effort) |
| `bundle.ready` | `{path, summary}` | ZIP written to disk |
| `upload.done` | `{job_id, status}` | `/import/new` returns |
| `error` | `{code, message, recoverable}` | Recoverable or terminal error |
| `session.closed` | `{reason}` | Session ended or expired |

`hitl.pending.changes` mirrors today's REPL prompt shape
(`PendingChange` from the pipeline state): one entry per proposed rename, each
with `before`, `after`, `reason`, and a stable `change_id`. The browser POSTs
back to `/resolve` with `{change_id, decision: "yes"|"no"|"edit", value?: str}`
per change.

### 4.3 Auto-upload to avni-server

`POST /sessions/{id}/upload-to-avni` reads the session's `bundle.zip`,
relays it to `avni-server` as a multipart POST to:

```
POST /import/new?type=Metadata%20Zip&autoApprove=false
Authorization: <admin's bearer token, captured at session start>
```

This is the same endpoint `avni-webapp`'s existing "Upload" page hits today
(`src/upload/api.js` → `bulkUpload`). The service emits `upload.done` with
the import job id; the browser then deep-links to the existing
`UploadStatus.jsx` view to follow the import.

The admin's auth token is **not stored beyond the session lifetime** — it
lives in the in-memory session record and is dropped on `DELETE /sessions/{id}`
or expiry.

---

## 5. avni-webapp UI surface

### 5.1 Route

New route under the admin app:

```
/admin/ai-assistant     →  AiAssistantPage.jsx
```

Gated behind the same role gate that today protects the `/upload` page (org
admin). The route is hidden when the FastAPI base URL is not configured
(`window.ENV.AI_ASSISTANT_URL` falsy).

### 5.2 React components

```
src/adminApp/aiAssistant/
├── AiAssistantPage.jsx        Page shell; layout 60/40 chat/sidebar
├── ChatPanel.jsx              Message list + composer
├── UploadDropzone.jsx         .xlsx multi-file dropzone
├── ConfirmationCard.jsx       One pending change → Yes / No / Edit input
├── BundleSummary.jsx          Post-generation counts + "Upload to org" button
├── api.js                     axios client + SSE wrapper (EventSource)
├── useChatSession.js          React hook: start / send / resolve / cleanup
└── types.js                   Event payload types
```

Conventions:

- Material UI v5 (matches the rest of the admin app).
- `axios` for non-SSE; native `EventSource` for SSE (no extra dep).
- `react-markdown` already in `package.json` — reuse for agent message
  rendering so code blocks and inline code from the chat agent render
  correctly.
- Errors render as a dismissible banner above the composer; recoverable errors
  do not tear the session down.

### 5.3 Session bootstrap (browser side)

On page mount, `useChatSession.js`:

1. `POST /sessions` to allocate a session id.
2. Open `EventSource('/sessions/{id}/events')`.
3. Block the composer until the SSE stream is `open`.
4. On `session.closed` or page unmount, `DELETE /sessions/{id}`.

The session id is stored in `sessionStorage` so a tab refresh resumes the same
session (events backlog: see §8.2).

---

## 6. Auth and org scoping

| Concern | Approach |
|---|---|
| Token format | Whatever the org's IDP uses today (Cognito JWT or Keycloak access token). The webapp already obtains this for every page; the FastAPI service treats it as opaque and forwards to `avni-server` for verification. |
| Verification | One `GET /me` against `avni-server` per session creation. On 401, the service returns 401 to the browser and the existing webapp auth flow kicks in. |
| Org binding | The org returned by `/me` is stored on the session record. Every tool invocation is run against that org's `resources/input/<org>/` (the xlsx is copied here on `/upload`). Cross-org references are rejected. |
| Token reuse | The token is captured at session start and reused for the auto-upload call. It is *not* refreshed mid-session; sessions are bounded to 30 min idle / 2 h absolute (see §8.1). |
| CORS | Service replies with CORS headers matching the webapp origin (configured via `AI_WEBAPP_ORIGIN`). |

The service never accepts an `org_name` from the browser. Letting the client
choose an org would defeat the org-scoping guarantee.

---

## 7. Bundle delivery

On `bundle.ready`:

1. The webapp shows `BundleSummary.jsx` with the same counts the REPL prints
   today (subject types, programs, encounter types, forms, concepts).
2. Two actions:
   - **Upload to org** → `POST /sessions/{id}/upload-to-avni`. Service relays
     the ZIP to `/import/new`; emits `upload.done` with the import job id; the
     webapp routes the user to the existing `/upload` status page filtered to
     that job id.
   - **Download ZIP** → `GET /sessions/{id}/bundle` streams the file.
3. The same `BundleSummary` view also exposes the existing chat tools
   (`edit_bundle_fields`, `set_visit_schedule_rule`) by simply continuing the
   chat conversation. No new endpoints — the existing tools already mutate the
   bundle on disk; subsequent uploads pick up the updated ZIP.

This matches the user's preferred shape (auto-upload primary, download as a
fallback the user can request by typing "give me the zip" in chat).

---

## 8. State, persistence, and lifecycle

### 8.1 Session record

In-memory, single-process. One record per session id:

```python
@dataclass
class ChatSession:
    session_id: str
    org_name: str
    auth_token: str          # forwarded only to avni-server, never logged
    workdir: Path            # /tmp/avni-ai/<sid>/  — input + output bundle
    graph: CompiledGraph     # build_chat_graph(checkpointer=MemorySaver())
    config: dict             # {"configurable": {"thread_id": session_id}}
    event_queue: asyncio.Queue
    pending_interrupts: dict[str, InterruptPayload]
    created_at: datetime
    last_activity_at: datetime
```

A background reaper removes records idle > 30 min or older than 2 h absolute,
deleting `workdir` recursively. `MemorySaver` is dropped with the record.

### 8.2 What survives a tab refresh

- Session id (kept in `sessionStorage`).
- Server-side: the LangGraph state + last 200 events buffered in the session
  record so a reconnecting `EventSource` can replay via `Last-Event-ID`.

What does **not** survive:

- A process restart of the FastAPI service. The reaper drops all sessions on
  startup. The user re-uploads the xlsx. This is documented; persisting
  LangGraph state across processes is a follow-up.

### 8.3 Scaling note

This is intentionally single-process. The chat agent + pipeline are
stateful and not trivially horizontally scalable. For multi-replica
deployments the session record must move to Redis or Postgres and
`MemorySaver` swapped for `PostgresSaver`. Called out here as a known
deferred problem; not built in this SDD.

---

## 9. Failure modes

| Stage | Failure | Behaviour |
|---|---|---|
| Session create | `/me` returns 401 | Service returns 401; webapp redirects to login. |
| Upload xlsx | parse error in `parse_documents` | `error` SSE event with the parser warning text; session stays alive; user can re-upload. |
| Pipeline node | any node raises | `error` SSE event with the node name + exception summary; session marked terminal; `session.closed`. |
| `interrupt()` resolve | client posts invalid `change_id` | 400 response on `/resolve`; the pending HITL stays pending. |
| `bundle.ready` | ZIP write fails (disk full) | `error` SSE event; session terminal. |
| Auto-upload | `/import/new` returns non-2xx | `upload.done` SSE with `status: "failed"` + `details`; user can retry or download instead. |
| Auto-upload | admin token expired mid-session | Service returns 401 on `/upload-to-avni`; webapp prompts re-auth and retries. |
| SSE disconnect | client EventSource drops | Server keeps the session alive; on reconnect, replays events buffered after `Last-Event-ID`. |
| Concurrent tab | same `session_id` opened twice | Second `EventSource` is rejected (`409 Conflict`); one stream per session. |

Logging: every error event is also logged to `logs/web.log` with the
`session_id` tagged; the auth token is **never** logged. The error message
shown to the user is the same as the one the REPL would print, so the failure
surface is consistent between transports.

---

## 10. Files to create / change

### avni-ai-tools (this repo)

| File | Status | Description |
|---|---|---|
| `src/web/__init__.py` | new | Package marker. |
| `src/web/app.py` | new | FastAPI app + lifespan setup. |
| `src/web/sessions.py` | new | `ChatSession`, in-memory store, reaper. |
| `src/web/auth.py` | new | `/me` verification, org extraction, token relay. |
| `src/web/events.py` | new | SSE encoder, event queue, `Last-Event-ID` replay buffer. |
| `src/web/routes/sessions.py` | new | `POST /sessions`, `DELETE /sessions/{id}`. |
| `src/web/routes/upload.py` | new | multipart upload, `/upload-to-avni`. |
| `src/web/routes/chat.py` | new | `/message`, `/resolve`, `/events`. |
| `src/web/routes/bundle.py` | new | `GET /bundle`. |
| `src/web/chat_service.py` | new | Wraps `build_chat_graph` + `MemorySaver`, translates LangGraph events into SSE event payloads, handles `interrupt()` ↔ `Command(resume=…)`. |
| `pyproject.toml` | edit | Add `fastapi`, `uvicorn[standard]`, `python-multipart`, `sse-starlette`. Add `[project.scripts]` entry `avni-ai-web = "web.app:run"`. |
| `.env.example` | edit | Document `AVNI_SERVER_BASE_URL`, `AI_WEBAPP_ORIGIN`, `AI_SESSION_DIR`, `AI_SESSION_IDLE_MIN`, `AI_SESSION_MAX_HOURS`. |
| `README.md` | edit | New "Running as a service" section pointing at `avni-ai-web`. |

### avni-webapp

| File | Status | Description |
|---|---|---|
| `src/adminApp/aiAssistant/AiAssistantPage.jsx` | new | Route component, layout shell. |
| `src/adminApp/aiAssistant/ChatPanel.jsx` | new | Message list + composer (markdown rendering). |
| `src/adminApp/aiAssistant/UploadDropzone.jsx` | new | Multi-file xlsx dropzone. |
| `src/adminApp/aiAssistant/ConfirmationCard.jsx` | new | One pending change → Yes / No / Edit. |
| `src/adminApp/aiAssistant/BundleSummary.jsx` | new | Counts + "Upload to org" / "Download". |
| `src/adminApp/aiAssistant/api.js` | new | axios + EventSource wrapper. |
| `src/adminApp/aiAssistant/useChatSession.js` | new | React hook for session lifecycle. |
| `src/adminApp/aiAssistant/types.js` | new | Event payload typedefs. |
| `src/adminApp/AdminApp.jsx` (or matching router) | edit | Mount `/admin/ai-assistant` behind the org-admin role guard. |
| `src/common/constants/AppRoutes.js` | edit | Register the new route + nav entry. |

No changes required to `avni-server`. The integration uses only already-public
endpoints (`/me`, `/import/new`, `/import/status`).

---

## 11. Open questions

1. **AdminApp vs DataEntryApp host.** `/upload` lives in
   `src/adminApp/`. Confirm this route is the right neighbour, or whether the
   feature belongs in `dataEntryApp/` because the user-facing workflow is
   closer to data entry than admin metadata management. Default in this SDD:
   `adminApp/`.
2. **SSE vs websockets.** SSE is simpler, one-way, replays on reconnect, and
   matches the agent → browser direction. Browser → server is plain POSTs.
   Confirm before implementing — switching later means rewriting `events.py`
   and `api.js`.
3. **Process restart resilience.** Acceptable to drop sessions on restart in
   v1, or does the service need a persistent checkpointer (`PostgresSaver`)
   from day one? This SDD assumes drop-on-restart with a clear user message.
4. **Token storage during auto-upload.** The admin's bearer token sits in
   memory for the session lifetime to support `/upload-to-avni`. Confirm
   that is acceptable, or whether the auto-upload should require the
   browser to re-attach a fresh token at upload time (one extra round-trip).

---

## 12. Out of scope (recap)

- Operator (cross-org) UX with org selector.
- Multi-replica horizontal scaling of the FastAPI service.
- Persisting LangGraph state across service restarts.
- Editing the rule KB catalog from the browser.
- Token-level streaming of LLM output.
- Embedding the generator inside the Java `avni-server` process.
- Any change to the generation, parsing, or rule-validation logic itself.
