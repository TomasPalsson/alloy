# Code design — 002 hooks and serve

Medium tier, sections 1–7. Cited by path from every slice; never restated in a slice prompt.

---

## 1. Contract file

`src/alloy/contracts.py` is **unchanged by this build**. No new error variant, no new dataclass,
no new type alias. If a slice believes it needs one, that is a design change — stop and escalate.

The one permitted edit is its module docstring, amended to name the split in §7-D3.

## 2. Module layout and allow-lists

| Module | New? | May import | Must NOT import |
|---|---|---|---|
| `src/alloy/hooks.py` | **new, public** | `contracts`, stdlib, `_agent` under `TYPE_CHECKING` only | `azure.*`, `openai`, `_loop`, `_foundry`, `os.environ` |
| `src/alloy/_loop.py` | edited | `contracts`, `hooks`, stdlib | `azure.*`, `openai`, `_agent` |
| `src/alloy/_agent.py` | edited | `contracts`, `hooks`, `_loop`, `_schema`, `_versions`, `_foundry` | `azure.*`, `openai` |
| `src/alloy/_schema.py` | untouched | — | — |
| `src/alloy/_foundry.py` | untouched | — | — |
| `examples/*.py` | new | anything stdlib + `alloy` | any third-party package |

Dependency direction is strictly one-way: `_agent → _loop → hooks → contracts`.
`hooks.py` importing `Agent` for typing uses `if TYPE_CHECKING:` — a runtime import there is a
circular-import crash, and is the single most likely way a slice breaks this build.

`test_design_rules.py` globs `src/alloy/*.py` only. `examples/` is outside every design rule,
including `test_b30_exactly_two_async_defs_in_package` — so the server example may use
`async def` freely. **Nothing under `src/alloy/` may gain a third `async def`.**

## 3. Naming table — bind these exactly

| Concept | Name | Owner |
|---|---|---|
| Registry object | `HookRegistry` | `hooks.py` |
| User-implemented base | `HookProvider` | `hooks.py` |
| Register a provider | `registry.add_hook(provider)` | `hooks.py` |
| Register one callback | `registry.add_callback(event_type, callback)` | `hooks.py` |
| Fire an event, return it | `registry.emit(event)` | `hooks.py` |
| Agent's own registry | `Agent.hooks` — a property returning `HookRegistry` | `_agent.py` |
| Before-run event | `BeforeInvocationEvent(agent, prompt)` | `hooks.py` |
| After-run event | `AfterInvocationEvent(agent, result)` | `hooks.py` |
| Before-tool event | `BeforeToolCallEvent(agent, tool_use, cancel_tool=None)` | `hooks.py` |
| After-tool event | `AfterToolCallEvent(agent, tool_use, result)` | `hooks.py` |
| Cancellation reason | `cancel_tool: str \| None` — a reason string, never a bool | `hooks.py` |

Event dataclasses are **mutable** (`@dataclass`, no `frozen=True`) — mutation is the feature.
The `ToolCall`/`ToolResult` they carry stay frozen; a hook rewrites by assigning a
`dataclasses.replace(...)` copy onto the event field.

Public import path is `from alloy.hooks import ...`. These names are **not** re-exported from
`alloy/__init__.py`, matching `strands.hooks`.

## 4. Trust boundaries

Exactly two, both pre-existing and unchanged:

1. `_foundry.py` — everything crossing to Azure.
2. `_loop.run_calls` — the single place a model-supplied tool name and argument string are
   turned into a local call.

Hooks add **no** new trust boundary. A hook is the user's own code running in the user's own
process; it is not validated, sandboxed, or rate-limited. A raising hook propagates (AC-16).

## 5. Shared resources — where each event fires, and why exactly once

This is the section every slice must obey. AC-17 (identical event order on all three call
paths) is satisfied **structurally**, by there being one firing site per event, not by three
sites kept in sync by hand.

| Event | Fires in | Reached by |
|---|---|---|
| `BeforeInvocationEvent` | `Agent._prepare_call` | all three paths, after slice 4 makes `__call__` use it |
| `BeforeToolCallEvent` | `_loop.run_calls`, per call, before dispatch | all three paths already call `run_calls` |
| `AfterToolCallEvent` | `_loop.run_calls`, per call, after the `ToolResult` exists | same |
| `AfterInvocationEvent` | `Agent._finish` (new, extracted in slice 4) | all three paths |

Two extractions are therefore **required**, not optional polish:

- **`Agent._finish(text, tool_failures) -> AgentResult`** — appends the assistant message,
  emits `AfterInvocationEvent`, returns the result. Replaces the identical two lines currently
  ending `__call__`, `invoke_async`, and `stream_async`.
- **`__call__` must call `_prepare_call`.** It currently duplicates that method's six
  statements byte-for-byte. Leaving both and adding a hook-emit to each is how this codebase
  produced three green-tests-broken-code bugs already.

`AfterToolCallEvent` fires for **every** call that produced a `ToolResult` — including unknown
tools, malformed arguments, tools that raised, and tools a hook cancelled. There is no path
through `run_calls` that emits `Before` without a matching `After`.

