# Spec: alloy — Strands-flavoured ergonomics over Azure AI Foundry

> **One-sentence summary**: A Python package that gives Azure AI Foundry agents the developer experience of the AWS Strands SDK.

**Status**: Draft
**Size**: Medium
**Author**: Tomas Pálsson (via flow)
**Created**: 2026-08-24
**Version**: 1.3 (async surface completed)

---

## TL;DR

**Problem**: Foundry's current GA SDK makes you hand-write a JSON Schema for every function tool, call `create_version` with a `PromptAgentDefinition`, fetch an OpenAI-compatible client, create a `Conversation`, and service tool-call round-trips yourself. Strands does none of that: a decorated function *is* a tool and an `Agent` is callable. `agent/agent.py` in this repo is that boilerplate, written out by hand.

**Solution**: `alloy`, exposing `Agent` and `@tool`, backed by `azure-ai-projects` 2.x.

**Who it's for**: The **SDK consumer** — an AWS-fluent Python engineer importing this package. Also the **maintainer** (same person, later) who needs Foundry-specific code isolated enough that an SDK break is a one-file fix.

**Design stance**: Take Strands' *principles* as binding and its *signatures* as the default. Deviate only where Azure genuinely needs something different, and say so when deviating. The five principles:
1. A decorated, typed, docstringed function is a tool. Never hand-write a schema.
2. The agent is callable: `agent("question")`.
3. Construction is free — no I/O until first use.
4. Conversation state just works; a second call continues the first.
5. Simple stays simple, with an escape hatch for raw Foundry objects.

**Non-goals (v1)**:
- No infrastructure management — the package uses an existing Foundry project; provisioning stays in `iac/`.
- No AWS or Bedrock support — it borrows Strands' shape, never calls AWS.
- No multi-agent orchestration — Foundry's equivalents are deprecated or sunset 2026-12-01.
- No cross-process session persistence — history lives in the `Agent` instance.
- No `hooks` or `callback_handler` — Strands has them; they do not earn their place in v1.
- No native async client — `invoke_async` and `stream_async` reach the confirmed sync client through
  `asyncio.to_thread`. `azure.ai.projects.aio` is only hinted at in the research, never verified.
- No custom retry/backoff — `azure-ai-projects` has its own policy.
- No typed wrappers for Foundry built-in tools (Code Interpreter, File Search, Bing) — their class names are unverified. Raw objects pass through instead.

**MVP cut line**: Section 4 `MUST` ships for v1. `SHOULD` is v1.1.

**Key decision**: Agent creation is **lazy and content-addressed**. Constructing an `Agent` performs no I/O. On first call the package hashes model + system prompt + tool schemas; it reuses a matching Foundry agent version if one exists and creates a new version only when the hash changes. Preserves free construction and avoids burning Foundry's 1,000-version cap on repeated script runs.

---

## 1. Context

### 1.1 Problem Statement

Every Foundry tool costs an engineer a hand-written JSON Schema that duplicates information already present in the function's type hints and docstring — and a wrong schema fails at model-call time, not at import time. Every script re-implements the same conversation and tool-call plumbing.

**Current workaround**: `agent/agent.py` — 126 lines, of which roughly 30 are the actual task.

**Business rationale**: This repo evaluates Foundry from an AWS background. Comparing Foundry's boilerplate against Strands' ergonomics measures boilerplate, not capability. Holding both to the same DX makes the comparison honest.

### 1.2 User Roles

| Role | Description | Volume | Key characteristic |
|------|-------------|--------|--------------------|
| SDK consumer | Engineer importing `alloy` | 1, possibly a small team | Knows Strands; will not learn Foundry's SDK to get started |
| Maintainer | Same person, after Foundry's SDK shifts | 1 | Needs Foundry-specific code in one isolated layer |

**Primary actor**: SDK consumer.
**Hidden stakeholders**: None — local library, no downstream consumers in v1.

### 1.3 Prior Art & Alternatives Considered

