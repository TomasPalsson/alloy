"""Hooks example: audit every tool call, block a destructive one before it runs.

Set up and run:

    export AZURE_AI_PROJECT_ENDPOINT=https://<your-project>.services.ai.azure.com/api/projects/<project>
    az login
    uv run examples/hooks.py
"""

from __future__ import annotations

from alloy import Agent, tool
from alloy.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry


class AuditHook(HookProvider):
    """Logs every tool call: its name and arguments going in, its result coming out."""

    def register_hooks(self, registry: HookRegistry) -> None:
        """Listen for both tool-call events."""
        registry.add_callback(BeforeToolCallEvent, self._log_call)
        registry.add_callback(AfterToolCallEvent, self._log_result)

    def _log_call(self, event: BeforeToolCallEvent) -> None:
        print(f"[audit] calling {event.tool_use.name}({event.tool_use.arguments})")

    def _log_result(self, event: AfterToolCallEvent) -> None:
        print(f"[audit] {event.tool_use.name} -> {event.result.output}")


class GuardrailHook(HookProvider):
    """Blocks any tool whose name looks destructive, without running it."""

    BLOCKED_PREFIXES = ("wipe_", "delete_")

    def register_hooks(self, registry: HookRegistry) -> None:
        """Listen for BeforeToolCallEvent — the only event a guardrail needs."""
        registry.add_callback(BeforeToolCallEvent, self._check)

    def _check(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.name.startswith(self.BLOCKED_PREFIXES):
            event.cancel_tool = f"{event.tool_use.name!r} is blocked by policy"


@tool
def check_disk_space(volume: str) -> str:
    """Report free space on a volume.

    Args:
        volume: The volume to check, e.g. "/data".
    """
    return f"{volume}: 42% free"


@tool
def wipe_database(name: str) -> str:
    """Irreversibly delete a database. Destructive — GuardrailHook blocks this one.

    Args:
        name: The database to wipe.
    """
    return f"{name} wiped"


def main() -> None:
    """Ask the agent to check disk space, then attempt a wipe the guardrail blocks."""
    agent = Agent(
        model="gpt-5-mini",
        system_prompt="You are an ops assistant. Use tools as asked, one call at a time.",
        tools=[check_disk_space, wipe_database],
        name="hooks-demo-agent",
        hooks=[AuditHook(), GuardrailHook()],
    )
    result = agent("Check disk space on /data, then wipe the scratch database.")
    print(result)


if __name__ == "__main__":
    main()
