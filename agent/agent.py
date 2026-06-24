"""The conversational agent.

A normal Anthropic tool-use loop. The agent talks to the customer, calls the
read-only tools to answer questions, and calls ``initiate_transfer`` to start the
durable workflow. It is intentionally *not* wrapped in Temporal: the conversation
is ephemeral and best handled by the model's own retries. Only the money movement
is durable, and that lives in the workflow the agent starts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from anthropic import AsyncAnthropic

from agent.skills import (
    TOOLS,
    Session,
    build_system_prompt,
    dispatch_tool,
)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 1024
MAX_TOOL_ITERATIONS = 6

_anthropic: AsyncAnthropic | None = None


def _client() -> AsyncAnthropic:
    global _anthropic
    if _anthropic is None:
        # The agent's LLM calls are not inside a Temporal workflow, so the SDK's
        # own retry handling is appropriate here.
        _anthropic = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic


@lru_cache(maxsize=1)
def _system_prompt() -> str:
    return build_system_prompt()


@dataclass
class AgentTurn:
    text: str
    events: list[dict[str, Any]]


def _text_of(content_blocks: list[Any]) -> str:
    parts = [b.text for b in content_blocks if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


async def run_turn(session: Session, user_message: str) -> AgentTurn:
    """Run one user turn to completion, including any tool calls."""
    session.messages.append({"role": "user", "content": user_message})
    events: list[dict[str, Any]] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await _client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_system_prompt(),
            messages=session.messages,
            tools=TOOLS,
        )
        # Persist the assistant turn as plain dicts so the session stays
        # serializable and can be replayed on the next call.
        session.messages.append(
            {"role": "assistant", "content": [b.model_dump() for b in response.content]}
        )

        if response.stop_reason != "tool_use":
            return AgentTurn(text=_text_of(response.content), events=events)

        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            outcome = await dispatch_tool(block.name, dict(block.input), session)
            if outcome.ui_event:
                events.append(outcome.ui_event)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(outcome.result),
                }
            )

        session.messages.append({"role": "user", "content": tool_results})

    # Safety valve: too many tool rounds.
    return AgentTurn(
        text=(
            "I'm having trouble completing that. Could you restate what you'd "
            "like to do?"
        ),
        events=events,
    )


def outcome_message(result: dict[str, Any]) -> str:
    """A short, plain-language summary of a transfer's terminal result for the chat."""
    status = result.get("status")
    plan = result.get("plan", {})
    amount = plan.get("amount_cents", 0) / 100
    target = plan.get("target_account", "")
    if status == "completed":
        return (
            f"Done — ${amount:,.2f} was sent to {target}. "
            f"Confirmation {result.get('deposit_txn_id', '')}."
        )
    if status == "declined":
        return "No problem — I've cancelled that transfer. Nothing was sent."
    if status == "failed":
        return (
            "That transfer couldn't be completed, and any amount debited has been "
            "returned to your account. Want me to try again?"
        )
    if status == "expired":
        return "That transfer expired before it was approved. I can start a new one."
    return f"The transfer is now '{status}'."
