# Inline Autopilot Cards — Software Design Document

## 1. Objective

Refactor the Avni Autopilot chat panel so that interactive cards
(`ConfirmationCard` for HITL pauses, `BundleSummary` for completed
bundles) render **inline as chat messages** rather than in a dedicated
slot above the chat. This eliminates the redundancy between the card
and the agent's prose narration of the same content, and preserves a
faithful conversation history in scrollback.

The change is contained to the `src/common/components/aiAssistant/`
feature in `avni-webapp`. No backend (avni-ai) changes; the SSE event
contract is unchanged.

---

## 2. Scope

### In scope

- `useChatSession.js` — make the `messages` array the single ordered
  record of the conversation by appending typed entries for HITL and
  bundle events alongside text messages.
- `ChatPanel.jsx` — render each message based on its `type` field
  (`"text"`, `"hitl"`, `"bundle"`).
- `ConfirmationCard.jsx` — accept a `resolved` prop. When true, render
  the same body with controls disabled and no per-change selection
  retained (the user's decisions live in the next chat text message —
  see §5).
- `BundleSummary.jsx` — render inline at the position of the
  `bundle.ready` event. The success / failure Alert blocks that today
  surface the upload outcome move out into a new `UploadResultCard`
  (§3.1, §5.2). `BundleSummary` keeps the `Upload to Avni` button and
  the bundle metadata only.
- `UploadResultCard.jsx` (new, small) — renders the success or
  failure Alert for one `upload.done` event. Inline message of type
  `"upload"`.
- `AvniAutopilotChatbot.jsx` — drop the dedicated top slots for
  `ConfirmationCard` and `BundleSummary`. The panel collapses to
  `Header + ChatPanel + Composer`.

### Out of scope

- Sticky / pinned unresolved cards at the bottom of the chat. The
  active flow pauses for user input, so no new messages arrive while a
  card is unresolved — the card stays at the bottom naturally.
- Per-card maximise / master-detail (the old `MasterDetailLayout` +
  keyboard-nav (↑↓/Y/N/E/⏎) for ≥ 8 changes). Dropped — inline
  cards already put the change list in the chat's scroll surface and
  each `ChangeCard` shows the same content the detail pane would.
  "Yes to all" / "No to all" handle the common bulk path in one
  click; per-card edit handles the targeted overrides. The
  `ChangeMasterList.jsx`, `ChangeDetail.jsx`, and `useKeyboardNav.js`
  files are removed in this phase. If keyboard speed becomes a real
  ask later, add Y / N shortcuts to the focused inline `ChangeCard`
  directly — that does not require a two-pane layout.
- Persistence of chat history across page reloads. Phase 1 produces a
  persistence-ready data shape (§6) but does not write or restore it.
- Prompt changes in `avni-ai/src/chat/prompts.py`. The agent continues
  to narrate; in the inline layout the narration sits naturally after
  the card and is no longer visually redundant. Token-saving prompt
  tweaks are a follow-up.
- Backend event contract changes. `hitl.pending`, `bundle.ready`,
  `agent.message`, `tool.call`, `tool.result` keep their current shapes
  (specs/AVNI_WEBAPP_INTEGRATION_SDD.md §4.2).

### Precondition

Today's `useChatSession.js` already handles all eight SSE event types
and the dedicated top-slot cards work end-to-end (visible in
ui_improvement_1.png).

---

## 3. Data model

### 3.1 Message shape

`messages` is an ordered array of typed entries. Every entry has
`type` and `ts`; the rest is type-specific:

```js
// Text message — same as today, with explicit type.
{ type: "text",   role: "user" | "assistant" | "system",
  content: string, ts: ISO8601,
  replacedByCard?: boolean }   // legacy, see §4.3

// HITL confirmation card.
{ type: "hitl",   interrupt_id: string,
  changes: HitlChange[],       // shape from types.js
  resolved: boolean,           // false on append; true after resolveChanges
  ts: ISO8601 }

// Bundle summary card.
{ type: "bundle", path: string,
  summary: BundleSummary,      // shape from BUNDLE_READY payload
  uploaded: boolean,           // set true once a matching upload message exists
  ts: ISO8601 }

// Upload outcome card.
{ type: "upload", job_id: string,
  status: "ok" | "failed",
  details?: string,            // optional error detail / progress text
  error_log_url?: string,      // present on failure if backend provided one
  ts: ISO8601 }
```

`uploadResult` as a separate piece of state goes away. Every
`upload.done` event becomes its own chronological entry in `messages`,
which means scrollback shows each upload outcome at the point it
happened — necessary so users can trigger generate → upload more than
once per session and see both results.

