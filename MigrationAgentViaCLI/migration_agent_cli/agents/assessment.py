from __future__ import annotations

from typing import Any

from migration_agent_cli.core.agent_base import StructuredMigrationAgent
from migration_agent_cli.core.models import AgentExecutionContext


class AssessmentAgent(StructuredMigrationAgent):
    agent_id = "assessment"
    title = "Assessment Agent"
    description = "Assesses migration readiness, complexity, blockers, risks, and recommended path."
    capabilities = ["Readiness scoring", "Risk identification", "Migration planning"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        target = context.input_data.get("targetFramework", "net8.0")
        repo = context.shared_state.get("repository-analysis", {}).get("repoSummary", {})
        project_count = repo.get("projectCount", 0)
        blockers = []
        if not project_count:
            blockers.append("Repository structure has not been analyzed or no projects were detected.")
        score = 70 if project_count else 45
        logs.append(f"Calculated initial readiness score {score} for target {target}.")
        return {
            "readinessScore": score,
            "complexity": "medium" if project_count <= 5 else "high",
            "migrationRecommendation": f"Target {target}; run dependency and code analysis before conversion.",
            "blockers": blockers,
            "risks": [{"severity": "medium", "description": "Framework-specific APIs may require manual migration."}],
            "estimatedEffort": {"level": "medium", "projectCount": project_count},
        }