### 5a. The exact order inside `run_calls`, per call — bind this literally

    1. emit BeforeToolCallEvent(agent, tool_use=call)
    2. event.cancel_tool is not None  -> cancelled result   -> emit After -> next call
    3. tools.get(name) is None        -> UnknownToolError    -> emit After -> next call
    4. json.loads(arguments) raises   -> ToolArgumentError   -> emit After -> next call
    5. a required argument is missing -> ToolArgumentError   -> emit After -> next call
    6. spec.call(**arguments) raises  -> that exception      -> emit After -> next call
    7. json.dumps(value) raises       -> TypeError           -> emit After -> next call   (FR-B2)
    8. otherwise                      -> success result      -> emit After -> next call

Two consequences a slice agent must not reinvent:

- **Cancellation outranks an unknown tool.** The emit at step 1 sits *above* the lookup at step
  3, so a hook blocking `delete_prod` blocks it whether or not a tool by that name exists. That
  is the useful guardrail semantics and it is deliberate.
- **A cancelled result's `output` is `json.dumps({"cancelled": reason})`**, mirroring
  `_failure()`'s existing `json.dumps({"error": ...})`. Its `failure` is `None` (§7-D4).

`run_calls` reads `event.tool_use` after the emit (a hook may have replaced it, AC-14) and
`event.result` after the After-emit (a hook may have replaced it, AC-15).

Multiple hooks on one event share **one** mutable event object, dispatched in the AC-02/AC-03
order. Last writer wins; each sees the previous one's value. No merge, no conflict detection,
no warning — the ordering rule is the whole conflict-resolution story.

### 5b. Signature constraint — verified, not assumed

`tests/test_slice3_tool_loop.py:189` calls `run_calls([call], {"needs_team": spec})` with two
positional arguments. The new parameters are therefore **trailing and defaulted**:

    def run_calls(calls, tools, agent=None, hooks=None) -> list[ToolResult]

Anything else breaks AC-05 at that one line.

## 6. Deliberate duplication — do not "fix" these

- The per-path `for result in run_calls(...)` block that appends `function_call_output` dicts
  stays duplicated across the three call paths for now. It differs per path (`stream_async`
  feeds it `pending_calls`, the others feed it `calls`) and the rule of three is met only on
  the *shape*, not the data. Extracting it is a follow-up, not this build.
- `examples/hooks.py` and `examples/serve.py` each define their own tool and agent. Examples
  are read in isolation; sharing a fixture between them makes both worse.

## 7. Decisions

- **D1 — `hooks.py` is public, not `_hooks.py`.** Breaks this repo's private-module convention
  on purpose, to match `strands.hooks`. The convention exists to keep `__init__` the only public
  surface; here the Strands parity is worth more than the consistency.
- **D2 — Four events, not ten.** Pattern gate: `HookProvider` is a public extension point whose
  implementations are supplied by users, not an internal abstraction over varying
  implementations — the "<2 production implementations" rule does not apply to a library's
  documented extension seam. It does apply to the *events*: model-call, message-added, and
  init events have no caller in this build and are omitted.
- **D3 — Event dataclasses live in `hooks.py`, not `contracts.py`.** `contracts.py`'s rule
  ("import from here; never redeclare") exists to prevent two modules drifting apart on a
  shared type. One owner satisfies it. `contracts.py` keeps the cross-cutting vocabulary
  (errors, `Message`, `ToolCall`, `ToolResult`); `hooks.py` owns the hook vocabulary. Its
  docstring is amended to say so, so the next reader is not left to guess.
- **D4 — A cancelled tool is not a failure.** `ToolResult.failure is None`, and the cancellation
  reason travels in `output` as `{"cancelled": "<reason>"}` so the model can explain itself.
  A guardrail firing correctly must never show up in `AgentResult.tool_failures`.
- **D5 — Hook exceptions propagate.** No try/except around callback dispatch. A guardrail that
  fails open is worse than no guardrail. Documented in the README, asserted by AC-16.
- **D6 — Callbacks are sync.** An async callback forces a third `async def` under `src/alloy/`
  and breaks `test_b30`. Users needing async work inside a hook can schedule it themselves.
- **D7 — The server's logic is plain functions; the handler class is a thin adapter.**
  `BaseHTTPRequestHandler.__init__` calls `setup()`, which calls `connection.makefile(...)` —
  verified: constructing one with no socket raises `AttributeError`. It *can* be faked with a
  `BytesIO`-backed stand-in, so this is awkward rather than impossible; the point stands that
  testing through it means testing Python's HTTP parser, not our dispatch. So parsing and
  dispatch live in module-level functions taking
  `bytes` and returning `(status, dict)` or an iterator of SSE frame strings. The handler reads
  the socket, calls them, writes the socket. AC-26 is satisfiable only in this shape.
- **D9 — The server binds loopback by default.** `127.0.0.1` with no flag; `--host 0.0.0.0` is
  an explicit opt-in that prints a warning naming the endpoint as unauthenticated. A container
  (AgentCore Runtime, or a Foundry hosted agent) must bind all interfaces, so the capability is
  needed — but an example that silently exposes an unauthenticated LLM endpoint on every
  interface the moment it is run is not an acceptable default.