The `uploaded` flag on the `bundle` message exists so the inline
`BundleSummary`'s `Upload to Avni` button can disable itself after
the corresponding upload completes. It's flipped in the `upload.done`
handler (§4.1) by matching the most recent `bundle` message.

### 3.2 What goes away

- `pendingChanges` state and `setPendingChanges` setter.
- `bundle` state and `setBundle` setter as separate concerns —
  `bundle` becomes a derived value (`messages.find(m => m.type === "bundle")?....`)
  or stays as a convenience pointer, but the *rendering source* moves
  to the `messages` array.
- `uploadResult` state and `setUploadResult` setter. The latest
  upload outcome is the most recent `type: "upload"` entry in
  `messages`; callers that previously read `uploadResult` derive it
  from `messages` if needed.
- The dedicated top slots in `AvniAutopilotChatbot.jsx:399-414`.

---

## 4. Event handling

### 4.1 New rules in `handleEvent`

| Event           | Action                                                            |
|-----------------|-------------------------------------------------------------------|
| `agent.message` | Append `{type: "text", role, content, ts}` (existing behaviour)   |
| `tool.call`     | Append to `toolCalls` (unchanged)                                 |
| `tool.result`   | Mark `toolCalls` entry resolved (unchanged)                       |
| `hitl.pending`  | Append `{type: "hitl", interrupt_id, changes, resolved: false, ts}` |
| `bundle.ready`  | Append `{type: "bundle", path, summary, uploaded: false, ts}`     |
| `upload.done`   | Append `{type: "upload", job_id, status, details?, error_log_url?, ts}`. On `status === "ok"`, flip `uploaded: true` on the most recent `bundle` message; on `"failed"`, leave `uploaded` false so the user can retry. |
| `error`         | Update `error` (unchanged)                                        |
| `session.closed`| Update `status` (unchanged)                                       |

### 4.2 Resolve flow

`resolveChanges(resolutions)`:

1. POST to backend (unchanged).
2. Mark the most recent `hitl` message with `resolved: true`. The card
   stays in place but renders read-only (§5).
3. Append the synthesised "Applied N decisions: …" `text` message via
   `buildDecisionsSummary` (unchanged — see §5 for rationale).

### 4.3 Removing the legacy collapse path

`collapseLatestAssistantMessage` and `replacedByCard` exist solely to
hide the agent's narration that arrives *before* a card appears on
the top slot. In the inline layout the agent message is no longer
redundant with the card — it sits before/after the inline card and
narrates it normally. **Remove both.** The card itself is the
authoritative record of what the agent proposed.

If any consumer depends on the `replacedByCard` field for tests, the
field stays defined on the text type but is never set. Mark the prop
as deprecated in `ChatPanel.jsx`.

---

## 5. Resolved-card rendering

A resolved card is a frozen prompt — it shows **what was asked**, not
**what was chosen**. The user's decisions are recorded as the
text-bubble immediately after the card via `buildDecisionsSummary`.

Rationale:

- Matches conventional chat UI patterns (Claude, ChatGPT artifacts):
  the interactive widget renders the prompt; the response is a turn
  in the conversation.
- Aligns with the backend — `chat_service.py:224-235` already injects
  the resolutions to the agent as a `HumanMessage`. The user-side
  bubble is a literal echo of the same content.
- Keeps the persistence model flat: card stores the prompt, text
  message stores the answer, no embedded decision state to evolve.

`ConfirmationCard` prop additions:

| Prop       | Type      | Behaviour when `true`                                        |
|------------|-----------|--------------------------------------------------------------|
| `resolved` | `boolean` | Buttons disabled; no per-change variant highlights; "Apply" button hidden; subtle "Resolved" caption above the list. |

Compact layout (`CompactLayout` in `ConfirmationCard.jsx`) is the
only layout — master-detail is dropped (see §2 Out of scope).

### 5.2 BundleSummary + UploadResultCard split

`BundleSummary` becomes informational only: summary counts +
download link + `Upload to Avni` button. The button is enabled while
the message's `uploaded` flag is false, shows a loading state during
the in-flight POST, and disables itself once `uploaded` flips to
true. No success/failure Alert renders inside `BundleSummary` —
those move into `UploadResultCard`.

`UploadResultCard` is a thin component:

- `status === "ok"`: green Alert — `"All set! Log in to the Avni
  mobile app and start using it."` + "View status" link
  (`IMPORT_STATUS_PATH`).
- `status === "failed"`: red Alert — error message + optional
  download link for `error_log_url`.

