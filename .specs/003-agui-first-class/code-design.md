# Code design — 003 AG-UI first class

Cross-slice structure only. Not a spec; never routed to spec-judge. Behaviour and acceptance
criteria live in `spec.md`. Smell families: see `flow-deepen/references/detection-heuristics.md`.

## 1. Contract file + ubiquitous language

**Contract file**: `src/alloy/agui.py`. Slice 1 creates it with every public signature below as a
stub; later slices fill bodies and never change a signature. A type you need that is not there is
an escalation.

`src/alloy/contracts.py` is NOT extended by this build. It must keep importing with zero
AG-UI dependencies present.

| Canonical | identifier | plural/collection | defined in | banned synonyms |
|---|---|---|---|---|
| AG-UI event | `event` | `events` | `ag_ui.core` | evt, msg, agui_event, ag_event |
| protocol module namespace | `import ag_ui.core as ag_ui_core` | — | every consumer | `from ag_ui.core import ToolCall`, `from ag_ui.core import Message` |
| alloy tool call | `contracts.ToolCall` | `calls` | `contracts.py` | AlloyToolCall, tool_use |
| tool call identity | `tool_call_id: str` | — | `agui.py` | toolCallId, tcid, id, and `call_id` **as a local name** — reading `contracts.ToolCall.call_id` is correct and required |
| message identity | `message_id: str` | — | `agui.py` | msg_id, messageId, mid |
| the request body | `run_input: RunAgentInput` | — | `ag_ui.core` | input, payload, request, body |
| the new turn's prompt | `prompt: str`, from `latest_user_prompt` | — | `agui.py` | message, text, query, user_input |
| per-run mutable bookkeeping | `_RunState` | — | `agui.py` | context, ctx, session, tracker |
| frontend-visible state | `state: dict[str, Any]` | — | `agui.py` | snapshot, store, model |
| state keys | `messages` · `activeToolCalls` · `completedToolCalls` · `toolFailures` | — | shape pinned in `spec.md` §4.2 | tool_calls, active, completed, failures, errors |
| translator entry point | `run_stream` | — | `agui.py` | translate, to_agui, adapt, convert |
| thread → conversation map | `ThreadStore` | — | `agui.py` | SessionStore, cache, registry, ThreadCache |
| backend conversation identity | `conversation_id: str` | — | `_agent.py` | convo_id, cid, session_id |

**Value objects**: `tool_call_id` is ALWAYS `contracts.ToolCall.call_id` verbatim — never minted,
never reformatted. `message_id` IS minted here, `uuid4().hex`, one per assistant message and one
per tool result. `thread_id`/`run_id` are echoed from `run_input` verbatim and never minted.

**Async convention**: `run_stream` is `async def` returning `AsyncIterator[ag_ui_core.BaseEvent]`,
because its only source (`Agent.stream_async`) is async. Every helper it calls is sync.

## 2. Type contracts & trust boundaries

| Boundary | untrusted input shape | parse fn | failure granularity |
|---|---|---|---|
| HTTP request body | `bytes` | `parse_run_input(raw: bytes) -> RunAgentInput` | 400 malformed JSON · 422 valid JSON, bad shape |
| `run_input.messages` — the NEW prompt | `list[ag_ui_core.Message]` | `latest_user_prompt(run_input) -> str` | raises `ToolArgumentError`-free `ValueError`; the HTTP layer turns it into 422 |
| `run_input.messages` — prior turns | `list[ag_ui_core.Message]` | none — deliberately unread | the BACKEND conversation is authoritative (A-08); client history is display-only |
| `run_input.state` | `Any` | `seed_state(run_input) -> dict[str, Any]` | never raises; non-dict is replaced, not rejected |
| Azure raw stream event | `Any` (SDK object) | `getattr(ev, "type", None)` guards, as `_loop.py` already does | unknown type is skipped, never raised |
| state values, at encode time | `Any` | `json_safe(value) -> Any` | never raises; coerces to `str` |

**Timestamps**: every event's `timestamp` is left `None`. alloy does not mint clock values.
**Absent vs null**: `exclude_none=True` on the wire. An absent optional and an explicit null must
not both appear for one field.

## 3. Error taxonomy

Declared in `contracts.py`, which already holds the closed hierarchy. **Adding a variant is a
design change — escalate, do not add locally.** This build adds **zero** new variants.

Thrown, not returned. One mapping, used by every slice:

| Condition | Transport representation |
|---|---|
| body is not JSON | HTTP 400, `{"error": "<parse detail>"}` |
| JSON but not a valid `RunAgentInput` | HTTP 422, `{"error": "<field detail>"}` |
| any exception raised during the run | inside a 200 stream: `RunErrorEvent(message=str(exc))`, then stop |
| `alloy.agui` imported without the extra | `ImportError` whose message contains `alloy-foundry[agui]` |

A `RunErrorEvent.message` is `str(exc)` only. Never a traceback, never `repr`, never a token.

## 4. Module boundaries

Enforced by `tests/test_design_rules.py`, which already AST-checks imports in this repo. Extend it;
do not add a new mechanism. **Anything not listed is a bug.**

- `src/alloy/agui.py` · layer 3 · may import: `ag_ui.core`, `ag_ui.encoder`, `.contracts`, `.hooks`, `._agent` (TYPE_CHECKING only), stdlib · exports: `run_stream`, `parse_run_input`, `latest_user_prompt`, `seed_state`, `json_safe`, `capabilities`, `check_conformance`, `ThreadStore`, `AGUI_EXTRA_HINT` · seam: **translator**
- `src/alloy/contracts.py` · layer 0 · may import: stdlib ONLY · **must never import `ag_ui`** · seam: **vocabulary**
- `src/alloy/hooks.py` · layer 1 · gains exactly one export, `HookRegistry.remove_callback` (see decision 2); no other change · **must never import `ag_ui`** · seam: **lifecycle**
- `src/alloy/_loop.py` · layer 1 · may import: `.contracts`, `.hooks`, stdlib · **must never import `ag_ui`** · seam: **tool execution**
- `src/alloy/_agent.py` · layer 2 · unchanged imports · gains ONE keyword-only `__init__` parameter, `conversation_id: str | None = None`, which seeds `self._conversation_id` instead of leaving it `None` · **must never import `ag_ui`** · seam: **orchestration**
- `examples/serve.py` · layer 4 · may import `alloy`, `alloy.agui`, `alloy.hooks`, stdlib · seam: **transport**

- `src/alloy/__init__.py` · layer 4 (public façade) · **NOT touched by this build** — `alloy.agui` is
  deliberately not re-exported here, matching `alloy.hooks`, so importing the base package never
  pulls `ag_ui` · seam: **public surface**
- `src/alloy/_foundry.py` · layer 1 · **NOT touched by this build** · **must never import `ag_ui`** · seam: **backend adapter**
- `src/alloy/_schema.py` · layer 1 · **NOT touched by this build** · **must never import `ag_ui`** · seam: **schema derivation**
- `src/alloy/_versions.py` · layer 1 · **NOT touched by this build** · **must never import `ag_ui`** · seam: **version fingerprint**

- `examples/hooks.py` · layer 4 · **NOT touched by this build** · seam: **hook demo**
- `examples/oncall.py` · layer 4 · **NOT touched by this build** · seam: **agent demo**

Six of the eleven modules above are marked NOT touched. That is a closed world, not an omission:
an edit to any of them is a defect, and slice 5 in particular must resist "while I am in
`_loop.py` anyway" changes to `_foundry.py`.

The rule that carries the optional extra: **only `agui.py` and `examples/serve.py` may name `ag_ui`.**
A test asserts this by AST over `src/alloy/`.

## 5. Shared resources & construction

| Resource | constructed-by | passed-how | received-by |
|---|---|---|---|
| `Agent` | `examples/serve.py:_build_agent` | argument | `run_stream(agent, run_input)` |
| `_RunState` | `run_stream`, once per call | closure-local, never global | its own helpers |
| `EventEncoder` | `examples/serve.py`, passed the request's `Accept` header | argument | the SSE writer only |
| `state: dict` | `seed_state(run_input)` | field on `_RunState` | patch recorder |
| tool-result signal | `run_stream` registers on `agent.hooks`, and removes it in a `finally` | `add_callback` / `remove_callback` | `_RunState` |
| `ThreadStore` | `examples/serve.py`, ONCE at module scope | closure into the handler factory | the `POST /` path, before the `Agent` is built |
| clock | nobody — no timestamps are minted | — | — |

`run_stream` MUST NOT construct an `Agent`, and MUST NOT touch a `ThreadStore`. It receives an agent
already built against the right conversation, and stays a pure translation of one run. The thread
lookup therefore happens in the CALLER, before `run_stream` is entered — that ordering is forced,
not stylistic: an agent's conversation is fixed at construction.

