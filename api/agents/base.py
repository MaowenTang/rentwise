"""Agent base + shared types.

Every agent implements `handle(message, session) -> AgentReply`. The
optional `tool_use_loop()` helper runs Anthropic's tool-use protocol so
a lead agent can call other agents' skills (see agents/tools.py).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from anthropic import Anthropic

DEFAULT_MODEL = os.environ.get("RENTWISE_MODEL", "claude-sonnet-4-6")


@dataclass
class AgentReply:
    agent: str                            # "search" | "property" | "location" | "outreach"
    text: str                             # markdown reply
    metadata: dict | None = None          # parsed_filters, drafted_emails, etc.
    awaiting: list[str] | None = None     # if set, agent is asking the user
                                          # for these fields and the next
                                          # user reply should route back here
    tool_calls: list[dict] = field(default_factory=list)
                                          # log entries from any cross-agent
                                          # tool calls; surfaced as "🔧 Used:"
                                          # footer in the chat


class BaseAgent:
    name: str = "base"

    def __init__(self, client: Anthropic | None = None, model: str = DEFAULT_MODEL):
        self._client = client
        self.model = model

    @property
    def client(self) -> Anthropic:
        if self._client is None:
            self._client = Anthropic()
        return self._client

    def handle(self, message: str, session) -> AgentReply:  # noqa: ANN001
        raise NotImplementedError

    # ---------------------- tool-use coordination -------------------------

    def tool_use_loop(
        self,
        prompt: str,
        *,
        scope,                                # noqa: ANN001 — list[Listing]
        all_listings,                         # noqa: ANN001
        max_tokens: int = 1500,
        max_calls: int | None = None,
    ) -> tuple[str, list[dict]]:
        """Run an Anthropic tool-use loop with this agent as lead.

        Returns (final_text, tool_call_logs). Tool definitions come from
        agents.tools, with the lead's own tools filtered out (no self-recursion).
        Hard cap on call count via tools.MAX_TOOL_CALLS.
        """
        # Lazy import to avoid agents/tools.py → agents/base.py cycles.
        from .tools import (
            MAX_TOOL_CALLS,
            dispatch_tool,
            filter_tools_for_lead,
        )

        cap = max_calls if max_calls is not None else MAX_TOOL_CALLS
        tools = filter_tools_for_lead(self.name)
        messages: list[dict] = [{"role": "user", "content": prompt}]
        logs: list = []  # list[ToolCallLog]

        while True:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                tools=tools,
                messages=messages,
            )

            if resp.stop_reason != "tool_use":
                # Done — concat all text blocks (model may interleave
                # text and tool_use, though we hit this branch only when
                # no tool_use is pending).
                text_parts: list[str] = []
                for block in resp.content:
                    if getattr(block, "type", None) == "text":
                        text_parts.append(block.text)
                final_text = "\n".join(text_parts).strip()
                return final_text, [log.__dict__ for log in logs]

            # Replay assistant turn verbatim — tool-use protocol requires
            # the model see its own tool_use blocks in the next turn.
            messages.append({"role": "assistant", "content": resp.content})

            tool_results: list[dict] = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue

                if len(logs) >= cap:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps({
                            "error": (
                                f"tool-call budget ({cap}) exceeded; "
                                "answer with what you have"
                            ),
                        }),
                        "is_error": True,
                    })
                    continue

                args = block.input if isinstance(block.input, dict) else {}
                result, log = dispatch_tool(
                    block.name, args, scope=scope, all_listings=all_listings,
                )
                logs.append(log)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                    "is_error": bool(log.error),
                })

            messages.append({"role": "user", "content": tool_results})