The card appears at its own chronological position in chat (right
after the user's Upload action). On a re-upload (separate
`bundle.ready` + Upload cycle), a new `upload` message lands without
touching the previous one — full per-cycle history is preserved.

---

## 6. Persistence-ready shape (informational)

Phase 1 does not persist. But the resulting `messages` array is
intended to be the unit of future persistence. Per-entry shape:

```
text:   { type: "text",   role, content, ts }
hitl:   { type: "hitl",   interrupt_id, changes, resolved, ts }
bundle: { type: "bundle", path, summary, uploaded, ts }
upload: { type: "upload", job_id, status, details?, error_log_url?, ts }
```

The synthesised "Applied N decisions" entry is a normal `text` entry
with `role: "user"`. Tool-call progress chips are *not* persisted —
they live in the separate `toolCalls` array (transient progress UI).

When persistence is added (future SDD), the writer dumps `messages`
verbatim and the reader rebuilds session state by replaying the
array into the React state. An unresolved `hitl` entry restored after
the backend interrupt has been GC'd should be tagged stale and the
user invited to re-trigger the flow.

---

## 7. Rendering contract

`ChatPanel.jsx` renders the `messages` array in order. For each entry:

```jsx
switch (msg.type) {
  case "text":   return <MessageRow msg={msg} />;
  case "hitl":   return <ConfirmationCard
                          pendingChanges={msg}
                          resolved={msg.resolved}
                          onResolve={msg.resolved ? noop : onResolve}
                          isMaximised={false} />;
  case "bundle": return <BundleSummary
                          bundle={msg}
                          downloadBundleUrl={downloadBundleUrl}
                          onUploadToAvni={onUploadToAvni} />;
  case "upload": return <UploadResultCard msg={msg} />;
}
```

Tool-call chips continue to render after the message stream, filtered
to in-progress items (`!tc.result`), as today.

Inline cards inherit the panel's column width. Master-detail layout
is not invoked in Phase 1 regardless of change count.

---

## 8. Migration / rollout

The change is large enough to deserve its own commit. Suggested
sequence inside one PR:

1. Introduce `type` on every existing append in `useChatSession.js`
   (`type: "text"` for all current messages). No render change yet.
2. Update `ChatPanel.jsx` to switch on `type`; still only `"text"` is
   produced.
3. Append `"hitl"` and `"bundle"` entries on the corresponding events;
   stop setting `pendingChanges` / `bundle` for render purposes.
4. Append `"upload"` entries on `upload.done` and flip the matching
   `bundle` message's `uploaded` flag. Remove `uploadResult` state.
5. Add inline rendering for `"hitl"` / `"bundle"` / `"upload"` in
   `ChatPanel.jsx`. Create `UploadResultCard.jsx`. Move the
   success/failure Alert blocks out of `BundleSummary.jsx`.
6. Remove the top-slot `ConfirmationCard` / `BundleSummary` mount
   points from `AvniAutopilotChatbot.jsx`.
7. Add `resolved` prop handling in `ConfirmationCard.jsx`. In
   `resolveChanges`, flip `resolved` on the matching message after
   the backend ack.
8. Remove `collapseLatestAssistantMessage`, `replacedByCard` filter
   in `ChatPanel.jsx`, and `lastUserActionRef` if no longer needed.

No backend changes; no SDD references in avni-ai need updating.

---

## 9. Testing

- Manual: run a generate flow with a needs_confirmation pause. Verify
  the card appears inline at the chronological position, decisions
  apply, the synthesised "Applied N decisions" text message lands
  after, and the card switches to read-only.
- Manual: scroll back after multiple HITL rounds. Each past card
  should show its original prompt, disabled controls, no retained
  selection, and the user-side bubble immediately after summarising
  the decisions.
- Manual: bundle-ready flow renders `BundleSummary` inline; `Upload
  to Avni` button works; on success a separate `UploadResultCard`
  message appears after the bundle card, and the bundle card's
  Upload button becomes disabled.
- Manual: upload failure surfaces the error Alert inside the
  `UploadResultCard` (separate inline message); the bundle card's
  Upload button stays enabled so the user can retry. The retry
  produces a second `UploadResultCard` below the first.
- Manual: generate → upload → generate-again-via-edit → upload again
  in the same session leaves two `bundle` cards each followed by
  their own `upload` card, in chronological order.
- Existing automated tests for `useChatSession` may reference
  `pendingChanges` / `bundle` state directly — update assertions to
  read from the `messages` array.

---

## 10. Open questions

_(none)_