| Option | Status | Why |
|--------|--------|-----|
| Use Foundry's SDK directly | Rejected | The boilerplate is the problem. |
| Use Strands against Azure | Rejected | Strands targets Bedrock; it does not speak Foundry's agent/conversation resources. |
| Fork Strands, add a Foundry provider | Rejected | Inherits Strands' whole model-provider abstraction for one backend. |
| Thin Strands-flavoured wrapper | **Selected** | Smallest surface delivering the ergonomics; Foundry code stays isolated. |

---

## 2. Scope

### 2.1 In Scope

- `@tool` — derives a JSON Schema from type hints and a Google-style docstring.
- `Agent` — constructed with model, system prompt, tools; callable; retains history.
- Automatic servicing of function-tool call-backs during a run.
- `stream_async` — async iteration over response events.
- Lazy, content-addressed version reuse.
- `uv` packaging, a test suite that mocks the Azure SDK, and a README.

### 2.2 Out of Scope

As listed in the TL;DR.

### 2.3 Public API (target shape)

Strands signatures where they fit; Azure-specific additions marked.

```python
from alloy import Agent, tool

@tool                                    # or @tool(name=..., description=...)
def get_oncall(team: str) -> str:
    """Look up who is on call for an engineering team.

    Args:
        team: Team name, e.g. platform
    """
    return ONCALL[team]

agent = Agent(
    model="gpt-4o",                      # Strands
    system_prompt="You help engineers.", # Strands (maps to Foundry `instructions`)
    tools=[get_oncall],                  # Strands
    name="oncall-agent",                 # Strands; also the Foundry agent_name
    endpoint=None,                       # AZURE-SPECIFIC; defaults to $AZURE_AI_PROJECT_ENDPOINT
    credential=None,                     # AZURE-SPECIFIC; defaults to DefaultAzureCredential()
)

result = agent("Who is on call for data?")
print(result)          # stringifiable, like Strands
result.text            # AZURE-SPECIFIC explicit accessor — Strands' AgentResult
                       # attributes were never confirmed in research, so we define a clean one

result = await agent.invoke_async("...")   # Strands; async, no streaming

async for event in agent.stream_async("..."):
    ...                # StreamEvent dicts, Strands-shaped keys: data, message,
                       # current_tool_use, result

agent.tool.get_oncall(team="data")   # Strands; run a held tool directly, no model call

agent.messages         # Strands: in-memory conversation history
```

---

## 3. User Journeys

### Journey 1 — Call an agent with a custom tool (Priority: P1)

**Actor**: SDK consumer.
**Starting condition**: A Foundry project exists; `AZURE_AI_PROJECT_ENDPOINT` is set; the caller holds an Entra principal with data-plane access.
**Goal**: Get an answer that required the model to call their Python function.

**Happy path**:
1. Consumer decorates a typed function with `@tool`.
2. Consumer constructs `Agent(model=..., tools=[fn])`. No network call occurs.
3. Consumer calls `agent("question")`.
4. The package derives the schema, ensures a matching Foundry agent version exists, creates a conversation, sends the prompt.
5. Foundry returns a function-call request; the package executes the local function and submits the result.
6. The final text is returned as a stringifiable result.

**Error path — the tool raises**:
1. The decorated function raises an exception during step 5.
2. The package catches it and submits a structured error payload back to the model rather than crashing the run.
3. The model may recover or explain; the exception is attached to the result for the consumer to inspect.

**Edge cases**:
- Tool called with an argument absent from the schema: rejected before invocation with a named error.
- Model requests a tool the agent does not hold: a named error is submitted back, run continues.
- Model requests no tool at all: step 5 is skipped entirely.

**Acceptance criteria**:

| ID | Given | When | Then | Priority |
|----|-------|------|------|----------|
| AC-001 | A function with type hints and a Google-style docstring | decorated with `@tool` | a JSON Schema is produced with one property per parameter, each carrying the type and the docstring's `Args:` description | MUST |
| AC-002 | A `@tool` function with a defaulted parameter | schema is derived | that parameter is absent from `required`; non-defaulted ones are present | MUST |
| AC-003 | An `Agent` with tools | constructed | zero calls are made against the Foundry backend | MUST |
| AC-004 | An `Agent` whose backend returns a function-call item | `agent("q")` is called | the matching Python function runs with the decoded arguments, and its return value is submitted back to the backend | MUST |
| AC-005 | A `@tool` function that raises `ValueError` | the model calls it | the run does not raise; an error payload is submitted back and the raised exception is reachable on the result | MUST |
| AC-006 | A backend returning no function-call items | `agent("q")` is called | the response text is returned without any tool invocation | MUST |
| AC-007 | A model requesting an unknown tool name | the run proceeds | a named error payload is submitted back; the run does not raise | MUST |
| AC-008 | An `@tool` function with an un-derivable parameter type | decoration is attempted | `ToolSchemaError` is raised at decoration time, naming the parameter | MUST |

### Journey 2 — Multi-turn conversation (Priority: P1)

**Actor**: SDK consumer.
**Starting condition**: An `Agent` that has answered once.
**Goal**: Ask a follow-up that depends on the first answer.

**Happy path**: consumer calls `agent(...)` a second time; the same conversation is reused; the model has the earlier turns.

**Error path — backend loses the conversation**: the package surfaces the backend error unchanged rather than silently starting a new conversation.

**Edge cases**: first call creates the conversation lazily; `agent.messages` reflects both turns.

| ID | Given | When | Then | Priority |
|----|-------|------|------|----------|
| AC-010 | An `Agent` called once | called a second time | the same conversation id is passed to the backend | MUST |
| AC-011 | An `Agent` never called | `agent.messages` is read | an empty list is returned | MUST |
| AC-012 | An `Agent` called twice | `agent.messages` is read | it contains both prompts and both responses in order | MUST |

### Journey 3 — Version reuse (Priority: P2)

**Actor**: SDK consumer re-running a script.

| ID | Given | When | Then | Priority |
|----|-------|------|------|----------|
| AC-020 | A backend already holding a version whose fingerprint matches | `agent("q")` runs | no new version is created | MUST |
| AC-021 | An agent whose system prompt changed since last run | `agent("q")` runs | exactly one new version is created | MUST |
| AC-022 | A backend that cannot list versions | `agent("q")` runs | a version is created, a warning is emitted, and the run succeeds | MUST |

### Journey 3b — Pass-through and exhaustion (Priority: P2)

| ID | Given | When | Then | Priority |
|----|-------|------|------|----------|
| AC-023 | A backend reporting the version cap is reached | `agent("q")` runs | `VersionCapError` is raised naming the agent and the cap; the backend error is chained as the cause | MUST |
| AC-040 | A `tools=` list mixing a `@tool` function and a non-decorated object | the agent runs | the decorated one is sent as a derived schema; the other is forwarded unmodified | MUST |
| AC-041 | A pass-through object the backend requests by name | the run proceeds | the package does NOT try to execute it locally; it is treated as backend-hosted | MUST |

### Journey 5 — Credential failure (Priority: P1)

**Actor**: SDK consumer whose `az login` session has expired — the single most common failure in this environment.

| ID | Given | When | Then | Priority |
|----|-------|------|------|----------|
| AC-050 | A backend rejecting the credential as expired | `agent("q")` is called | `BackendAuthError` is raised, its message names re-authentication as the fix | MUST |
| AC-051 | Any `BackendAuthError` | the message and repr are inspected | no token, secret, or bearer value appears in either | MUST |

### Journey 4 — Streaming (Priority: P2)

**Actor**: SDK consumer wanting incremental output.

| ID | Given | When | Then | Priority |
|----|-------|------|------|----------|
| AC-030 | A backend yielding text deltas | `async for e in agent.stream_async("q")` | events with a `data` key arrive in order and concatenate to the full text | MUST |
| AC-031 | A backend that does not support streaming | `stream_async` is used | `StreamingUnsupportedError` is raised, naming the backend limitation | MUST |
| AC-032 | A streaming run that invokes a tool | streaming proceeds | a `current_tool_use` event is yielded before the tool result is submitted | SHOULD |
| AC-033 | A running event loop | `await agent.invoke_async("q")` | an `AgentResult` is returned and the loop was never blocked (a concurrent task made progress during the call) | MUST |
| AC-034 | A stream abandoned mid-iteration | the consumer breaks out of the `async for` | the worker thread terminates and no thread is left alive | MUST |
| AC-042 | An agent holding a tool named `get_oncall` | `agent.tool.get_oncall(team="data")` | the function runs and returns its value with NO backend call made | MUST |
| AC-043 | An agent holding no tool named `nope` | `agent.tool.nope()` | `UnknownToolError` naming the attribute | MUST |

