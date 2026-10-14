"""LLM provider selection for Telco OSS Spike."""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from langchain_core.language_models import BaseChatModel

# Same default as the legacy demo runtime (OPENAI_MODEL, gpt-4o).
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")


def build_llm(temperature: float = 0) -> BaseChatModel:
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_API_KEY is missing; add it to .env or project secrets")
    return ChatOpenAI(
        model=MODEL,
        temperature=temperature,
        api_key=SecretStr(api_key),
        use_responses_api=True,
    )
