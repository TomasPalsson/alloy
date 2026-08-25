# alloy

**Strands-flavoured ergonomics for Azure AI Foundry agents.**

An alloy is two metals combined into something with properties neither has alone. This is
that: the developer experience of the [AWS Strands Agents SDK](https://strandsagents.com),
fused to Azure AI Foundry's Agent Service.

> **Status: alpha, in active development.** The public API below is specified and being built
> test-first. It is not on PyPI yet.

---

## The problem

Foundry's current GA SDK makes you hand-write a JSON Schema for every function tool, call
`create_version` with a `PromptAgentDefinition`, fetch an OpenAI-compatible client, create a
`Conversation`, and service tool-call round-trips yourself.

Every one of those steps is mechanical. Every one duplicates information your Python function
already carries in its type hints and docstring — and a wrong schema fails at model-call time,
not at import time.

## The shape

```python
from alloy import Agent, tool

@tool
def get_oncall(team: str) -> str:
    """Look up who is on call for an engineering team.

    Args:
        team: Team name, e.g. platform
    """
    return ONCALL[team]

agent = Agent(
    model="gpt-4o",
    system_prompt="You help engineers find who is on call.",
    tools=[get_oncall],
)

print(agent("Who is on call for the data team?"))
```

No schema. No version bookkeeping. No conversation plumbing. The second call continues the
first.

Streaming is a sibling, not a bolt-on:

```python
async for event in agent.stream_async("Who is on call?"):
    if "data" in event:
        print(event["data"], end="")
```

## Hooks

Four lifecycle events, matching Strands' `HookProvider`/`HookRegistry` shape:

| Event | Fires |
|---|---|
| `BeforeInvocationEvent` | Once per `__call__`/`invoke_async`/`stream_async`, before any backend call |
| `AfterInvocationEvent` | Once per call, after the final text is known |
| `BeforeToolCallEvent` | Once per tool call, before the tool runs |
| `AfterToolCallEvent` | Once per tool call, after its `ToolResult` exists |

A `BeforeToolCallEvent` callback can act on the pending call two ways: set
`event.cancel_tool = "<reason>"` to block it without running it, or replace `event.tool_use`
to rewrite its arguments before it runs. An `AfterToolCallEvent` callback can replace
`event.result` to change what goes back to the model.

```python
from alloy import Agent, tool
from alloy.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

class Guardrail(HookProvider):
    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self.block_destructive)

    def block_destructive(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.name.startswith("delete_"):
            event.cancel_tool = "blocked by policy"

agent = Agent(model="gpt-4o", tools=[...], hooks=[Guardrail()])
```

**Cancellation outranks an unknown tool name.** The check happens before the tool lookup, so
a hook blocking `delete_prod` blocks it whether or not the agent actually holds a tool by that
name — that's the useful semantics for a guardrail.

**A cancelled call is not a failure.** `ToolResult.failure` is `None` for a cancelled call, and
it never appears in `AgentResult.tool_failures` — a guardrail firing correctly is a decision,
not an error.

**A hook that raises propagates.** Callbacks run outside any try/except; a guardrail that
fails silently is worse than no guardrail, so an exception raised inside a hook reaches your
code unchanged.

**Ordering.** `Before*` callbacks run in registration order; `After*` callbacks run in reverse
registration order (LIFO), matching Strands — the last hook to see a call is the first to see
its result. Callbacks are synchronous, called inline, on all three call paths.

See `examples/hooks.py` for a runnable audit-and-guardrail demo.

## Design principles

Borrowed from Strands, held as binding:

1. **A decorated function is a tool.** Types and docstring produce the schema.
2. **The agent is callable.** `agent("question")`.
3. **Construction is free.** No network call until first use.
4. **Conversation just works.** A second call continues the first.
5. **Simple stays simple, with an escape hatch.** Non-decorated tool objects pass through to
   the backend untouched.

Strands' signatures are the default; `alloy` deviates only where Azure genuinely differs —
`endpoint=` and `credential=` kwargs, and an explicit `result.text`.

## Architecture

Exactly one module imports `azure.ai.projects`. Everything else talks to a narrow backend
protocol. Two consequences:

- The entire test suite runs offline against a fake, with no Azure credentials.
- When Foundry's SDK shifts — and it is shifting; the classic Assistants API retires
  2026-08-26 — the blast radius is one file.

## Requirements

An existing Azure AI Foundry project with a model deployment, and an Entra principal with
data-plane access to it. `alloy` manages no infrastructure — it expects the endpoint:

```bash
export AZURE_AI_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
az login
```

Note that endpoint is **project**-scoped. The bare account endpoint will not work.

## Known-unverified

Honesty beats confidence on a preview platform. Two behaviours are specified but not yet
confirmed against a live Foundry endpoint:

| Behaviour | Status | How `alloy` handles it |
|---|---|---|
| Function-call round-trip shape | Assumed to follow the OpenAI Responses API | Isolated behind the backend protocol; one file changes if wrong |
| `stream=True` support | Unconfirmed on Foundry | Raises `StreamingUnsupportedError` rather than failing obscurely |

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run pyright
```

Tests require no Azure credentials and make no network calls.

## Licence

MIT
