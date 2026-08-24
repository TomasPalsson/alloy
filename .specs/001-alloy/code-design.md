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

Every public function is sync except `Agent.stream_async`, the only `async def`.

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

Closed set in `contracts.py`: `AlloyError` · `ToolSchemaError` · `UnknownToolError` ·
`BackendAuthError` · `VersionCapError` · `StreamingUnsupportedError`. Adding a variant is a design
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

`AZURE_AI_PROJECT_ENDPOINT` is the only env var read. Tests inject a stub through `client=` — there
is no interface to implement, only a shape to match.

## 6. Deliberately duplicated — do NOT consolidate

- **`json` calls.** Each module calls `json` directly. No `_json.py`.
- **Error messages.** Written at the raise site. No message-template module.
- **No retry/backoff anywhere.** `azure-ai-projects` owns retries. A slice that feels it needs one is
  an escalation, not a local loop.
- **No `utils/`.** A helper used by one module lives in that module.

## 7. Decisions

- Azure seam, facing **one production backend and a test stub (the pattern gate counts a stub as zero)**: chose **concrete `_foundry.FoundryClient` + an `.importlinter` contract**, rejected **a `Backend` Protocol**, to contain blast radius without a one-implementer interface, accepting **a second backend means editing `_agent.py`, not adding a class**. `Makes hard:` a second backend — touches `_agent.py`, `_foundry.py`.
- Lifecycle, facing **Foundry's 1,000-version cap and repeated script runs**: chose **`fingerprint(...)` gating `create_version`**, rejected **create-on-construct**, to get free construction and idempotent reruns, accepting **a collision would silently reuse a stale version**. `Makes hard:` per-call definition overrides — touches `_agent.py`, `_versions.py`.
- Tool failure, facing **AC-005 "the run does not raise"**: chose **`ToolResult.failure` + `AgentResult.tool_failures`**, rejected **propagating**, for model-side recovery, accepting **a silent failure is visible only if the caller looks**. `Makes hard:` fail-fast callers — touches `_loop.py`, `_agent.py`.
- Streaming, facing **A-2 (Low confidence: `stream=True` unverified)**: chose **raising `StreamingUnsupportedError`**, rejected **chunking a complete response**, for an honest failure, accepting **no streaming until Foundry is confirmed**. `Makes hard:` a streaming-always API — touches `_agent.py`.

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
CALLS      `derive(fn: Callable[..., Any]) -> ToolSpec` · `tool(fn=None, *, name: str | None = None, description: str | None = None)`
DUPLICATE  Google-style docstring parsing lives here only. No shared `_docstring.py`.
THE FIVE   (as Slice 1)

## Contract for this slice — Slice 3: Tool-call loop
CONTRACT   `src/alloy/contracts.py`
NAMES      `ToolCall`(calls) · `ToolResult`(results) · `UnknownToolError`
MODULE     `src/alloy/_loop.py` layer 1 · may import: contracts, stdlib · exports: `extract_tool_calls`, `decode_arguments`, `run_calls` · seam: tool loop
CALLS      `extract_tool_calls(response: Any) -> list[ToolCall]` · `decode_arguments(raw: str) -> dict[str, Any]` · `run_calls(calls: Sequence[ToolCall], tools: Mapping[str, ToolSpec]) -> list[ToolResult]`
DUPLICATE  A tool's exception is RETURNED on `ToolResult.failure`, never raised. No retry loop.
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
CALLS      `fingerprint(model: str, system_prompt: str, tool_schemas: Sequence[JsonSchema]) -> str`
DUPLICATE  Hashing lives here only. `_agent.py` calls it; it never re-implements it.
THE FIVE   (as Slice 1)

## Contract for this slice — Slice 6: Streaming
CONTRACT   `src/alloy/contracts.py`
NAMES      `StreamingUnsupportedError`
MODULE     `src/alloy/_agent.py` layer 2 · seam: orchestration
CALLS      `Agent.stream_async(self, prompt: str) -> AsyncIterator[dict[str, Any]]`
DUPLICATE  NEVER emulate streaming by chunking a complete response — raise instead (Decision 4).
THE FIVE   (as Slice 1)

## Contract for this slice — Slice 7: Foundry adapter, auth, example
CONTRACT   `src/alloy/contracts.py`
NAMES      `BackendAuthError` · `FoundryClient`
MODULE     `src/alloy/_foundry.py` layer 1 · may import: contracts, `azure.*`, `openai` · exports: `FoundryClient` · seam: Azure adapter
CALLS      `FoundryClient.__init__(self, *, endpoint: str | None = None, credential: object | None = None) -> None`
           `resolve_endpoint(explicit: str | None) -> str`
DUPLICATE  This is the ONLY module that may import `azure.*` or `openai`. `AIProjectClient` must be constructed with `allow_preview=True` or `get_openai_client(agent_name=...)` raises `ValueError`. Map SDK auth exceptions to `BackendAuthError` here and nowhere else; never let a token reach the message.
THE FIVE   (as Slice 1)