`ThreadStore` carries a `ponytail:` comment naming its ceiling: process-local, so a multi-instance
deployment loses continuity silently — swap in a shared store, the interface is two methods. An
empty or whitespace-only `thread_id` MUST be rejected before it reaches the store, never defaulted:
a session key that collapses to a constant puts every caller in one conversation.

`ThreadStore` is DEFINED in `agui.py` (layer 3) so a user writing their own server gets the
mechanism, but INSTANTIATED in `examples/serve.py` (layer 4) because the lifetime of a session map
is the server's, not the library's. A module-scope instance is correct here and is NOT the shared
mutable state that spec 002 banned: it holds two strings per thread and never an `Agent`, so no
conversation can leak between callers through it.

Config keys: `AZURE_AI_PROJECT_ENDPOINT` only, already read by `_foundry.resolve_endpoint`. No new
environment variable. Only the orchestrator may add a dependency.

## 6. Deliberately duplicated — do NOT consolidate

- **`EventEncoder`'s `accept` argument.** The Python encoder ACCEPTS an `accept` parameter and
  ignores it — it always emits SSE (upstream issue #2094). Pass the header anyway and take the
  content type from `get_content_type()`, so the day upstream wires negotiation up alloy gets it
  free. Do NOT hardcode `"text/event-stream"`, and do NOT write your own negotiation.

- **SSE framing.** `examples/serve.py` already has `_sse_frame`. The AG-UI route uses
  `EventEncoder.encode` instead. Two framers, deliberately: the encoder negotiates on `Accept` and
  owns the protocol's wire format. Do NOT route the existing `/invoke` through the encoder, and do
  NOT hand-format AG-UI frames with `_sse_frame`.
- **Event-type string literals.** Azure raw type strings (`"response.output_text.delta"`) appear in
  both `_loop.py` and `agui.py`. Leave them duplicated and inline. Do NOT extract a shared
  constants module: grepping the literal that arrives in the data must land on the code that runs.
- **JSON serialisation.** `_loop.py` uses `json.dumps` for tool output; `agui.py` uses `json_safe`
  for state. Different failure contracts — one may raise into a `ToolResult.failure`, one may never
  raise. Do NOT merge them.
- **The 400/422 split.** `_handle_invoke` returns 400 for both cases today. The AG-UI route
  distinguishes them. Do NOT "fix" the older route to match.

- **Thread identity vs conversation identity.** `thread_id` (the client's) and `conversation_id`
  (Azure's) stay two separate strings that happen to be mapped. Do NOT collapse them, do NOT use
  one as the other, and do NOT send `thread_id` to Azure. The service owns conversation identity
  (`_foundry.create_conversation`); the client owns thread identity.

## 7. Decisions

1. In the context of **event translation**, facing **six Azure raw types mapping to five AG-UI types
   with bracket state between them**, we chose **an explicit `if`/`elif` on `getattr(raw, "type", None)`
   in one stateful loop, literals inline** and rejected **a `HANDLERS: dict[str, Callable]` table**, to
   achieve **grep-to-running-code for every wire string**, accepting that **a seventh type edits one
   long function**. *Makes hard:* adding a raw type without reading the loop — `src/alloy/agui.py`.
2. In the context of **tool results**, facing **`stream_async` never yielding a `ToolResult`**, we
   chose **registering an `AfterToolCallEvent` callback inside `run_stream` and removing it in a
   `finally`** and rejected **changing `stream_async` to yield results**, to achieve **zero change to
   `stream_async`, verified live last round**, accepting that **`HookRegistry` has no removal method
   today, so this decision REQUIRES adding `HookRegistry.remove_callback(event_type, callback) -> None`
   — verified absent by reading `src/alloy/hooks.py`**. Without it a second `run_stream` on the same
   `Agent` emits every `TOOL_CALL_RESULT` twice. *Makes hard:* two CONCURRENT `run_stream` calls on one
   `Agent`, which stays unsupported — `src/alloy/agui.py`, `src/alloy/hooks.py`.
3. In the context of **state deltas**, facing **RFC 6902 patches with no diff library and a
   two-dependency budget**, we chose **recording the patch at the moment of mutation
   (`_RunState.apply` mutates `state` and appends to `patches` together)** and rejected **diffing
   snapshots with `jsonpatch`**, to achieve **no third dependency and patches correct by
   construction**, accepting that **any mutation not routed through `apply` is silently missing from
   the stream**. *Makes hard:* writing state from outside — `src/alloy/agui.py`, `examples/serve.py`.
4. In the context of **incremental tool arguments**, facing **`extract_tool_call_from_stream_item`
   firing only on `output_item.done`**, we chose **a sibling `extract_tool_argument_delta(raw) ->
   tuple[str, str] | None`** and rejected **widening the existing extractor's return type**, to achieve
   **no change to the contract slice 4 built against**, accepting that **`_loop.py` grows a second
   similar-looking extractor**. *Makes hard:* changing tool-call recognition — `src/alloy/_loop.py`,
   `src/alloy/_agent.py`.
5. In the context of **the optional extra**, facing **`alloy.agui` needing to fail helpfully when
   `ag_ui` is absent**, we chose **module-level `try: import ag_ui.core / except ImportError: raise
   ImportError(AGUI_EXTRA_HINT) from None`** and rejected **lazy per-function imports**, to achieve
   **one failure point at import time, not mid-stream**, accepting that **`import alloy.agui` in a
   `try` is the only feature detection**. *Makes hard:* partial support without the SDK —
   `src/alloy/agui.py`, `pyproject.toml`.

6. In the context of **thread continuity**, facing **`Agent.__init__` hardcoding
   `self._conversation_id = None` so every agent always creates a fresh conversation**, we chose
   **one keyword-only `conversation_id: str | None = None` parameter that seeds that field** and
   rejected **a public setter or mutating `_conversation_id` from outside**, to achieve **an agent
   whose conversation is fixed at construction, which is what makes `run_stream` safe to keep
   pure**, accepting that **a caller can now pass a conversation id the backend will reject, so
   the stale-id recovery row in §9 is mandatory, not optional**.
   *Makes hard:* changing conversation ownership — `src/alloy/_agent.py`, `src/alloy/_foundry.py`.

<!-- SIZE: large only -->
## 8. Hidden decisions + churn

| Decision hidden | Module | STABLE / CHURNING / PROVISIONAL |
|---|---|---|
| `tool_call_id` is alloy's `call_id`, never minted | `agui.py` | **STABLE** — slices 4, 5, 7 all depend on it |
| one `message_id` per assistant message, minted here | `agui.py` | **STABLE** — slices 3, 7 depend on it |
| the shape of `state` (`messages`/`activeToolCalls`/`completedToolCalls`/`toolFailures`) | `agui.py` | **CHURNING** — no frontend has consumed it yet |
| the `Accept`-derived content type | `serve.py` | STABLE |
| which Azure raw types are translated | `agui.py` | **CHURNING** — Appendix D is one model's behaviour, not a contract |
| `ThreadStore`'s 100-entry cap and LRU policy | `agui.py` | **PROVISIONAL** — no measurement justifies 100; it exists so the map is bounded rather than because 100 is right |
| that `ThreadStore` is process-local | `agui.py` | **PROVISIONAL** — correct for one long-lived process, silently wrong on any multi-instance host; see spec §5.5 |
| that a thread maps to exactly ONE backend conversation | `agui.py` | **STABLE** — slices 7 and 8 both depend on it |

A CHURNING `state` shape sits inside a STABLE `_RunState`. That is deliberate: the container is
fixed so slices can write against it; the dict's keys are expected to move after real frontend use.

## 9. State machines

The run's bracket state IS the conformance contract. `_RunState.phase` is a **5**-value field —
`NOT_STARTED`, `OPEN`, `TEXT_OPEN`, `FINISHED`, `ERRORED` — so an exhaustive `match` in `agui.py`
makes a missing pair a type error. Declare the `Literal` with all five or the exhaustiveness claim
is false.

**The single-open rule.** The reference client's validator (`verifyEvents` in `@ag-ui/client`) is
strictly single-threaded: while a tool call is open, it rejects ANY other event, a new
`TEXT_MESSAGE_START` included. So a stream that interleaves text and tool calls is legal-looking
and still refused by real clients. `run_stream` therefore CLOSES an open text message before
emitting `TOOL_CALL_START`, and never opens a text message while a tool call is open. This is why
the table below has no `TEXT_OPEN` row for any tool event — not an omission, a forced ordering.

| current | event | next | guard | side effect |
|---|---|---|---|---|
| `NOT_STARTED` | run begins | `OPEN` | — | emit `RUN_STARTED`, then `STATE_SNAPSHOT` of `seed_state(...)`. This is the ONLY state write that does not go through `apply` — it is construction, not mutation |
| `OPEN` | first text delta | `TEXT_OPEN` | no text message open | mint `message_id`, emit `TEXT_MESSAGE_START` |
| `TEXT_OPEN` | text delta | `TEXT_OPEN` | — | emit `TEXT_MESSAGE_CONTENT` |
| `TEXT_OPEN` | text done | `OPEN` | — | emit `TEXT_MESSAGE_END` |
| `TEXT_OPEN` | tool call seen | `OPEN` | — | emit `TEXT_MESSAGE_END` FIRST (single-open rule), then `TOOL_CALL_START`, then `_RunState.apply("add", "/activeToolCalls/<id>", ...)` |
| `OPEN` | tool call seen | `OPEN` | — | emit `TOOL_CALL_START`, then `_RunState.apply("add", "/activeToolCalls/<id>", ...)` |
| `OPEN` | tool arg delta | `OPEN` | that `tool_call_id` is active | emit `TOOL_CALL_ARGS` |
| `OPEN` | tool args done | `OPEN` | that `tool_call_id` is active | emit `TOOL_CALL_END` |
| `OPEN` | hook fires | `OPEN` | — | emit `TOOL_CALL_RESULT`, then `_RunState.apply("remove", "/activeToolCalls/<id>")` + `apply("add", "/completedToolCalls/-", ...)` as ONE `STATE_DELTA` |
| `TEXT_OPEN` | run ends | `FINISHED` | — | emit `TEXT_MESSAGE_END` FIRST, then `RUN_FINISHED` |
| `OPEN` | run ends | `FINISHED` | — | emit `RUN_FINISHED` |
| `OPEN` / `TEXT_OPEN` | backend rejects a STALE conversation id | `OPEN` | the id came from `ThreadStore` | drop the mapping, build a fresh conversation, retry ONCE. Do NOT emit `RUN_ERROR` — spec AC-40 |
| `OPEN` / `TEXT_OPEN` | any other exception | `ERRORED` | — | close an open text message if any, emit `RUN_ERROR`, emit nothing further |
| `FINISHED` / `ERRORED` | anything | — | — | **illegal; absent rows are the point** |

The `TEXT_OPEN → run ends` row is the one a naive implementation gets wrong: it closes the text
message before closing the run, so `TEXT_MESSAGE_END` is never orphaned.

## 10. Cross-slice call ladder

1. `examples/serve.py:dispatch(method, path, raw_body, agent_factory)` → routes `POST /`
2. `alloy.agui.parse_run_input(raw: bytes) -> RunAgentInput`
3. `alloy.agui.ThreadStore.resolve(thread_id: str) -> str | None` — the mapped conversation id, or `None`
4. `alloy.agui.ThreadStore.remember(thread_id: str, conversation_id: str) -> None` — records and marks used; evicts LRU past the cap
5. `alloy.Agent(..., conversation_id: str | None = None)` — built by the caller, never by `run_stream`
6. `alloy.agui.run_stream(agent: Agent, run_input: RunAgentInput) -> AsyncIterator[BaseEvent]`
7. `alloy.agui.latest_user_prompt(run_input: RunAgentInput) -> str` — newest `role="user"` message's text; raises `ValueError` when there is none
8. `alloy.agui.seed_state(run_input: RunAgentInput) -> dict[str, Any]`
9. `_RunState.apply(op: str, path: str, value: Any = None) -> dict[str, Any]`
10. `alloy.Agent.stream_async(prompt: str) -> AsyncIterator[contracts.StreamEvent]`
11. `alloy._loop.extract_tool_argument_delta(raw: Any) -> tuple[str, str] | None`
12. `alloy.agui.check_conformance(events: Sequence[BaseEvent]) -> None` — raises `AssertionError` naming the rule and the offending event type
13. `alloy.agui.capabilities() -> ag_ui_core.AgentCapabilities`

## 11. Test seams & shared fakes

- **Fake Azure stream**: one `FakeRawEvent` + `fake_stream(...)` helper in `tests/conftest.py`,
  beside the existing `StubConversation`. Public names, no leading underscore — pyright strict
  flags cross-module private use. Every slice from 3 onward builds its raw events with it.
- **Fake agent**: `tests/conftest.py` already has the pattern from `test_serve_example.py`. Reuse it;
  do not write a second one.
- Tests live in `tests/`, named `test_slice<N>_<behaviour>.py`, matching the existing convention.
- No unit test targets `_RunState` directly. Test it through `run_stream`'s emitted events.
- Tests are named for spec behaviours (`test_text_message_end_precedes_run_finished`), never for
  production symbols.
<!-- END SIZE: large only -->
