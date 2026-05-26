from __future__ import annotations

import asyncio
import contextvars
import json
import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv


def microsoft_agent_framework_available() -> bool:
    try:
        import agent_framework  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


@dataclass(frozen=True)
class AgenticRuntimeConfig:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None


class AgenticRuntimeConfigurationError(RuntimeError):
    pass


class AgenticRuntimeExecutionError(RuntimeError):
    pass


def load_agentic_runtime_config() -> AgenticRuntimeConfig:
    load_dotenv()
    provider = os.getenv("MIGRATION_AGENT_LLM_PROVIDER", "groq").strip().lower()

    if provider == "groq":
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip()
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    else:
        raise AgenticRuntimeConfigurationError(
            "Unsupported MIGRATION_AGENT_LLM_PROVIDER. Use 'groq' or 'openai'."
        )

    if not api_key:
        env_name = "GROQ_API_KEY" if provider == "groq" else "OPENAI_API_KEY"
        raise AgenticRuntimeConfigurationError(
            f"LLM usage is mandatory for this agentic solution. Set {env_name} before running agents."
        )
    if not model:
        raise AgenticRuntimeConfigurationError("LLM model is required. Set GROQ_MODEL or OPENAI_MODEL.")

    return AgenticRuntimeConfig(provider=provider, model=model, api_key=api_key, base_url=base_url)


_event_loop: asyncio.AbstractEventLoop | None = None


def _get_event_loop() -> asyncio.AbstractEventLoop:
    global _event_loop
    if _event_loop is None or _event_loop.is_closed():
        _event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_event_loop)
    return _event_loop


def run_agentic_review(agent_title: str, agent_description: str, output: dict[str, Any]) -> dict[str, Any]:
    config = load_agentic_runtime_config()
    try:
        from agent_framework import Agent
        from agent_framework.openai import OpenAIChatClient
    except Exception as exc:
        raise AgenticRuntimeConfigurationError(
            "Microsoft Agent Framework is required but could not be imported."
        ) from exc

    from agent_framework.observability import disable_instrumentation
    disable_instrumentation()

    client = OpenAIChatClient(model=config.model, api_key=config.api_key, base_url=config.base_url)
    agent = Agent(
        client=client,
        name=agent_title,
        description=agent_description,
        instructions=(
            "You are a .NET migration specialist agent. Review the scanner output, "
            "identify migration implications, and return concise JSON with keys: "
            "summary, recommendations, risks, manualActions."
        ),
    )
    prompt = (
        f"Agent: {agent_title}\n"
        f"Description: {agent_description}\n"
        "Scanner output JSON:\n"
        f"{json.dumps(output, default=str)[:12000]}\n\n"
        "Return only valid JSON."
    )

    try:
        loop = _get_event_loop()
        ctx = contextvars.copy_context()
        response = loop.run_until_complete(ctx.run(agent.run, prompt))
        text = response.text or str(response.value)
        return {
            "provider": config.provider,
            "model": config.model,
            "framework": "microsoft-agent-framework",
            "response": _parse_json_or_text(text),
        }
    except Exception as exc:
        raise AgenticRuntimeExecutionError(f"Microsoft Agent Framework LLM execution failed: {exc}") from exc


def _parse_json_or_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    except json.JSONDecodeError:
        return {"summary": cleaned, "recommendations": [], "risks": [], "manualActions": []}
