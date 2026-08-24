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
