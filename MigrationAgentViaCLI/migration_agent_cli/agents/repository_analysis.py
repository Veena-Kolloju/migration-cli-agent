from __future__ import annotations

from typing import Any

from migration_agent_cli.core.agent_base import StructuredMigrationAgent, safe_source_path
from migration_agent_cli.core.models import AgentExecutionContext


class RepositoryAnalysisAgent(StructuredMigrationAgent):
    agent_id = "repository-analysis"
    title = "Repository Analysis Agent"
    description = "Discovers solutions, projects, package files, config files, and repository structure."
    capabilities = ["Repository scanning", "Project discovery", "Framework detection"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        source = safe_source_path(context, logs)
        if not source:
            return {"solutions": [], "projects": [], "configFiles": [], "packageFiles": [], "repoSummary": {"projectCount": 0, "hasSolutionFile": False}}

        solutions = [str(p.relative_to(source)) for p in source.rglob("*.sln")]
        project_files = list(source.rglob("*.csproj")) + list(source.rglob("*.vbproj")) + list(source.rglob("*.fsproj"))
        projects = []
        for project in project_files:
            text = project.read_text(encoding="utf-8", errors="ignore")
            target = "unknown"
            for tag in ("TargetFramework", "TargetFrameworkVersion"):
                start = text.find(f"<{tag}>")
                end = text.find(f"</{tag}>")
                if start >= 0 and end > start:
                    target = text[start + len(tag) + 2 : end]
                    break
            projects.append({"name": project.stem, "path": str(project.relative_to(source)), "targetFramework": target})
        config_files = [str(p.relative_to(source)) for p in source.rglob("*.config")]
        package_files = [str(p.relative_to(source)) for p in source.rglob("packages.config")]
        logs.append(f"Found {len(solutions)} solution files and {len(projects)} projects.")
        return {
            "solutions": solutions,
            "projects": projects,
            "configFiles": config_files,
            "packageFiles": package_files,
            "repoSummary": {"projectCount": len(projects), "hasSolutionFile": bool(solutions), "sourcePath": str(source)},
        }

