# Spec 002 — Lifecycle hooks and a runnable `/invoke` server

**Tier:** Medium · **Repo:** `alloy` · **Branch:** `flow/hooks-and-serve`
**Depends on:** `.specs/001-alloy/spec.md` (the shipped module), `.specs/001-alloy/code-design.md`

---

## 1. Why

`alloy` gives you a Foundry agent with Strands-shaped ergonomics. Two things a Strands user
reaches for on day two are missing:

1. **Hooks.** Strands exposes typed lifecycle events so you can log, meter, redact, or *block*
   a tool call without editing the agent. `alloy` has no interception point at all — the tool
   loop is closed. A user who wants "never let the model call `delete_prod`" has to fork.
2. **A servable agent.** The AWS answer to "put this agent behind HTTP" is
   `BedrockAgentCoreApp` + `@app.entrypoint`, which serves `POST /invocations` and `GET /ping`
   on `0.0.0.0:8080`. Azure's comparable surface is Foundry **hosted agents** (BYO container),
   which also expects an HTTP server in the container. `alloy` ships no example of that shape,
   so the streaming API — the feature the user named as most important — has no demonstration
   of its actual use case.

**AWS mapping.** Hooks map to Strands' `HookProvider`/`HookRegistry` (same names, same
`register_hooks(registry)` shape). The server maps to AgentCore Runtime's container contract;
the leak is that AgentCore's contract is *mandatory and fixed* (`/invocations`, `/ping`, port
8080, ARM64) and enforced by the service, whereas nothing on the Azure side validates a path
name — so the example serves both `/invoke` and `/invocations` and the choice is cosmetic.

## 2. Users

A Python developer who already has `alloy` working against a Foundry project and now wants to
(a) observe or constrain what their agent does, and (b) run it behind HTTP.

They know Python. They may know Strands. They do **not** want to install a web framework to
read an example.

## 3. Scope

### In scope
- A public `alloy.hooks` module: `HookProvider`, `HookRegistry`, and four event types.
- `Agent(hooks=[...])` plus `agent.hooks.add_hook(...)` post-construction.
- Hooks fire identically on all three call paths: `__call__`, `invoke_async`, `stream_async`.
- Mutation: a hook may cancel a tool call or rewrite its arguments or its result.
- `examples/hooks.py` — a runnable hooks demo.
- `examples/serve.py` — a runnable stdlib HTTP server exposing the agent.
- README sections for both.
- Two verified bug fixes carried in (see §7).

### Explicitly NOT in scope
- No model-call events (`BeforeModelCallEvent`/`AfterModelCallEvent`), no `MessageAddedEvent`,
  no `AgentInitializedEvent`. Four events cover watch-and-block; the rest have no caller.
- No `HookOrder` priority presets. Registration order plus LIFO-after is enough.
- No async hook callbacks. Callbacks are sync, called inline, on all three paths.
- No retry/resume semantics (`event.retry`, `event.resume` in Strands).
- The HTTP server does **not** enter `src/alloy/`. It is an example, not a shipped feature —
  the same split Strands keeps (`bedrock_agentcore` hosts, `strands` does not).
- No new runtime dependency, at all. Not FastAPI, not uvicorn, not starlette.
- No authentication, TLS, rate limiting, or multi-tenant session store on the example server.
  Because of that, the server **binds `127.0.0.1` by default** and reaching `0.0.0.0` is an
  explicit opt-in (`--host 0.0.0.0`). Choosing not to build auth and warning the operator are
  two different things; both are required here, not just the first.
- No Dockerfile, no Container Apps deployment, no IaC.

## 4. Acceptance criteria

All acceptance criteria below are **must-have**. There is no MVP cut line: the four events, the
mutation semantics, and the three server endpoints are one coherent unit, and shipping a subset
would mean shipping hooks that cannot block or a server that cannot stream — the two things
that motivated the work. The only priority split in this build is in the plan's Behavior
Inventory, which marks the three README behaviours P1 against everything else P0.

### Hooks — registry
- **AC-01** Given a `HookProvider` whose `register_hooks` adds a callback for
  `BeforeToolCallEvent`, when the agent runs a tool, then that callback is called exactly once
  with an event whose `tool_use.name` is the tool's name.
- **AC-02** Given two hooks registered for the same event type, when that event fires, then
  **Before** callbacks run in registration order.
- **AC-03** Given two hooks registered for the same event type, when an **After** event fires,
  then callbacks run in **reverse** registration order (LIFO, matching Strands).
- **AC-04** Given an agent constructed with `hooks=[h]`, when `agent.hooks.add_hook(h2)` is
  called afterwards and then the agent runs, then both hooks fire.
