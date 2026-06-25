from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


AgentStatus = Literal["completed", "failed", "partial", "skipped"]


class AgentExecutionContext(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}")
    agent_id: str
    input_data: dict[str, Any] = Field(default_factory=dict)
    shared_state: dict[str, Any] = Field(default_factory=dict)
    output_dir: str = "artifacts"
    dry_run: bool = False
    verbose: bool = False

    @property
    def source_path(self) -> Path | None:
        value = self.input_data.get("sourcePath")
        return Path(value) if value else None


class AgentExecutionResult(BaseModel):
    run_id: str
    agent_id: str
    status: AgentStatus
    started_at: datetime
    completed_at: datetime
    logs: list[str] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None

    @classmethod
    def completed(
        cls,
        context: AgentExecutionContext,
        started_at: datetime,
        logs: list[str],
        output: dict[str, Any],
        artifacts: list[str] | None = None,
        status: AgentStatus = "completed",
    ) -> "AgentExecutionResult":
        return cls(
            run_id=context.run_id,
            agent_id=context.agent_id,
            status=status,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            logs=logs,
            output=output,
            artifacts=artifacts or [],
        )


class AgentMetadata(BaseModel):
    agent_id: str = Field(alias="agentId")
    title: str
    description: str
    icon: str
    capabilities: list[str]
    techstack: list[str]
    ui_spec: dict[str, Any] = Field(alias="uiSpec")
    api_spec: dict[str, Any] = Field(alias="apiSpec")
    category: str
    sub_category: str = Field(alias="subCategory")


class WorkflowInput(BaseModel):
    workflow_id: str = Field(default="dotnet-migration", alias="workflowId")
    source_path: str | None = Field(default=None, alias="sourcePath")
    repository_url: str | None = Field(default=None, alias="repositoryUrl")
    branch: str = "main"
    target_framework: str = Field(default="net8.0", alias="targetFramework")
    agents: list[str]
    output_dir: str = Field(default="artifacts", alias="outputDir")
    target_frontend: str = Field(default="react", alias="targetFrontend")
    dry_run: bool = Field(default=False, alias="dryRun")
    continue_on_error: bool = Field(default=True, alias="continueOnError")

    @property
    def target_architecture(self) -> str:
        return "decoupled-spa" if self.target_frontend == "react" else "default"

    @property
    def output_structure(self) -> str:
        return "monorepo" if self.target_frontend == "react" else "default"