---

## 4. Functional Requirements

### 4.1 Core Requirements

| ID | Actor | Requirement | Priority | AC |
|----|-------|-------------|----------|-----|
| FR-001 | SDK consumer | MUST derive a JSON Schema from a decorated function's type hints and Google-style docstring | MUST | AC-001, AC-002 |
| FR-002 | SDK consumer | MUST be able to construct an `Agent` without any network call occurring | MUST | AC-003 |
| FR-003 | SDK consumer | MUST be able to invoke the agent by calling it, receiving a stringifiable result exposing `.text` | MUST | AC-004, AC-006 |
| FR-004 | System | MUST execute the matching local function when the backend requests a tool call, and submit its result back | MUST | AC-004 |
| FR-005 | System | MUST convert a tool exception into an error payload rather than aborting the run | MUST | AC-005 |
| FR-006 | System | MUST reject an unknown tool request with a named error payload without aborting the run | MUST | AC-007 |
| FR-007 | SDK consumer | MUST receive `ToolSchemaError` at decoration time for an un-derivable parameter type | MUST | AC-008 |
| FR-008 | System | MUST reuse a Foundry agent version whose fingerprint matches, and create one only when it does not | MUST | AC-020, AC-021 |
| FR-009 | System | MUST degrade to version creation with a warning when the backend cannot list versions | MUST | AC-022 |
| FR-010 | SDK consumer | MUST be able to stream a response asynchronously, receiving ordered `data` events | MUST | AC-030 |
| FR-011 | System | MUST raise `StreamingUnsupportedError` when the backend does not support streaming | MUST | AC-031 |
| FR-012 | SDK consumer | MUST be able to read conversation history from `agent.messages` | MUST | AC-011, AC-012 |
| FR-013 | SDK consumer | MUST be able to mix pass-through tool objects with `@tool` functions in one `tools=` list; anything that is not `@tool`-decorated is forwarded to the backend unmodified | MUST | AC-040, AC-041 |
| FR-016 | System | MUST raise `VersionCapError` rather than a backend error when agent versions are exhausted, naming the agent and the cap | MUST | AC-023 |
| FR-014 | System | MUST surface a named `BackendAuthError` when the backend rejects the caller's credential or its token has expired, without leaking the token value | MUST | AC-050, AC-051 |
| FR-015 | System | SHOULD emit a `current_tool_use` event during a streaming run that invokes a tool | SHOULD | AC-032 |
| FR-017 | SDK consumer | MUST be able to `await agent.invoke_async(prompt)` without blocking the event loop | MUST | AC-033 |
| FR-018 | System | MUST terminate the streaming worker thread when the consumer abandons the stream | MUST | AC-034 |
| FR-019 | SDK consumer | MUST be able to invoke a held tool directly via `agent.tool.<name>(**kwargs)` with no backend call | MUST | AC-042, AC-043 |

### 4.2 Data Requirements

| Entity | Description | Key attributes | Relationships |
|--------|-------------|----------------|---------------|
| ToolSpec | Derived, immutable description of a callable tool | name, description, JSON Schema, the callable | Held by Agent; sent to backend |
| AgentFingerprint | Content hash identifying a definition | model, system prompt, ordered tool schemas | Determines version reuse |
| Conversation handle | Backend conversation identity | id | One per Agent instance, created lazily |
| Message | One turn of history | role, content | Ordered list on Agent |

**Data retention**: In-process only. Nothing is written to disk by this package.
**Data sensitivity**: Prompts and responses may contain anything the consumer sends. The package MUST NOT log them at default verbosity.

---

## 5. Non-Functional Requirements

### 5.1 Performance