- **AC-05** Given no hooks are registered, when the agent runs, then behaviour is byte-for-byte
  what it was before this change (all 49 existing tests still pass, unmodified).
  **Verified before building, not assumed.** Greps against the current suite: exactly one test
  calls `run_calls` directly, with two positional arguments
  (`test_slice3_tool_loop.py:189`) — so `run_calls`'s new parameter must be optional and
  trailing; zero tests use `@tool(name=...)`, so FR-B1 changes no existing assertion; zero tests
  return a non-JSON-serializable value from a tool, so FR-B2 changes none either.

### Hooks — the four events
- **AC-06** `BeforeInvocationEvent` fires once per `__call__`/`invoke_async`/`stream_async`,
  before any backend call, carrying `agent` and `prompt`.
- **AC-07** `AfterInvocationEvent` fires once per call, after the final text is known, carrying
  `agent` and `result: AgentResult`.
- **AC-08** `BeforeToolCallEvent` fires once per tool call, before the tool runs, carrying
  `agent` and `tool_use: ToolCall`.
- **AC-09** `AfterToolCallEvent` fires once per tool call, after it runs, carrying `agent`,
  `tool_use`, and `result: ToolResult`.
- **AC-10** When a tool raises, `AfterToolCallEvent` still fires, and its `result.failure` is
  the raised exception.
- **AC-11** When the model requests an unknown tool, `BeforeToolCallEvent` still fires (the
  hook sees the attempt), and `AfterToolCallEvent`'s `result.failure` is `UnknownToolError`.

### Hooks — mutation
- **AC-12** Given a hook sets `event.cancel_tool = "blocked by policy"` on
  `BeforeToolCallEvent`, when the loop services that call, then the tool's function is **never
  invoked**, and the `ToolResult` submitted to the backend carries the reason string.
- **AC-13** A cancelled tool call produces a `ToolResult` whose `failure` is `None` — a
  guardrail block is a decision, not an error, and must not appear in
  `AgentResult.tool_failures`.
- **AC-14** Given a hook replaces `event.tool_use` with one carrying different `arguments`, when
  the tool runs, then it receives the rewritten arguments.
- **AC-15** Given a hook replaces `event.result` on `AfterToolCallEvent`, then the replaced
  result is what is submitted to the backend.
- **AC-15a** Given two hooks both rewrite the same event field, the **last one to run wins**,
  and each sees the previous one's value — because callbacks receive the same mutable event
  object in the order AC-02/AC-03 fix. There is no merge, no conflict detection, and no
  warning. Two guardrails fighting over one field is the author's problem to notice, and the
  ordering rule is what lets them resolve it deliberately.
- **AC-16** A hook callback that raises propagates to the agent's caller unchanged — it is not
  swallowed, logged, or converted to an `AlloyError`. A guardrail that fails silently is worse
  than no guardrail.

### Hooks — parity across call paths
- **AC-17** The same hook, the same agent config, and the same stubbed backend produce the same
  ordered sequence of event types under `__call__`, under `invoke_async`, and under
  `stream_async`. This is asserted by one test that runs all three and compares.
- **AC-17a** (the seam AC-17 depends on) `BeforeToolCallEvent` and `AfterToolCallEvent` fire
  from inside `_loop.run_calls` — the one call site all three paths already share — and **never**
  at the point `stream_async` discovers a tool call mid-stream via `response.output_item.done`
  (`_agent.py:269-272`). Firing at discovery would batch all Befores at stream time and all
  Afters at execution time, producing a different order from the sync paths. Discovery keeps
  yielding `{"current_tool_use": call}` as it does today; that is a stream event, not a hook.
- **AC-17b** Per tool call, `run_calls` performs its steps in exactly this order, emitting
  `AfterToolCallEvent` on every one of the six exits:
  1. emit `BeforeToolCallEvent`
  2. hook set `cancel_tool` → cancelled result, exit
  3. tool name unknown → `UnknownToolError` result, exit
  4. arguments are malformed JSON → `ToolArgumentError` result, exit
  5. a required argument is missing → `ToolArgumentError` result, exit
  6. the tool raises → that exception as the result's failure, exit
  7. the return value is not JSON-serializable → `TypeError` as the failure, exit (FR-B2)
  8. otherwise → success result
  **Cancellation therefore outranks an unknown tool**: a hook blocking `delete_prod` blocks it
  whether or not the agent actually holds a tool by that name. That is the useful semantics for
  a guardrail, and it is the reason the emit sits above the lookup.
- **AC-17c** A cancelled call's `ToolResult.output` is `json.dumps({"cancelled": "<reason>"})`,
  mirroring the existing `_failure()` convention of `json.dumps({"error": ...})` in `_loop.py`.

### The `/invoke` server example
- **AC-18** `uv run examples/serve.py` starts a server on port 8080 with **zero** installs
  beyond `uv sync` (no new dependency in `pyproject.toml`, in any group).
- **AC-19** `GET /ping` returns HTTP 200 and `{"status": "Healthy"}`.
- **AC-20** `POST /invoke` with `{"prompt": "..."}` returns HTTP 200 and a JSON body containing
  the agent's text.
