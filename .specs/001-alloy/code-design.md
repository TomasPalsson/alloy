# Code design — alloy (Medium: sections 1–7)

## 1. Contract file + ubiquitous language

**`src/alloy/contracts.py`** — runtime-importable. Slice agents import from it, never edit it.
A type you need that is not there is an escalation, never a local declaration.

| Canonical | identifier | defined in | banned synonyms |
|---|---|---|---|
| tool spec | `ToolSpec` | contracts | Function, ToolDef, Schema, FunctionTool |
| agent result | `AgentResult` | contracts | Response, Completion, Reply |
| system prompt | `system_prompt` | public API | instructions, prompt, sys_prompt |
| fingerprint | `fingerprint` | `_versions` | hash, digest, signature |

Public API is sync except `Agent.invoke_async` and `Agent.stream_async`. Both are `async def` and
both reach the sync backend through `asyncio.to_thread` — they never block the caller's event loop.
Enforced by `tests/test_design_rules.py::test_only_two_async_defs` (AST walk of `src/alloy/`).

## 2. Type contracts & trust boundaries

| Boundary | untrusted shape | parse fn | failure granularity |
|---|---|---|---|
| Backend response | `Any` | `_loop.extract_tool_calls` | per response; unparseable → `AlloyError` |
| Tool arguments (model-authored) | `str` JSON | `_loop.decode_arguments` | per call → `ToolResult.failure` |
| Function signature | `Callable[..., Any]` | `_schema.derive` | per parameter; names it |
| `AZURE_AI_PROJECT_ENDPOINT` | `str | None` | `_foundry.resolve_endpoint` | absent → `AlloyError` naming the variable |

Normalize redundancy, reject ambiguity. Banned: coercing `""` → `None`; defaulting an unrecognized
tool name; truncating tool output.

## 3. Error taxonomy

Closed set in `contracts.py`: `AlloyError` · `ToolSchemaError` · `ToolArgumentError` ·
`UnknownToolError` · `BackendAuthError` · `VersionCapError` · `StreamingUnsupportedError`. Adding a variant is a design
change — escalate. All **thrown**, except a tool's own exception, which is **returned** on
`ToolResult.failure` and surfaced on `AgentResult.tool_failures`. Backend errors chain the SDK
exception as `__cause__`.

## 4. Module boundaries

`.importlinter` ships the closed world. Anything not listed is a bug.

| module | layer | may import | exports | seam |
|---|---|---|---|---|
| `contracts.py` | 0 | stdlib | shared types + errors | contract |
| `_schema.py` | 1 | contracts, stdlib | `derive`, `tool` | schema derivation |
| `_loop.py` | 1 | contracts, stdlib | `extract_tool_calls`, `decode_arguments`, `run_calls` | tool loop |
| `_versions.py` | 1 | contracts, stdlib | `fingerprint` | version identity |
| `_foundry.py` | 1 | contracts, `azure.*`, `openai` | `FoundryClient` | Azure adapter |
| `_agent.py` | 2 | contracts, `_schema`, `_loop`, `_versions`, `_foundry` | `Agent` | orchestration |
| `__init__.py` | 3 | contracts, `_agent`, `_schema` | `Agent`, `tool`, errors | public API |

**`_foundry.py` is the only module that may import `azure.*` or `openai`.** A test walks every other
module's AST and fails on either import.

## 5. Shared resources & construction

| resource | constructed-by | passed-how | received-by |
|---|---|---|---|
| Foundry client | `_foundry.FoundryClient` | ctor arg `client=` (default `None` → build one) | `Agent` |
| Endpoint | env `AZURE_AI_PROJECT_ENDPOINT` | ctor arg `endpoint=` overrides | `FoundryClient` |
| Conversation id | `FoundryClient`, first call | held on the `Agent` instance | `_agent` |

`AZURE_AI_PROJECT_ENDPOINT` is the only env var read, and only `_foundry.py` may read it — enforced by
`tests/test_design_rules.py::test_env_reads_confined_to_foundry` (AST walk for `os.environ`/`os.getenv`).
Tests inject a stub through `client=` — no interface to implement, only a shape to match.

## 6. Deliberately duplicated — do NOT consolidate

- **`json` calls.** Each module calls `json` directly. No `_json.py`.
- **Error messages.** Written at the raise site. No message-template module.
- **Argument validation is duplicated on purpose.** `_schema.derive` validates a *signature* at
  decoration time; `_loop` validates *model-supplied arguments* against the derived schema at call
  time, raising `ToolArgumentError` before the tool runs. These look similar and are not — do NOT
  extract a shared validator. `_loop` owns Journey 1's absent-argument edge case.
- **No retry/backoff anywhere.** `azure-ai-projects` owns retries. Enforced by
  `tests/test_design_rules.py::test_no_retry_primitives` (grep for `time.sleep`, `tenacity`,
  `backoff` outside `tests/`).
