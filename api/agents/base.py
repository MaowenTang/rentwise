"""Agent base + shared types.

Every agent implements `handle(message, session) -> AgentReply`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

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