- **AC-21** `POST /invoke` with `{"prompt": "...", "stream": true}` returns
  `Content-Type: text/event-stream` and emits one `data: {...}` frame per `StreamEvent`, ending
  with a frame carrying the final result.
- **AC-22** `POST /invocations` behaves identically to `POST /invoke` (AgentCore path parity).
- **AC-23** A request with a malformed JSON body returns HTTP 400 and a JSON error body — it
  does not return a stack trace and does not kill the server.
- **AC-24** A request with no `prompt` key returns HTTP 400.
- **AC-25** Two concurrent requests do not share conversation state: each request builds its own
  `Agent`, so request B never sees request A's history.
- **AC-26** The server's request handler is exercised by tests **without** binding a socket or
  reaching Azure — the handler logic is a plain function tested directly.
- **AC-26c** An empty prompt (`{"prompt": ""}`) returns HTTP 400. An empty string is a client
  mistake, not a question, and forwarding it burns a model call to produce nothing.
- **AC-26d** When the agent itself fails — `BackendAuthError`, `VersionCapError`, any
  `AlloyError` — `/invoke` returns HTTP **502** with a JSON body carrying the error class name
  and its message. The distinction is deliberate: 400 means the caller sent something wrong,
  502 means the caller was fine and the thing behind us failed. The server logs the traceback
  to stderr and stays up; one bad request never takes the process down.
- **AC-26e** A `BackendAuthError` surfaced through the server carries no token or secret in the
  response body — the error type already guarantees this, and the server must not add one by
  serializing anything beyond `type` and `str(exc)`.
- **AC-26a** With no arguments, the server binds `127.0.0.1`, not `0.0.0.0`. Startup prints one
  line naming the bound interface, so an operator can see which it got.
- **AC-26b** `--host 0.0.0.0` binds all interfaces and prints a warning that the endpoint is
  unauthenticated and must not be reachable from an untrusted network. This is the flag a
  container image would set, since both AgentCore Runtime and a Foundry hosted-agent container
  require binding all interfaces inside the container.

### Non-functional, security
- **NFR-06** The example server has no authentication by design. The safe default (loopback) and
  the documented warning on the opt-in flag are what make that acceptable in an example. The
  README says plainly that this is a demonstration of the request/response contract, not a
  production deployment.

### Documentation
- **AC-27** README gains a "Hooks" section showing a runnable cancel-a-tool example.
- **AC-28** README gains a "Serving over HTTP" section with the exact `uv run` command and a
  `curl` for each of the three endpoints.
- **AC-29** README's "Known-unverified" table is corrected: both rows were verified live against
  Foundry during the 001 build, and the table currently states the opposite.

## 5. Non-functional

- **NFR-01** Hooks add no measurable cost when unregistered: the no-hooks path performs no
  allocation per event beyond a single `if` on an empty registry.
- **NFR-02** `pyright --strict` passes with zero errors, as it does today.
- **NFR-03** `ruff check .` passes with the existing rule set, including the `D` docstring rules.
- **NFR-04** The design-rule tests in `tests/test_design_rules.py` continue to pass unmodified,
  **including** `test_b30_exactly_two_async_defs_in_package`. Hook callbacks are sync precisely
  so this invariant holds.
- **NFR-05** No `azure.*` or `openai` import enters `hooks.py` (enforced by `test_b23`).

## 6. Who maintains this

The repo author, solo, from an AWS background. The architecture note that matters: this
codebase's three shipped bugs all came from *tests written against a stub the bug's own author
invented*. Therefore the hook tests **reuse the existing stubs**, which were corrected against
live Azure during the 001 build, rather than introducing fresh ones.

## 7. Carried bug fixes (verified against source, not taken on report)

- **FR-B1** `@tool(name="x")` stores an overridden `ToolSpec` on the callable
  (`_schema.py:157`), but `Agent.__init__` calls `derive(t)` again (`_agent.py:65`), discarding
  it. Fix: read the stored spec. AC: a renamed tool is reachable as `agent.tool.<new_name>` and
  the new name is what reaches the backend schema.
- **FR-B2** `json.dumps(value)` sits outside the `try` in `run_calls` (`_loop.py:74`), so a tool
  returning a non-JSON-serializable value raises a bare `TypeError` past the "catch `AlloyError`
  alone" contract. Fix: move it inside. AC: a tool returning `datetime.now()` produces a
  `ToolResult.failure`, and the agent call returns normally.

## 8. Assumptions

- **A1** (confidence: High) Hook callbacks are synchronous. Async callbacks would force a third
  `async def` into the package and break NFR-04.
- **A2** (confidence: High) The server example creates one `Agent` per request. This costs a
  `list_versions` round trip per request and is stated as a trade-off in the example's
  docstring, with the session-keyed alternative described but not built.
- **A3** (confidence: Medium) `cancel_tool` carries a `str` reason rather than a bool. Strands
  uses a truthy field; a required reason string is strictly more useful to the model receiving
  the result, and is a deliberate divergence.