- **D8 — One `Agent` per HTTP request.** An `Agent` owns a conversation id and a message list;
  a module-level agent would leak one caller's history into another's (AC-25). The cost is a
  `list_versions` round trip per request. Stated in the example's docstring, with the
  session-keyed alternative described and not built.

---

## Per-slice contract blocks

### Contract for slice 1 — carried bug fixes
Touches `src/alloy/_agent.py` (one line, `__init__`) and `src/alloy/_loop.py` (move one line
inside an existing `try`). Adds no name from §3. Does not touch `hooks.py` — it does not exist
yet. `contracts.py` unchanged.
For FR-B1 the stored spec is read via the module-level `_TOOL_SPEC_ATTRIBUTE` constant already
defined in **both** `_agent.py` and `_schema.py` with the same value — use `_agent.py`'s.

### Contract for slice 2 — `alloy.hooks`, standalone
Creates `src/alloy/hooks.py` only. Binds every name in §3 exactly. Imports `contracts` and
stdlib; imports `Agent` **only** under `if TYPE_CHECKING:`. Does not edit `_agent.py` or
`_loop.py` — no agent wiring in this slice. `emit` returns the event it was given so callers
can read mutated fields. Before-callbacks run in registration order; After-callbacks run
reversed. `HookProvider` is a runtime-checkable `Protocol` or a plain base class with
`register_hooks(self, registry: HookRegistry) -> None`.

### Contract for slice 3 — tool events inside `run_calls`
Edits `src/alloy/_loop.py` only, plus tests. `run_calls` gains a trailing
`hooks: HookRegistry | None = None` parameter, defaulted so every existing caller is unchanged
(AC-05). Fires `BeforeToolCallEvent` before dispatch and `AfterToolCallEvent` after the
`ToolResult` exists, on every branch — unknown tool, malformed JSON, missing argument, raised
tool, cancelled tool, success. Honours `cancel_tool` per §7-D4 and a replaced `event.tool_use`
per AC-14, and a replaced `event.result` per AC-15. Does not touch `_agent.py`.
Red phase also moves the duplicated `_StubConversation` / `_StubConversations` into
`tests/conftest.py` and points existing test files at it — test-only work, no source change.

### Contract for slice 4 — invocation events and the shared-path refactor
Edits `src/alloy/_agent.py` only, plus tests. Three changes, in this order:
1. `__call__` replaces its duplicated setup with `client = self._prepare_call(prompt)`.
2. `_prepare_call` emits `BeforeInvocationEvent` after recording the prompt message.
3. A new `_finish(text, tool_failures)` appends the assistant message, emits
   `AfterInvocationEvent`, returns the `AgentResult`; all three paths end by calling it.
`Agent.__init__` gains `hooks: Sequence[HookProvider] = ()`; `Agent.hooks` is a property
returning the live `HookRegistry`. All three paths pass `self._hooks` into `run_calls`.
Adds no `async def`. Must not alter the `create_kwargs` sent to the backend.

### Contract for slice 5 — `examples/hooks.py` and README
Creates `examples/hooks.py`; edits `README.md`. Imports only `alloy`, `alloy.hooks`, and
stdlib. Shows one hook that logs every tool call and one that cancels a named tool. No source
change under `src/alloy/`.

### Contract for slice 6 — status-code map, bound literally

| Situation | Status | Body |
|---|---|---|
| `GET /ping` | 200 | `{"status": "Healthy"}` |
| `POST /invoke` or `/invocations`, ok | 200 | `{"text": ...}` (or SSE when `stream` is true) |
| body is not valid JSON | 400 | `{"error": "..."}` |
| no `prompt` key, or `prompt` is `""` | 400 | `{"error": "..."}` |
| any unknown path | 404 | `{"error": "..."}` |
| the agent raised (`AlloyError` and subclasses, or anything else) | **502** | `{"error": "<ClassName>", "message": "<str(exc)>"}` |

400 means the caller sent something wrong; 502 means the caller was fine and the backend was
not. The handler catches broadly so one bad request cannot kill `ThreadingHTTPServer`, logs the
traceback to stderr, and serializes **only** `type(exc).__name__` and `str(exc)` — never
`repr`, never the traceback, never `exc.__dict__`, because `BackendAuthError`'s no-secrets
guarantee must survive the trip through HTTP.

### Contract for slice 6 — `examples/serve.py`, its tests, and README
Creates `examples/serve.py` and `tests/test_serve_example.py`; edits `README.md`. Shape is
fixed by §7-D7: plain functions for parsing and dispatch, a thin `BaseHTTPRequestHandler`
adapter, `ThreadingHTTPServer` on port 8080. Routes `POST /invoke` and `POST /invocations`
to the same handler, `GET /ping` to `{"status": "Healthy"}`. Binds `127.0.0.1` unless
`--host` says otherwise (§7-D9). Tests call the plain functions with
a fake agent factory — they bind no socket and reach no network. Stdlib only; adding any
dependency to `pyproject.toml` fails AC-18. Also corrects README's "Known-unverified" table
(AC-29): both rows were confirmed live during the 001 build.
