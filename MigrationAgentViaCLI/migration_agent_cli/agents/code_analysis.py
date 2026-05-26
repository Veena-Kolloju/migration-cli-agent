from __future__ import annotations

from typing import Any

from migration_agent_cli.core.agent_base import StructuredMigrationAgent, safe_source_path
from migration_agent_cli.core.models import AgentExecutionContext


class CodeAnalysisAgent(StructuredMigrationAgent):
    agent_id = "code-analysis"
    title = "Code Analysis Agent"
    description = "Detects code-level migration issues such as deprecated or framework-specific APIs."
    capabilities = ["C# scanning", "API compatibility findings", "Modernization opportunities"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        source = safe_source_path(context, logs)
        findings = []
        files_scanned = 0
        if source:
            for file in source.rglob("*.cs"):
                if any(part in {"bin", "obj"} for part in file.parts):
                    continue
                files_scanned += 1
                lines = file.read_text(encoding="utf-8", errors="ignore").splitlines()
                for index, line in enumerate(lines, start=1):
                    if "System.Web" in line:
                        findings.append({"file": str(file.relative_to(source)), "line": index, "severity": "high", "ruleId": "DOTNET-MIGRATION-SYSTEM-WEB", "message": "System.Web usage detected.", "suggestedFix": "Map to ASP.NET Core middleware and abstractions."})
                    if "ConfigurationManager" in line:
                        findings.append({"file": str(file.relative_to(source)), "line": index, "severity": "medium", "ruleId": "DOTNET-MIGRATION-CONFIGURATION", "message": "ConfigurationManager usage detected.", "suggestedFix": "Move settings to IConfiguration/appsettings.json."})
        logs.append(f"Scanned {files_scanned} C# files and found {len(findings)} findings.")
        return {"findings": findings, "summary": {"filesScanned": files_scanned, "high": sum(f["severity"] == "high" for f in findings), "medium": sum(f["severity"] == "medium" for f in findings), "low": 0}}

