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


# ---------------------------------------------------------------------------
# Agents that do not benefit from LLM review — skip to save tokens
# ---------------------------------------------------------------------------
_SKIP_LLM_AGENTS: set[str] = {
    "build-validation",
    "build-fix",
    "test-validation",
    "configuration-migration",
    "repository-analysis",
    "project-conversion",
}

# ---------------------------------------------------------------------------
# Agent-specific LLM instructions
# ---------------------------------------------------------------------------
_AGENT_INSTRUCTIONS: dict[str, str] = {
    "assessment": (
        "You are a .NET migration readiness specialist. Review the assessment output and return JSON with keys: "
        "summary, recommendations, risks, manualActions. Focus on migration blockers and effort estimation."
    ),
    "dependency-analysis": (
        "You are a NuGet dependency expert. Review incompatible packages and return JSON with keys: "
        "summary, recommendations, risks, manualActions. Focus on package replacements and version conflicts."
    ),
    "code-analysis": (
        "You are a .NET code migration expert. Review the code findings and return JSON with keys: "
        "summary, recommendations, risks, manualActions. Focus on System.Web usages and API replacements needed."
    ),
    "ef-migration": (
        "You are an Entity Framework migration expert. Review the EF migration output and return JSON with keys: "
        "summary, recommendations, risks, manualActions. Focus on DbContext changes and EF Core compatibility."
    ),
    "api-transformation": (
        "You are an ASP.NET Core REST API expert. Review the API transformation output and return JSON with keys: "
        "summary, recommendations, risks, manualActions. Focus on controller patterns and REST compliance."
    ),
    "auth-transformation": (
        "You are an ASP.NET Core Identity and JWT expert. Review the auth transformation output and return JSON with keys: "
        "summary, recommendations, risks, manualActions. Focus on security gaps and Identity configuration."
    ),
    "code-transformation": (
        "You are a .NET code transformation expert. Review the transformation output and return JSON with keys: "
        "summary, recommendations, risks, manualActions. Focus on TODO items and manual fixes needed."
    ),
    "frontend-migration": (
        "You are a React and AngularJS migration expert. Review the frontend migration output and return JSON with keys: "
        "summary, recommendations, risks, manualActions. Focus on component completeness and routing gaps."
    ),
    "report-generation": (
        "You are a migration project manager. Review the full migration report output and return JSON with keys: "
        "summary, recommendations, risks, manualActions. Give an executive summary of the migration outcome."
    ),
}

_DEFAULT_INSTRUCTIONS = (
    "You are a .NET migration specialist agent. Review the scanner output, "
    "identify migration implications, and return concise JSON with keys: "
    "summary, recommendations, risks, manualActions."
)


def _build_compact_payload(agent_title: str, output: dict[str, Any]) -> str:
    """Build a compact summary payload — send counts not full lists to save tokens."""
    compact: dict[str, Any] = {}
    for key, value in output.items():
        if key == "agenticReview":
            continue
        if isinstance(value, list):
            compact[f"{key}Count"] = len(value)
            # Include first 3 items as sample
            compact[f"{key}Sample"] = value[:3]
        elif isinstance(value, dict) and len(json.dumps(value, default=str)) > 500:
            compact[key] = {k: v for k, v in list(value.items())[:5]}
        else:
            compact[key] = value
    return json.dumps(compact, default=str)[:6000]


def run_agentic_review(agent_title: str, agent_description: str, output: dict[str, Any], target_framework: str = "") -> dict[str, Any]:
    # Skip LLM for mechanical agents — saves 6 calls per workflow run
    agent_id_guess = agent_title.lower().replace(" agent", "").replace(" ", "-")
    if agent_id_guess in _SKIP_LLM_AGENTS:
        return {
            "provider": "skipped",
            "model": "none",
            "framework": "microsoft-agent-framework",
            "response": {"summary": f"{agent_title} — LLM review skipped (mechanical agent).", "recommendations": [], "risks": [], "manualActions": []},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

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

    # Use agent-specific instructions if available
    instructions = _AGENT_INSTRUCTIONS.get(agent_id_guess, _DEFAULT_INSTRUCTIONS)

    client = OpenAIChatClient(model=config.model, api_key=config.api_key, base_url=config.base_url)
    agent = Agent(
        client=client,
        name=agent_title,
        description=agent_description,
        instructions=instructions,
    )

    # Build compact payload — counts not full lists
    compact_payload = _build_compact_payload(agent_title, output)
    framework_line = f"Migration target framework: {target_framework}\n" if target_framework else ""
    prompt = (
        f"Agent: {agent_title}\n"
        f"Description: {agent_description}\n"
        f"{framework_line}"
        "Scanner output summary:\n"
        f"{compact_payload}\n\n"
        "Return only valid JSON with keys: summary, recommendations, risks, manualActions."
    )

    try:
        loop = _get_event_loop()
        ctx = contextvars.copy_context()
        response = loop.run_until_complete(ctx.run(agent.run, prompt))
        text = response.text or str(response.value)
        usage = getattr(response, "usage", {})
        return {
            "provider": config.provider,
            "model": config.model,
            "framework": "microsoft-agent-framework",
            "response": _parse_json_or_text(text),
            "usage": usage if isinstance(usage, dict) else {},
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
