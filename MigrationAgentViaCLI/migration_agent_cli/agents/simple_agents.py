from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from migration_agent_cli.core.artifacts import run_dir
from migration_agent_cli.core.agent_base import StructuredMigrationAgent, safe_source_path
from migration_agent_cli.core.models import AgentExecutionContext


class ProjectConversionAgent(StructuredMigrationAgent):
    agent_id = "project-conversion"
    title = "Project Conversion Agent"
    description = "Converts project files toward SDK-style project format and target framework."
    capabilities = ["Project conversion", "Target framework update", "Conversion diff"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        source = safe_source_path(context, logs)
        projects = [str(p.relative_to(source)) for p in source.rglob("*.csproj")] if source else context.input_data.get("projectFiles", [])
        dry_run = context.dry_run or context.input_data.get("dryRun", False)
        target_framework = context.input_data.get("targetFramework", "net8.0")
        logs.append(f"Prepared conversion plan for {len(projects)} projects. dryRun={dry_run}.")

        if dry_run or not source:
            return {
                "convertedProjects": [{"path": p, "status": "planned"} for p in projects],
                "changedFiles": [],
                "warnings": ["Dry run only. Re-run with dryRun=false to create a migrated copy under artifacts."],
                "diffPath": None,
            }

        migrated_root = run_dir(context) / "migrated-source"
        if migrated_root.exists():
            shutil.rmtree(migrated_root)
        ignore = shutil.ignore_patterns("bin", "obj", ".git", ".vs", "artifacts")
        shutil.copytree(source, migrated_root, ignore=ignore)

        changed_files: list[str] = []
        converted_projects: list[dict[str, str]] = []
        upgrade_assistant_available = _check_upgrade_assistant(logs)

        for relative_project in projects:
            project_path = migrated_root / relative_project
            original = project_path.read_text(encoding="utf-8", errors="ignore")

            # Try upgrade-assistant first
            if upgrade_assistant_available:
                ua_status = _run_upgrade_assistant(project_path, target_framework, logs)
                if ua_status == "upgraded":
                    changed_files.append(str(project_path))
                    converted_projects.append({"path": relative_project, "status": "upgraded-via-upgrade-assistant"})
                    continue

            # Fallback: manual conversion preserving all PackageReferences
            converted = _convert_csproj_to_sdk_style(original, target_framework)
            if converted != original:
                project_path.write_text(converted, encoding="utf-8")
                changed_files.append(str(project_path))
                converted_projects.append({"path": relative_project, "status": "converted"})
            else:
                converted_projects.append({"path": relative_project, "status": "reviewRequired"})

        return {
            "convertedProjects": converted_projects,
            "changedFiles": changed_files,
            "warnings": ["A migrated copy was created. Review it before replacing the original application."],
            "migratedSourcePath": str(migrated_root),
            "diffPath": None,
        }


class CodeTransformationAgent(StructuredMigrationAgent):
    agent_id = "code-transformation"
    title = "Code Transformation Agent"
    description = "Applies automated source transformations based on migration rules."
    capabilities = ["API replacement", "Namespace updates", "Startup.cs migration", "Transformation diff"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        from migration_agent_cli.agents.code_transformer import transform_cs_file, generate_program_cs, STARTUP_PATTERN

        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        if not migrated_root:
            logs.append("No migrated source path found — skipping code transformation.")
            return {"appliedFixes": [], "changedFiles": [], "skippedFixes": [], "diffPath": None}

        root = Path(migrated_root)
        all_fixes: list[dict[str, Any]] = []
        changed_files: list[str] = []
        startup_migrated: list[str] = []

        for cs_file in root.rglob("*.cs"):
            if any(part in {"bin", "obj"} for part in cs_file.parts):
                continue
            original = cs_file.read_text(encoding="utf-8", errors="ignore")
            transformed, fixes = transform_cs_file(original, cs_file, logs)
            if fixes:
                cs_file.write_text(transformed, encoding="utf-8")
                changed_files.append(str(cs_file))
                all_fixes.extend(fixes)

            # Generate Program.cs from Startup.cs
            if cs_file.name == "Startup.cs" and STARTUP_PATTERN.search(original):
                program_cs = generate_program_cs(cs_file, logs)
                if program_cs:
                    program_path = cs_file.parent / "Program.cs"
                    # Only write if Program.cs doesn't already exist or is the old WebHost style
                    existing = program_path.read_text(encoding="utf-8", errors="ignore") if program_path.exists() else ""
                    if "CreateHostBuilder" in existing or "CreateWebHostBuilder" in existing or not program_path.exists():
                        program_path.write_text(program_cs, encoding="utf-8")
                        startup_migrated.append(str(program_path))
                        logs.append(f"Generated modern Program.cs at {program_path.name}.")

        logs.append(f"Transformed {len(changed_files)} files with {len(all_fixes)} fixes. Startup migrations: {len(startup_migrated)}.")
        return {
            "appliedFixes": all_fixes,
            "changedFiles": changed_files,
            "startupMigrated": startup_migrated,
            "skippedFixes": [],
            "diffPath": None,
        }


class ConfigurationMigrationAgent(StructuredMigrationAgent):
    agent_id = "configuration-migration"
    title = "Configuration Migration Agent"
    description = "Migrates app.config/web.config settings to modern configuration files."
    capabilities = ["Config migration", "appsettings generation", "Secret reference detection"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        source = safe_source_path(context, logs)
        configs = [str(p.relative_to(source)) for p in source.rglob("*.config")] if source else context.input_data.get("configFiles", [])
        logs.append(f"Found {len(configs)} configuration files.")
        generated_files: list[str] = []
        migrated_settings: list[dict[str, str]] = []
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        if migrated_root:
            appsettings_path = Path(migrated_root) / "appsettings.migration.json"
            appsettings = {"MigrationNotes": {"sourceConfigFiles": configs, "reviewRequired": True}}
            appsettings_path.write_text(json.dumps(appsettings, indent=2), encoding="utf-8")
            generated_files.append(str(appsettings_path))
            migrated_settings.append({"file": str(appsettings_path), "status": "generatedReviewTemplate"})
        return {"generatedFiles": generated_files, "migratedSettings": migrated_settings, "unmigratedSettings": [{"file": c, "reason": "Manual mapping required."} for c in configs], "warnings": []}


class BuildValidationAgent(StructuredMigrationAgent):
    agent_id = "build-validation"
    title = "Build Validation Agent"
    description = "Runs restore/build commands and structures compiler errors."
    capabilities = ["Restore", "Build execution", "Compiler error parsing"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        if not migrated_root:
            logs.append("No migrated source path — skipping build validation.")
            return {"buildStatus": "skipped", "exitCode": None, "errors": [], "warnings": [], "summary": {"errorCount": 0, "warningCount": 0, "failedProjects": 0}}

        build_path = Path(migrated_root)
        logs.append(f"Running dotnet restore on {build_path.name}.")
        restore_result = _run_dotnet("dotnet restore", build_path, logs)

        logs.append(f"Running dotnet build on {build_path.name}.")
        build_result = _run_dotnet("dotnet build --no-restore", build_path, logs)

        errors = _parse_build_output(build_result["output"])
        warnings = _parse_build_output(build_result["output"], severity="warning")

        build_status = "succeeded" if build_result["exitCode"] == 0 else "failed"
        logs.append(f"Build {build_status}. Errors: {len(errors)}, Warnings: {len(warnings)}.")

        return {
            "buildStatus": build_status,
            "exitCode": build_result["exitCode"],
            "errors": errors,
            "warnings": warnings,
            "restoreExitCode": restore_result["exitCode"],
            "summary": {
                "errorCount": len(errors),
                "warningCount": len(warnings),
                "failedProjects": sum(1 for e in errors if e.get("project")),
            },
        }


class BuildFixAgent(StructuredMigrationAgent):
    agent_id = "build-fix"
    title = "Build Fix Agent"
    description = "Attempts automated fixes for known post-migration build failures."
    capabilities = ["Build error fixes", "Package fixes", "Namespace fixes"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        errors = context.shared_state.get("build-validation", {}).get("errors", [])
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        logs.append(f"Received {len(errors)} build errors for fix planning.")

        if not errors or not migrated_root:
            return {"fixStatus": "notNeeded", "appliedFixes": [], "changedFiles": [], "unresolvedErrors": errors, "diffPath": None}

        applied_fixes: list[dict[str, Any]] = []
        changed_files: list[str] = []
        unresolved: list[dict[str, Any]] = []

        for error in errors:
            fix = _attempt_build_fix(error, Path(migrated_root), logs)
            if fix:
                applied_fixes.append(fix)
                if fix["file"] not in changed_files:
                    changed_files.append(fix["file"])
            else:
                unresolved.append(error)

        logs.append(f"Applied {len(applied_fixes)} fixes. Unresolved: {len(unresolved)}.")
        return {
            "fixStatus": "completed" if not unresolved else "partial",
            "appliedFixes": applied_fixes,
            "changedFiles": changed_files,
            "unresolvedErrors": unresolved,
            "diffPath": None,
        }


class TestValidationAgent(StructuredMigrationAgent):
    agent_id = "test-validation"
    title = "Test Validation Agent"
    description = "Runs tests and categorizes migration-related failures."
    capabilities = ["Test execution", "Failure categorization", "Regression summary"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        if not migrated_root:
            logs.append("No migrated source path — skipping test validation.")
            return {"testStatus": "skipped", "passed": 0, "failed": 0, "skipped": 0, "failedTests": [], "summary": "No migrated source available."}

        test_path = Path(migrated_root)
        logs.append(f"Running dotnet test on {test_path.name}.")
        result = _run_dotnet("dotnet test --no-build --logger trx", test_path, logs)

        passed, failed, skipped_count, failed_tests = _parse_test_output(result["output"], logs)
        test_status = "passed" if result["exitCode"] == 0 else "failed"
        logs.append(f"Tests {test_status}. Passed: {passed}, Failed: {failed}, Skipped: {skipped_count}.")

        return {
            "testStatus": test_status,
            "exitCode": result["exitCode"],
            "passed": passed,
            "failed": failed,
            "skipped": skipped_count,
            "failedTests": failed_tests,
            "summary": f"Passed: {passed}, Failed: {failed}, Skipped: {skipped_count}",
        }


class ReportGenerationAgent(StructuredMigrationAgent):
    agent_id = "report-generation"
    title = "Report Generation Agent"
    description = "Generates migration reports from individual or orchestrated agent outputs."
    capabilities = ["HTML report", "JSON summary", "Executive summary"]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        statuses = {key: "available" for key in context.shared_state.keys()}
        logs.append(f"Prepared report from {len(statuses)} agent outputs.")
        report_path = run_dir(context) / "migration-report.md"

        # Collect data from all agents
        code_findings = context.shared_state.get("code-analysis", {}).get("findings", [])
        dependencies = context.shared_state.get("dependency-analysis", {}).get("dependencies", [])
        incompatible = context.shared_state.get("dependency-analysis", {}).get("incompatiblePackages", [])
        upgraded_files = context.shared_state.get("dependency-analysis", {}).get("upgradedFiles", [])
        migrated_source = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        converted_projects = context.shared_state.get("project-conversion", {}).get("convertedProjects", [])
        applied_fixes = context.shared_state.get("code-transformation", {}).get("appliedFixes", [])
        startup_migrated = context.shared_state.get("code-transformation", {}).get("startupMigrated", [])
        frontend_root = context.shared_state.get("frontend-migration", {}).get("frontendRoot")
        razor_converted = context.shared_state.get("frontend-migration", {}).get("razorPagesConverted", 0)
        blazor_converted = context.shared_state.get("frontend-migration", {}).get("blazorComponentsConverted", 0)
        build_status = context.shared_state.get("build-validation", {}).get("buildStatus", "notRun")
        build_errors = context.shared_state.get("build-validation", {}).get("errors", [])
        build_warnings = context.shared_state.get("build-validation", {}).get("warnings", [])
        build_fixes = context.shared_state.get("build-fix", {}).get("appliedFixes", [])
        test_status = context.shared_state.get("test-validation", {}).get("testStatus", "notRun")
        test_passed = context.shared_state.get("test-validation", {}).get("passed", 0)
        test_failed = context.shared_state.get("test-validation", {}).get("failed", 0)
        failed_tests = context.shared_state.get("test-validation", {}).get("failedTests", [])

        lines = [
            "# .NET Migration Report",
            "",
            "## Summary",
            "",
            f"- Agents completed: {len(statuses)}",
            f"- Migrated source: {migrated_source or 'not generated'}",
            "",
            "## Project Conversion",
            "",
            f"- Projects converted: {len(converted_projects)}",
        ]
        for p in converted_projects:
            lines.append(f"  - {p.get('path')} → {p.get('status')}")

        lines += [
            "",
            "## Dependency Analysis",
            "",
            f"- Total packages: {len(dependencies)}",
            f"- Upgraded in csproj files: {len(upgraded_files)}",
            f"- Incompatible packages: {len(incompatible)}",
        ]
        for pkg in incompatible:
            lines.append(f"  - {pkg.get('name')}: {pkg.get('upgradeNotes')}")

        lines += [
            "",
            "## Code Transformation",
            "",
            f"- Transformation rules applied: {len(applied_fixes)}",
            f"- Startup.cs → Program.cs migrations: {len(startup_migrated)}",
            f"- Code findings: {len(code_findings)}",
        ]

        lines += [
            "",
            "## Frontend Migration",
            "",
            f"- Razor Pages converted to React: {razor_converted}",
            f"- Blazor components converted to React: {blazor_converted}",
            f"- React frontend root: {frontend_root or 'not generated'}",
        ]

        lines += [
            "",
            "## Build Validation",
            "",
            f"- Build status: {build_status}",
            f"- Errors: {len(build_errors)}",
            f"- Warnings: {len(build_warnings)}",
            f"- Auto-fixes applied: {len(build_fixes)}",
        ]
        for err in build_errors[:10]:
            lines.append(f"  - [{err.get('code')}] {err.get('file')}:{err.get('line')} — {err.get('message')}")

        lines += [
            "",
            "## Test Validation",
            "",
            f"- Test status: {test_status}",
            f"- Passed: {test_passed}, Failed: {test_failed}",
        ]
        for t in failed_tests[:10]:
            lines.append(f"  - {t.get('name')} ({t.get('category')})")

        lines += [
            "",
            "## Next Actions",
            "",
            "1. Review converted project files and verify all PackageReferences are correct.",
            "2. Run `npm install && npm run dev` in the frontend folder to start the React app.",
            "3. Resolve any remaining build errors listed above.",
            "4. Update database connection strings in appsettings.json.",
            "5. Review TODO comments inserted by the code transformation agent.",
            "6. Run full test suite and fix migration-related failures.",
        ]

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return {
            "reports": [str(report_path)],
            "summary": {
                "overallStatus": build_status,
                "agentOutputs": statuses,
                "buildErrors": len(build_errors),
                "testsPassed": test_passed,
                "testsFailed": test_failed,
                "frontendGenerated": bool(frontend_root),
                "manualActionsRequired": len(code_findings) + len(incompatible) + len(build_errors),
            },
        }


def _attempt_build_fix(error: dict[str, Any], migrated_root: Path, logs: list[str]) -> dict[str, Any] | None:
    """Attempt automated fix for known build error codes."""
    code = error.get("code", "")
    file_path = error.get("file", "")
    message = error.get("message", "")

    if not file_path:
        return None

    target = Path(file_path)
    if not target.exists():
        # Try resolving relative to migrated root
        target = migrated_root / file_path
    if not target.exists() or target.suffix != ".cs":
        return None

    try:
        source = target.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    updated = source

    # CS0234 / CS0246: missing type or namespace — add common using
    if code in ("CS0234", "CS0246"):
        ns_match = re.search(r"type or namespace name '([\w.]+)'", message)
        if ns_match:
            missing = ns_match.group(1)
            using_map = {
                "IConfiguration": "using Microsoft.Extensions.Configuration;",
                "IMemoryCache": "using Microsoft.Extensions.Caching.Memory;",
                "IHttpContextAccessor": "using Microsoft.AspNetCore.Http;",
                "JsonSerializer": "using System.Text.Json;",
                "ILogger": "using Microsoft.Extensions.Logging;",
            }
            if missing in using_map and using_map[missing] not in updated:
                updated = using_map[missing] + "\n" + updated

    # CS0103: name does not exist — common _configuration field injection hint
    if code == "CS0103" and "_configuration" in message:
        if "private readonly IConfiguration _configuration;" not in updated:
            updated = re.sub(
                r'(public\s+class\s+\w+[^{]*\{)',
                r'\1\n    private readonly IConfiguration _configuration;',
                updated, count=1
            )

    if updated != source:
        target.write_text(updated, encoding="utf-8")
        logs.append(f"Auto-fixed {code} in {target.name}.")
        return {"file": str(target), "code": code, "fix": f"Applied automated fix for {code}"}

    return None


def _run_dotnet(command: str, cwd: Path, logs: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command.split(),
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(cwd),
        )
        logs.append(f"{command} exit={result.returncode}")
        return {"exitCode": result.returncode, "output": result.stdout + result.stderr}
    except Exception as exc:
        logs.append(f"{command} failed: {exc}")
        return {"exitCode": -1, "output": str(exc)}


def _parse_build_output(output: str, severity: str = "error") -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    # MSBuild error/warning format: file(line,col): error|warning CODE: message
    pattern = re.compile(
        rf'([^\(]+)\((\d+),(\d+)\):\s+{severity}\s+(\w+):\s+(.+)',
        re.IGNORECASE,
    )
    for match in pattern.finditer(output):
        findings.append({
            "file": match.group(1).strip(),
            "line": int(match.group(2)),
            "column": int(match.group(3)),
            "code": match.group(4),
            "message": match.group(5).strip(),
            "severity": severity,
        })
    return findings


def _parse_test_output(output: str, logs: list[str]) -> tuple[int, int, int, list[dict[str, Any]]]:
    passed = 0
    failed = 0
    skipped = 0
    failed_tests: list[dict[str, Any]] = []

    # dotnet test summary line: "Passed: X, Failed: Y, Skipped: Z"
    summary_match = re.search(r'Passed:\s*(\d+).*?Failed:\s*(\d+).*?Skipped:\s*(\d+)', output, re.IGNORECASE)
    if summary_match:
        passed = int(summary_match.group(1))
        failed = int(summary_match.group(2))
        skipped = int(summary_match.group(3))

    # Capture individual failed test names
    for match in re.finditer(r'Failed\s+(\S+)', output):
        test_name = match.group(1)
        if test_name not in ("!", "0"):
            # Categorize as migration-related or pre-existing
            category = "migration-related" if any(
                kw in test_name.lower() for kw in ["startup", "config", "auth", "identity", "web", "http"]
            ) else "unknown"
            failed_tests.append({"name": test_name, "category": category})

    return passed, failed, skipped, failed_tests


def _check_upgrade_assistant(logs: list[str]) -> bool:
    try:
        result = subprocess.run(["upgrade-assistant", "--version"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logs.append(f"upgrade-assistant available: {result.stdout.strip()}")
            return True
    except Exception as exc:
        logs.append(f"upgrade-assistant not available: {exc}")
    return False


def _run_upgrade_assistant(project_path: Path, target_framework: str, logs: list[str]) -> str:
    try:
        result = subprocess.run(
            ["upgrade-assistant", "upgrade", str(project_path), "--non-interactive", "--target-tfm-support", "LTS"],
            capture_output=True, text=True, timeout=300, cwd=str(project_path.parent),
        )
        logs.append(f"upgrade-assistant [{project_path.name}] exit={result.returncode}")
        if result.returncode == 0:
            return "upgraded"
        logs.append(f"upgrade-assistant stderr: {result.stderr[:500]}")
    except Exception as exc:
        logs.append(f"upgrade-assistant failed for {project_path.name}: {exc}")
    return "failed"


def _detect_sdk(project_xml: str) -> str:
    if "Microsoft.NET.Sdk.Web" in project_xml or "<Project Sdk=\"Microsoft.NET.Sdk.Web\"" in project_xml:
        return "Microsoft.NET.Sdk.Web"
    if "Microsoft.NET.Sdk.BlazorWebAssembly" in project_xml:
        return "Microsoft.NET.Sdk.BlazorWebAssembly"
    if "Microsoft.NET.Sdk.Razor" in project_xml:
        return "Microsoft.NET.Sdk.Razor"
    # Legacy web project indicators
    if any(x in project_xml for x in ["System.Web", "WebApplication", "<UseIISExpress>", "aspnet"]):
        return "Microsoft.NET.Sdk.Web"
    return "Microsoft.NET.Sdk"


def _convert_csproj_to_sdk_style(project_xml: str, target_framework: str) -> str:
    sdk = _detect_sdk(project_xml)

    # Preserve all existing PackageReference entries
    existing_refs: dict[str, str] = {}
    for match in re.finditer(
        r'<PackageReference\s+Include="([^"]+)"[^/]*/?>',
        project_xml, re.IGNORECASE | re.DOTALL
    ):
        full_tag = match.group(0)
        name = match.group(1)
        version_match = re.search(r'Version="([^"]+)"', full_tag, re.IGNORECASE)
        version = version_match.group(1) if version_match else "*"
        existing_refs[name] = version

    # Also pick up old-style <Reference Include="SomeLib, Version=...">
    for match in re.finditer(r'<Reference Include="([^"]+)"', project_xml):
        raw = match.group(1).split(",")[0].strip()
        if raw and not raw.startswith("System") and not raw.startswith("Microsoft") and raw not in existing_refs:
            existing_refs[raw] = "*"

    # Detect if already SDK-style — just update TargetFramework
    if re.search(r'<Project\s+Sdk=', project_xml):
        updated = re.sub(
            r'<TargetFramework>[^<]*</TargetFramework>',
            f'<TargetFramework>{target_framework}</TargetFramework>',
            project_xml
        )
        # Add ImplicitUsings and Nullable if missing
        if "<ImplicitUsings>" not in updated:
            updated = updated.replace(
                f'<TargetFramework>{target_framework}</TargetFramework>',
                f'<TargetFramework>{target_framework}</TargetFramework>\n    <ImplicitUsings>enable</ImplicitUsings>\n    <Nullable>enable</Nullable>'
            )
        return updated

    # Legacy project — full conversion
    item_group = ""
    if existing_refs:
        refs = "\n".join(
            f'    <PackageReference Include="{name}" Version="{ver}" />'
            for name, ver in sorted(existing_refs.items())
        )
        item_group = f"\n\n  <ItemGroup>\n{refs}\n  </ItemGroup>"

    return (
        f'<Project Sdk="{sdk}">\n\n'
        "  <PropertyGroup>\n"
        f"    <TargetFramework>{target_framework}</TargetFramework>\n"
        "    <ImplicitUsings>enable</ImplicitUsings>\n"
        "    <Nullable>enable</Nullable>\n"
        "  </PropertyGroup>"
        f"{item_group}\n\n"
        "</Project>\n"
    )