- **No `utils/`.** A helper used by one module lives in that module. Enforced by
  `tests/test_design_rules.py::test_no_utils_module` (path check over `src/alloy/`).

## 7. Decisions

- Azure seam, facing **one production backend and a test stub (the pattern gate counts a stub as zero)**: chose **concrete `_foundry.FoundryClient` + an `.importlinter` contract**, rejected **a `Backend` Protocol**, to contain blast radius without a one-implementer interface, accepting **a second backend means editing `_agent.py`, not adding a class**. `Makes hard:` a second backend — touches `_agent.py`, `_foundry.py`.
- Lifecycle, facing **Foundry's 1,000-version cap and repeated script runs**: chose **`fingerprint(...)` gating `create_version`**, rejected **create-on-construct**, to get free construction and idempotent reruns, accepting **a collision would silently reuse a stale version**. `Makes hard:` per-call definition overrides — touches `_agent.py`, `_versions.py`.
- Tool failure, facing **AC-005 "the run does not raise"**: chose **`ToolResult.failure` + `AgentResult.tool_failures`**, rejected **propagating**, for model-side recovery, accepting **a silent failure is visible only if the caller looks**. `Makes hard:` fail-fast callers — touches `_loop.py`, `_agent.py`.
- Streaming, facing **A-2 (Low confidence: `stream=True` unverified)**: chose **raising `StreamingUnsupportedError`**, rejected **chunking a complete response**, for an honest failure, accepting **no streaming until Foundry is confirmed**. `Makes hard:` a streaming-always API — touches `_agent.py`.
- Async, facing **`get_openai_client()` returning a documented SYNC `openai.OpenAI`, with an async variant only hinted at**: chose **`asyncio.to_thread` for `invoke_async`, and a worker thread pumping the sync stream iterator into an `asyncio.Queue` for `stream_async`**, rejected **`azure.ai.projects.aio`**, to use only verified surface without blocking the event loop, accepting **one thread hop per call and a queue to drain on cancellation**. `Makes hard:` true end-to-end async — touches `_foundry.py` only.

---

# Per-slice contract blocks

## Contract for this slice — Slice 1: Walking skeleton
CONTRACT   `src/alloy/contracts.py` — import from it. A type you need that is not there is an escalation, never a local declaration.
NAMES      `ToolSpec`(tools) not Function/ToolDef/Schema · `AgentResult` not Response/Completion · `system_prompt` not instructions/prompt
MODULE     `src/alloy/_schema.py` layer 1 · may import: contracts, stdlib · exports: `derive`, `tool` · seam: schema derivation
           `src/alloy/_agent.py` layer 2 · may import: contracts, `_schema`, `_foundry` · exports: `Agent` · seam: orchestration
           `src/alloy/__init__.py` layer 3 · exports: `Agent`, `tool`, errors · seam: public API
CALLS      `derive(fn: Callable[..., Any]) -> ToolSpec`
           `Agent.__init__(self, *, model: str, system_prompt: str = "", tools: Sequence[object] = (), name: str | None = None, endpoint: str | None = None, credential: object | None = None, client: object | None = None) -> None`
           `Agent.__call__(self, prompt: str) -> AgentResult`
DUPLICATE  `json` is called directly here — do not add a `_json.py`. Error strings are written at the raise site.
THE FIVE   (1) NEVER invent an error type, field name or result shape that already exists in the contract — copy the literal declaration. (2) NEVER type a boundary function's parameter as the narrow type; the narrow type appears only as the RETURN of a fallible function. (3) NEVER add a mode, flag, boolean or extra required parameter to a shared abstraction the design handed you — duplicate it inside your slice and say so in your completion note; and before extracting anything, write the signature first, because a flag needed at birth disproves the extraction. (4) NEVER refactor, rename or restructure outside your slice — a change to an unlisted file is a defect. (5) NEVER abbreviate inside an identifier. Spell the word.

## Contract for this slice — Slice 2: Schema derivation, complete
CONTRACT   `src/alloy/contracts.py`
NAMES      `ToolSpec` · `ToolSchemaError` · `JsonSchema` not Schema/dict
MODULE     `src/alloy/_schema.py` layer 1 · may import: contracts, stdlib · exports: `derive`, `tool` · seam: schema derivation
           `src/alloy/__init__.py` layer 3 · re-export `ToolSchemaError` · seam: public API
CALLS      `derive(fn: Callable[..., Any]) -> ToolSpec` · `tool(fn=None, *, name: str | None = None, description: str | None = None)`
DUPLICATE  Google-style docstring parsing lives here only. No shared `_docstring.py`. Signature
           validation lives here; ARGUMENT validation is `_loop`'s — do not share a validator.
THE FIVE   (as Slice 1)