| Metric | Target | Condition | Measurement |
|--------|--------|-----------|-------------|
| `Agent()` construction | 0 backend calls | always | Fake backend asserts call count == 0 |
| Schema derivation | < 5 ms | function with ≤ 10 params | pytest timing assertion |
| Version reuse check | ≤ 1 list call per Agent per process | repeated invocations | Fake backend call-count assertion |

**Performance budget**: Model latency is Foundry's. A defect here is only a defect if the package adds a round-trip Foundry did not require.

### 5.2 Security

**Authentication**: `DefaultAzureCredential`, unwrapped. The package MUST NOT accept, store, or log raw credentials or tokens.
**Authorization**: Whatever RBAC the caller's Entra principal holds. No authorization layer is added.
**Logging**: Endpoint URLs, tokens, and conversation content MUST NOT be logged at default verbosity.

### 5.3 Code quality (elevated — explicit user requirement)

| Principle | Binding requirement |
|-----------|--------------------|
| Single responsibility | Schema derivation, agent lifecycle, and the tool-call loop live in separate modules; no module does two. |
| Open/closed | Adding a tool *kind* MUST NOT require editing the run loop. |
| Liskov | Anything satisfying the tool protocol works wherever a `@tool` function does. |
| Interface segregation | Public API is `Agent`, `tool`, and the exception types. All else is `_`-prefixed. |
| Dependency inversion | `Agent` depends on a narrow backend protocol, never on `AIProjectClient` concretely. This is what makes it testable offline and survivable when Foundry's SDK shifts. |

**Layer ownership (binding).** Exactly one module — the Foundry adapter — may import `azure.ai.projects` or `azure.identity`. It is the only place that knows `AIProjectClient` must be constructed with `allow_preview=True` (without which `get_openai_client(agent_name=...)` raises `ValueError`), and the only place that maps SDK exceptions onto this package's error types. No other module may import either SDK, and a test MUST assert that.

**Measurable gate**: `ruff check` and `pyright` clean; every public symbol documented; no function over 40 lines.

### 5.4 Testability

All tests MUST pass with no Azure credentials and no network access, via a fake implementation of the backend protocol named in 5.3.

---

## 6. Dependencies

| Dependency | Why | Failure mode |
|------------|-----|--------------|
| `azure-ai-projects` 2.x | The only client for Foundry's agent resources | Pinned `>=2,<3`; the 1.x line is the deprecated generation |
| `azure-identity` | `DefaultAzureCredential` | Expired sessions surface as `BackendAuthError` (FR-014) |
| A provisioned Foundry project | The package manages no infrastructure | `iac/` provisions it |

---

## 7. Assumptions

| ID | Assumption | Confidence | If wrong |
|----|------------|-----------|----------|
| A-1 | The Responses API function-call round-trip uses OpenAI's shape (`function_call` items; `function_call_output` submitted back). | Medium | Reworks one module only, by 5.3's DIP boundary. |
| A-2 | `responses.create(stream=True)` works against a Foundry-hosted agent. | **Low** | FR-011 covers it: `StreamingUnsupportedError`, documented. |
| A-3 | ~~`list_versions` exists~~ **VERIFIED** against installed SDK 2.5.0, with `metadata` on `create_version` to carry the fingerprint. | High | n/a — resolved |
| A-4 | `gpt-4o` is deployed in the target project. | High | Configurable; `iac/` deploys it. |
| A-5 | `get_openai_client()` returns a genuine `openai.OpenAI`, so the OpenAI SDK's client-side shapes apply even where Foundry's server behaviour is unverified. | High | Confirmed by SDK source inspection in research. |

---

## 8. Open Questions

| ID | Question | Blocking? | Resolution |
|----|----------|-----------|------------|
| Q-1 | Exact function-call item shape on Foundry's Responses API. | No — isolated behind the backend protocol | One live call after `az login`; print `response.output`. |
| Q-2 | Whether `stream=True` works against Foundry. | No — FR-011 degrades honestly | Live smoke test. |
| Q-3 | ~~Signature of `list_versions`~~ **RESOLVED** 2026-08-24: `list_versions(agent_name, *, limit, order, before, include_drafts)`. | Closed | — |
