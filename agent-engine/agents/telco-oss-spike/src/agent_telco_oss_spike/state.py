"""Agent state definition for Telco OSS Spike."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class HelloWorldState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