## Contract for this slice — Slice 3: Tool-call loop
CONTRACT   `src/alloy/contracts.py`
NAMES      `ToolCall`(calls) · `ToolResult`(results) · `UnknownToolError`
MODULE     `src/alloy/_loop.py` layer 1 · may import: contracts, stdlib · exports: `extract_tool_calls`, `decode_arguments`, `run_calls` · seam: tool loop
           `src/alloy/_agent.py` layer 2 · wire the loop into the run · seam: orchestration
           `src/alloy/__init__.py` layer 3 · re-export `UnknownToolError`, `ToolArgumentError` · seam: public API
CALLS      `extract_tool_calls(response: Any) -> list[ToolCall]` · `decode_arguments(raw: str) -> dict[str, Any]` · `run_calls(calls: Sequence[ToolCall], tools: Mapping[str, ToolSpec]) -> list[ToolResult]`
           `Agent.tool` -> attribute namespace over the agent's own `ToolSpec` map; `agent.tool.<name>(**kwargs)` runs it with NO model call. Unknown attribute -> `UnknownToolError`.
DUPLICATE  A tool's exception is RETURNED on `ToolResult.failure`, never raised. No retry loop.
           Arguments not matching the derived schema raise `ToolArgumentError` BEFORE the tool runs
           (Journey 1 edge case). Do not reuse `_schema`'s signature validator for this.
THE FIVE   (as Slice 1)

## Contract for this slice — Slice 4: Conversation state and history
CONTRACT   `src/alloy/contracts.py`
NAMES      `Message`(messages) not Turn/Entry/ChatMessage
MODULE     `src/alloy/_agent.py` layer 2 · seam: orchestration
CALLS      `Agent.messages` -> `list[Message]` (read-only view)
DUPLICATE  Conversation id is held on the `Agent` instance; do not add a session store.
THE FIVE   (as Slice 1)

## Contract for this slice — Slice 5: Version lifecycle
CONTRACT   `src/alloy/contracts.py`
NAMES      `fingerprint` not hash/digest/signature · `VersionCapError`
MODULE     `src/alloy/_versions.py` layer 1 · may import: contracts, stdlib · exports: `fingerprint` · seam: version identity
           `src/alloy/_agent.py` layer 2 · call `fingerprint` before each run · seam: orchestration
           `src/alloy/__init__.py` layer 3 · re-export `VersionCapError` · seam: public API
CALLS      `fingerprint(model: str, system_prompt: str, tool_schemas: Sequence[JsonSchema]) -> str`
DUPLICATE  Hashing lives here only. `_agent.py` calls it; it never re-implements it.
THE FIVE   (as Slice 1)

## Contract for this slice — Slice 6: Async — streaming (PRIORITY) and invoke
CONTRACT   `src/alloy/contracts.py`
NAMES      `StreamEvent` not Chunk/Delta/Event · `StreamingUnsupportedError`
MODULE     `src/alloy/_agent.py` layer 2 · seam: orchestration
           `src/alloy/_foundry.py` layer 1 · owns the thread hop · seam: Azure adapter
           `src/alloy/_loop.py` layer 1 · emit `current_tool_use` from the loop · seam: tool loop
           `src/alloy/__init__.py` layer 3 · re-export `StreamingUnsupportedError` · seam: public API
CALLS      `Agent.stream_async(self, prompt: str) -> AsyncIterator[StreamEvent]`
           `Agent.invoke_async(self, prompt: str) -> AgentResult`
           `FoundryClient.stream(self, **kwargs) -> Iterator[Any]`  (sync; the adapter owns it)
DUPLICATE  The sync backend is reached through `asyncio.to_thread` in `_foundry.py` ONLY. `_agent.py`
           never calls `to_thread` itself. `stream_async` pumps the sync iterator on a worker thread
           into an `asyncio.Queue`; the queue is drained and the thread joined on cancellation.
           NEVER emulate streaming by chunking a complete response — raise instead (Decision 4).
THE FIVE   (as Slice 1)

## Contract for this slice — Slice 7: Foundry adapter, auth, example
CONTRACT   `src/alloy/contracts.py`
NAMES      `BackendAuthError` · `FoundryClient`
MODULE     `src/alloy/_foundry.py` layer 1 · may import: contracts, `azure.*`, `openai` · exports: `FoundryClient` · seam: Azure adapter
           `src/alloy/_versions.py` layer 1 · list/create versions by fingerprint · seam: version identity
           `src/alloy/__init__.py` layer 3 · re-export `BackendAuthError` · seam: public API
CALLS      `FoundryClient.__init__(self, *, endpoint: str | None = None, credential: object | None = None) -> None`
           `resolve_endpoint(explicit: str | None) -> str`
DUPLICATE  This is the ONLY module that may import `azure.*` or `openai`. `AIProjectClient` must be constructed with `allow_preview=True` or `get_openai_client(agent_name=...)` raises `ValueError`. Map SDK auth exceptions to `BackendAuthError` here and nowhere else; never let a token reach the message.
THE FIVE   (as Slice 1)
