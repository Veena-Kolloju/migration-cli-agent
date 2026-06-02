from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migration_agent_cli.core.models import AgentExecutionContext, AgentExecutionResult
from migration_agent_cli.llm.microsoft_agent_adapter import run_agentic_review


class MigrationAgent(ABC):
    agent_id: str
    title: str
    description: str

    @abstractmethod
    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        raise NotImplementedError

    def _start(self) -> datetime:
        return datetime.now(timezone.utc)


class StructuredMigrationAgent(MigrationAgent):
    capabilities: list[str] = []

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        started = self._start()
        logs = [f"Starting {self.title}.", f"Input keys: {', '.join(sorted(context.input_data.keys())) or 'none'}."]
        try:
            output = self.analyze(context, logs)
            logs.append("Running mandatory Microsoft Agent Framework LLM review.")
            output["agenticReview"] = run_agentic_review(self.title, self.description, output, context.input_data.get("targetFramework", ""))
            logs.append(f"Completed {self.title}.")
            return AgentExecutionResult.completed(context, started, logs, output)
        except Exception as exc:
            logs.append(f"Failed {self.title}: {exc}")
            return AgentExecutionResult(
                run_id=context.run_id,
                agent_id=context.agent_id,
                status="failed",
                started_at=started,
                completed_at=datetime.now(timezone.utc),
                logs=logs,
                output={},
                error=str(exc),
            )

    @abstractmethod
    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        raise NotImplementedError


def safe_source_path(context: AgentExecutionContext, logs: list[str]) -> Path | None:
    source_path = context.source_path
    if not source_path:
        logs.append("No sourcePath provided; returning contract-focused output.")
        return None
    if not source_path.exists():
        logs.append(f"sourcePath does not exist: {source_path}")
        return None
    return source_path
