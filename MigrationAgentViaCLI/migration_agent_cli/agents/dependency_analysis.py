from __future__ import annotations

import re
import urllib.request
import urllib.error
import json
from pathlib import Path
from typing import Any

from migration_agent_cli.core.agent_base import StructuredMigrationAgent, safe_source_path
from migration_agent_cli.core.models import AgentExecutionContext


class DependencyAnalysisAgent(StructuredMigrationAgent):
    agent_id = "dependency-analysis"
    title = "Dependency Analysis Agent"
    description = "Analyzes NuGet and framework dependencies for target compatibility and upgrades."
    capabilities = ["NuGet scanning", "Compatibility checks", "Upgrade recommendations", "Auto version upgrade"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        source = safe_source_path(context, logs)
        target_framework = context.input_data.get("targetFramework", "net8.0")
        packages: list[dict[str, Any]] = []

        if source:
            for package_file in source.rglob("packages.config"):
                text = package_file.read_text(encoding="utf-8", errors="ignore")
                for match in re.finditer(r'id="([^"]+)".*?version="([^"]+)"', text):
                    packages.append({"name": match.group(1), "currentVersion": match.group(2), "source": str(package_file)})
            for project_file in source.rglob("*.csproj"):
                text = project_file.read_text(encoding="utf-8", errors="ignore")
                for match in re.finditer(r'<PackageReference Include="([^"]+)".*?Version="([^"]+)"', text, flags=re.S):
                    packages.append({"name": match.group(1), "currentVersion": match.group(2), "source": str(project_file)})

        logs.append(f"Discovered {len(packages)} package references. Querying NuGet API for compatibility.")

        compatible: list[dict[str, Any]] = []
        incompatible: list[dict[str, Any]] = []
        upgrade_recommended: list[dict[str, Any]] = []

        seen: set[str] = set()
        for pkg in packages:
            name = pkg["name"]
            if name in seen:
                continue
            seen.add(name)
            result = _query_nuget(name, target_framework, logs)
            pkg.update(result)
            if result["status"] == "compatible":
                compatible.append(pkg)
            elif result["status"] == "incompatible":
                incompatible.append(pkg)
            else:
                upgrade_recommended.append(pkg)

        # Apply version upgrades to migrated source csproj files
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        upgraded_files: list[str] = []
        if migrated_root:
            upgraded_files = _apply_version_upgrades(Path(migrated_root), packages, logs)

        logs.append(f"Compatible: {len(compatible)}, Upgrade recommended: {len(upgrade_recommended)}, Incompatible: {len(incompatible)}.")
        return {
            "dependencies": packages,
            "incompatiblePackages": incompatible,
            "upgradeRecommended": upgrade_recommended,
            "upgradedFiles": upgraded_files,
            "summary": {
                "totalPackages": len(seen),
                "compatible": len(compatible),
                "upgradeRecommended": len(upgrade_recommended),
                "incompatible": len(incompatible),
            },
        }


def _query_nuget(package_name: str, target_framework: str, logs: list[str]) -> dict[str, Any]:
    """Query NuGet API for latest version compatible with target framework."""
    try:
        url = f"https://api.nuget.org/v3-flatcontainer/{package_name.lower()}/index.json"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        versions: list[str] = data.get("versions", [])
        if not versions:
            return {"status": "reviewRequired", "latestVersion": None, "upgradeNotes": "No versions found on NuGet."}

        # Filter stable versions only
        stable = [v for v in versions if not any(x in v.lower() for x in ["alpha", "beta", "preview", "rc"])]
        latest = stable[-1] if stable else versions[-1]

        return {
            "status": "upgradeRecommended",
            "latestVersion": latest,
            "upgradeNotes": f"Latest stable version: {latest}. Verify {target_framework} compatibility.",
        }
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "incompatible", "latestVersion": None, "upgradeNotes": "Package not found on NuGet — may be deprecated or renamed."}
        logs.append(f"NuGet API error for {package_name}: {exc}")
        return {"status": "reviewRequired", "latestVersion": None, "upgradeNotes": f"NuGet API error: {exc}"}
    except Exception as exc:
        logs.append(f"NuGet query failed for {package_name}: {exc}")
        return {"status": "reviewRequired", "latestVersion": None, "upgradeNotes": "NuGet query failed — check network."}


def _apply_version_upgrades(migrated_root: Path, packages: list[dict[str, Any]], logs: list[str]) -> list[str]:
    """Update PackageReference versions in migrated csproj files to latest stable."""
    upgraded_files: list[str] = []
    upgrade_map = {
        pkg["name"]: pkg["latestVersion"]
        for pkg in packages
        if pkg.get("latestVersion") and pkg.get("status") == "upgradeRecommended"
    }
    if not upgrade_map:
        return upgraded_files

    for csproj in migrated_root.rglob("*.csproj"):
        original = csproj.read_text(encoding="utf-8", errors="ignore")
        updated = original

        for name, latest_version in upgrade_map.items():
            updated = re.sub(
                rf'(<PackageReference\s+Include="{re.escape(name)}"\s+Version=")[^"]*(")',
                rf'\g<1>{latest_version}\g<2>',
                updated,
                flags=re.IGNORECASE,
            )

        if updated != original:
            csproj.write_text(updated, encoding="utf-8")
            upgraded_files.append(str(csproj))
            logs.append(f"Updated package versions in {csproj.name}.")

    return upgraded_files
