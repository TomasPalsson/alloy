"""On-call assistant example, backed by a real Azure AI Foundry agent.

Set up and run:

    export AZURE_AI_PROJECT_ENDPOINT=https://<your-project>.services.ai.azure.com/api/projects/<project>
    az login
    uv run examples/oncall.py
"""

from __future__ import annotations

from alloy import Agent, tool


@tool
def check_service_status(service_name: str) -> str:
    """Look up whether a service is currently healthy.

    Args:
        service_name: The service to check, e.g. "checkout-api".
    """
    known_incidents = {"checkout-api": "degraded — elevated latency since 09:12 UTC"}
    return known_incidents.get(service_name, "no known incidents")


def main() -> None:
    """Ask the on-call agent about checkout-api and print its answer."""
    agent = Agent(
        model="gpt-5-mini",
        system_prompt="You are an on-call assistant. Use tools to check real service status.",
        tools=[check_service_status],
        name="oncall-assistant",
    )
    result = agent("Is checkout-api having problems right now?")
    print(result)


if __name__ == "__main__":
    main()
