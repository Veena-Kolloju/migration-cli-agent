from __future__ import annotations

import json
import os
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

        target_frontend = context.input_data.get("targetFrontend", "react")
        use_monorepo = target_frontend == "react"
        migration_base = run_dir(context) / "migration-code"
        source_code_path = migration_base / "source-code"
        target_code_path = migration_base / "target-code"
        migrated_root = target_code_path / "backend" if use_monorepo else target_code_path

        if migration_base.exists():
            shutil.rmtree(migration_base)

        # Step 1 — Copy full source to source-code/ (read-only reference, never touched again)
        ignore = shutil.ignore_patterns("bin", "obj", ".git", ".vs", "artifacts")
        _copytree_safe(source, source_code_path, ignore=ignore, logs=logs)
        logs.append(f"Copied source to source-code/ (reference snapshot).")

        # Step 2 — Selectively copy only useful files to target-code/backend/
        _selective_copy_to_target(source, migrated_root, logs)

        global_json = migrated_root / "global.json"
        if global_json.exists():
            global_json.unlink()
            logs.append("Removed global.json from target-code to avoid SDK version pinning.")

        changed_files: list[str] = []
        converted_projects: list[dict[str, str]] = []
        upgrade_assistant_available = _check_upgrade_assistant(logs)

        for relative_project in projects:
            project_path = migrated_root / relative_project
            if not project_path.exists():
                converted_projects.append({"path": relative_project, "status": "notFound"})
                continue
            original = project_path.read_text(encoding="utf-8", errors="ignore")
            has_assembly_info = any(project_path.parent.rglob("AssemblyInfo.cs"))

            # Try upgrade-assistant first
            if upgrade_assistant_available:
                ua_status = _run_upgrade_assistant(project_path, target_framework, logs)
                if ua_status == "upgraded":
                    changed_files.append(str(project_path))
                    converted_projects.append({"path": relative_project, "status": "upgraded-via-upgrade-assistant"})
                    continue

            # Fallback: manual conversion preserving all PackageReferences
            converted = _convert_csproj_to_sdk_style(original, target_framework, has_assembly_info, project_path)
            if converted != original:
                from migration_agent_cli.core.guardrails import check_xml
                if check_xml(converted, project_path.name, logs):
                    project_path.write_text(converted, encoding="utf-8")
                    changed_files.append(str(project_path))
                    if has_assembly_info:
                        logs.append(f"Added GenerateAssemblyInfo=false for {relative_project} to avoid CS0579 duplicate attribute errors.")
                    converted_projects.append({"path": relative_project, "status": "converted"})
                else:
                    converted_projects.append({"path": relative_project, "status": "guardrailFailed"})
            else:
                converted_projects.append({"path": relative_project, "status": "reviewRequired"})

        # Regenerate .sln file to avoid old-format restore failures
        _regenerate_sln(migrated_root, projects, logs)

        # Copy static files to wwwroot for each web project
        _setup_wwwroot(migrated_root, logs)

        # Scaffold backend folder structure (Services/, DTOs/, Data/)
        if use_monorepo:
            _scaffold_backend_folders(migrated_root, logs)

        return {
            "convertedProjects": converted_projects,
            "changedFiles": changed_files,
            "warnings": ["migration-code created. source-code/ is your reference. target-code/ is the new application."],
            "migratedSourcePath": str(migrated_root),
            "migratedBasePath": str(migration_base),
            "sourceCodePath": str(source_code_path),
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
        has_startup = any(root.rglob("Startup.cs"))

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
                    existing = program_path.read_text(encoding="utf-8", errors="ignore") if program_path.exists() else ""
                    if "CreateHostBuilder" in existing or "CreateWebHostBuilder" in existing or not program_path.exists():
                        program_path.write_text(program_cs, encoding="utf-8")
                        startup_migrated.append(str(program_path))
                        logs.append(f"Generated modern Program.cs at {program_path.name}.")

        # Handle Global.asax — generate Program.cs if no Startup.cs exists
        for global_asax in root.rglob("Global.asax.cs"):
            if any(part in {"bin", "obj"} for part in global_asax.parts):
                continue
            program_path = global_asax.parent / "Program.cs"
            if not program_path.exists() and not has_startup:
                program_cs = _generate_program_cs_from_global_asax(global_asax, logs)
                program_path.write_text(program_cs, encoding="utf-8")
                startup_migrated.append(str(program_path))
                logs.append(f"Generated Program.cs from Global.asax.cs at {global_asax.parent.name}.")
            # Neutralize Global.asax.cs — comment out System.Web references
            original = global_asax.read_text(encoding="utf-8", errors="ignore")
            neutralized = re.sub(r'^(using System\.Web[^;]*;)', r'// \1  // Removed: not available in .NET Core', original, flags=re.MULTILINE)
            # Remove base class entirely — HttpApplication doesn't exist in .NET Core
            neutralized = re.sub(r'\s*:\s*System\.Web\.HttpApplication', '', neutralized)
            neutralized = re.sub(r'\s*:\s*HttpApplication', '', neutralized)
            # Comment out AreaRegistration — not available in .NET Core
            neutralized = re.sub(
                r'(AreaRegistration\.RegisterAllAreas\(\);)',
                r'// \1  // Removed: AreaRegistration not available in .NET Core',
                neutralized
            )
            if neutralized != original:
                global_asax.write_text(neutralized, encoding="utf-8")
                changed_files.append(str(global_asax))
                logs.append(f"Neutralized System.Web references in {global_asax.name}.")

        # Disable dead App_Start files
        _disable_dead_app_start_files(root, changed_files, logs)

        # Clean up legacy files replaced by modern equivalents
        _cleanup_legacy_files(root, logs)

        # Delete Views/Web.config — MVC5-specific, not needed in .NET 8
        for views_webconfig in root.rglob("Web.config"):
            if "Views" in views_webconfig.parts and views_webconfig.exists():
                views_webconfig.unlink()
                logs.append(f"Deleted MVC5-specific Views/Web.config.")

        # Transform Razor views
        for cshtml_file in root.rglob("*.cshtml"):
            if any(part in {"bin", "obj"} for part in cshtml_file.parts):
                continue
            _transform_razor_view(cshtml_file, changed_files, logs)

        from migration_agent_cli.core.guardrails import check_program_cs_exists, check_target_framework, run_csharp_standards, check_connection_strings_exist, check_dbset_exists, check_async_controllers, check_input_validation, check_dependency_vulnerabilities
        check_program_cs_exists(migrated_root, logs)
        check_target_framework(migrated_root, context.input_data.get("targetFramework", "net8.0"), logs)
        check_connection_strings_exist(migrated_root, logs)
        check_dbset_exists(migrated_root, logs)
        check_async_controllers(migrated_root, logs)
        check_input_validation(migrated_root, logs)
        check_dependency_vulnerabilities(migrated_root, logs)
        run_csharp_standards(migrated_root, logs)
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

        # Find the primary solution file to avoid ambiguity
        sln_files = list(build_path.glob("*.sln"))
        sln_arg = str(sln_files[0].name) if len(sln_files) == 1 else "eShopOnWeb.sln" if (build_path / "eShopOnWeb.sln").exists() else (sln_files[0].name if sln_files else "")

        logs.append(f"Running dotnet restore on {build_path.name} using {sln_arg}.")
        restore_result = _run_dotnet(f"dotnet restore {sln_arg}".strip(), build_path, logs)

        # If solution-level restore failed, try restoring each project individually
        if restore_result["exitCode"] != 0:
            logs.append("Solution restore failed — attempting per-project restore.")
            for csproj in build_path.rglob("*.csproj"):
                _run_dotnet(f"dotnet restore {csproj.name}", csproj.parent, logs)
            restore_result = _run_dotnet(f"dotnet restore {sln_arg}".strip(), build_path, logs)

        logs.append(f"Running dotnet build on {build_path.name}.")
        build_cmd = f"dotnet build {sln_arg}".strip() if restore_result["exitCode"] == 0 else f"dotnet build {sln_arg} --no-restore".strip()
        build_result = _run_dotnet(build_cmd, build_path, logs)

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

        # Deduplicate errors by (file, code) before processing
        seen_errors: set[tuple[str, str]] = set()
        unique_errors: list[dict[str, Any]] = []
        for error in errors:
            key = (_extract_cs_path(error.get("file", "")), error.get("code", ""))
            if key not in seen_errors:
                seen_errors.add(key)
                unique_errors.append(error)

        for error in unique_errors:
            fix = _attempt_build_fix(error, Path(migrated_root), logs)
            if fix:
                applied_fixes.append(fix)
                if fix["file"] not in changed_files:
                    changed_files.append(fix["file"])
            else:
                unresolved.append(error)

        logs.append(f"Applied {len(applied_fixes)} fixes. Unresolved: {len(unresolved)}.")

        # Re-run build after fixes to confirm they worked
        rebuild_status = "notRun"
        rebuild_errors: list[dict[str, Any]] = []
        if applied_fixes:
            logs.append("Re-running dotnet build after fixes to verify.")
            build_path = Path(migrated_root)
            sln_files = list(build_path.glob("*.sln"))
            sln_arg = sln_files[0].name if sln_files else ""
            rebuild_result = _run_dotnet(f"dotnet build {sln_arg}".strip(), build_path, logs)
            rebuild_errors = _parse_build_output(rebuild_result["output"])
            rebuild_status = "succeeded" if rebuild_result["exitCode"] == 0 else "failed"
            logs.append(f"Post-fix build {rebuild_status}. Remaining errors: {len(rebuild_errors)}.")

        return {
            "fixStatus": "completed" if not unresolved else "partial",
            "appliedFixes": applied_fixes,
            "changedFiles": changed_files,
            "unresolvedErrors": unresolved,
            "rebuildStatus": rebuild_status,
            "rebuildErrors": rebuild_errors,
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
        sln_files = list(test_path.glob("*.sln"))
        sln_arg = str(sln_files[0].name) if len(sln_files) == 1 else "eShopOnWeb.sln" if (test_path / "eShopOnWeb.sln").exists() else (sln_files[0].name if sln_files else "")
        logs.append(f"Running dotnet test on {test_path.name} using {sln_arg}.")
        result = _run_dotnet(f"dotnet test {sln_arg} --no-build --logger trx".strip(), test_path, logs)

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
        rebuild_status = context.shared_state.get("build-fix", {}).get("rebuildStatus", "notRun")
        rebuild_errors = context.shared_state.get("build-fix", {}).get("rebuildErrors", [])

        # True overall status: prefer post-fix rebuild result if available
        if rebuild_status == "succeeded":
            overall_status = "succeeded"
        elif rebuild_status == "failed":
            overall_status = "failed"
        else:
            overall_status = build_status
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
            f"- Post-fix build status: {rebuild_status}",
            f"- Post-fix remaining errors: {len(rebuild_errors)}",
        ]
        for err in rebuild_errors[:10] or build_errors[:10]:
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
            "## Next Steps — Follow In Order",
            "",
            "### Step 1 — Update Connection String",
            "",
            f"Open: `{migrated_source}\\appsettings.json`",
            "",
            "Replace the `DefaultConnection` value with your actual SQL Server connection string:",
            "",
            "```json",
            '"ConnectionStrings": {',
            '  "DefaultConnection": "Server=YOUR_SERVER;Database=YOUR_DB;User Id=YOUR_USER;Password=YOUR_PASSWORD;TrustServerCertificate=True"',
            '}',
            "```",
            "",
            "### Step 2 — Run EF Core Migrations",
            "",
        ]

        # Detect if multiple DbContexts exist — need --context flag
        ef_context_flag = ""
        if migrated_source:
            cs_files = list(Path(migrated_source).rglob("*.cs"))
            ctx_count = sum(
                1 for f in cs_files
                if not any(p in f.parts for p in {"bin", "obj"})
                and re.search(r'class\s+\w+\s*:\s*(?:IdentityDbContext|DbContext)', f.read_text(encoding="utf-8", errors="ignore"))
            )
            if ctx_count > 1:
                ef_context_flag = " --context ApplicationDbContext"

        lines += [
            f"```",
            f"cd \"{migrated_source}\"",
            f"dotnet ef migrations add Initial{ef_context_flag}",
            f"dotnet ef database update{ef_context_flag}",
            "```",
            "",
            "### Step 3 — Run SQL Script (if exists)",
        ]

        source_path = context.input_data.get("sourcePath")
        sql_scripts = _find_sql_scripts(source_path, logs) if source_path else []
        if sql_scripts:
            lines.append("")
            lines.append("Run the following SQL scripts manually against your database:")
            for script in sql_scripts:
                lines.append(f"  - `{script}`")
            lines.append("")
            lines.append("Run them after `dotnet ef database update`.")
        else:
            lines.append("")
            lines.append("No SQL scripts found — skip this step.")

        lines += [
            "",
            "### Step 4 — Start the Backend",
            "",
            "```",
            f"cd \"{migrated_source}\"",
            "dotnet run",
            "```",
            "",
            f"Swagger UI: https://localhost:5001/swagger",
            "",
            "### Step 5 — Start the Frontend",
            "",
        ]

        if frontend_root:
            lines += [
                "```",
                f"cd \"{frontend_root}\"",
                "npm install",
                "npm run dev",
                "```",
                "",
                "Frontend runs at: http://localhost:5173",
            ]
        else:
            lines.append("Frontend was not generated — check frontend-migration agent logs.")

        lines += [
            "",
            "### Step 6 — Verify End-to-End",
            "",
            "1. Open http://localhost:5173 — Login page should load",
            "2. Register a user via /register",
            "3. Login — should receive JWT token",
            "4. Navigate to /menu — should load menu list from GET /api/menu",
        ]

        # Scan TODO comments from migrated source
        todo_items = _scan_todo_comments(migrated_source, logs) if migrated_source else []

        if todo_items:
            lines += ["", "## TODO Items — Manual Review Required", ""]
            current_file = None
            for item in todo_items:
                if item["file"] != current_file:
                    current_file = item["file"]
                    rel = current_file.replace(str(migrated_source), "").lstrip("\\/")
                    lines.append(f"### {rel}")
                lines.append(f"- Line {item['line']}: {item['message']}")
            lines += ["", "## TODO Items — Manual Review Required", ""]
            current_file = None
            for item in todo_items:
                if item["file"] != current_file:
                    current_file = item["file"]
                    # Show relative path
                    rel = current_file.replace(str(migrated_source), "").lstrip("\\/")
                    lines.append(f"### {rel}")
                lines.append(f"- Line {item['line']}: {item['message']}")

        from migration_agent_cli.core.guardrails import check_report_status_accuracy, check_jwt_expiry_set, write_audit_trail
        check_report_status_accuracy(overall_status, len(build_errors), logs)
        if migrated_source:
            check_jwt_expiry_set(migrated_source, logs)
        audit_path = write_audit_trail(context.run_id, str(run_dir(context)), agent_results if 'agent_results' in dir() else [], logs)
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return {
            "reports": [str(report_path)],
            "auditTrail": audit_path,
            "summary": {
                "overallStatus": overall_status,
                "agentOutputs": statuses,
                "buildErrors": len(build_errors),
                "testsPassed": test_passed,
                "testsFailed": test_failed,
                "frontendGenerated": bool(frontend_root),
                "todoItemsFound": len(todo_items),
                "manualActionsRequired": len(code_findings) + len(incompatible) + len(build_errors),
            },
        }


def _copytree_safe(src: Path, dst: Path, ignore, logs: list[str]) -> None:
    """copytree that skips files causing WinError 3 (path too long) instead of crashing."""
    dst.mkdir(parents=True, exist_ok=True)
    ignored = ignore(str(src), [x.name for x in src.iterdir()]) if src.is_dir() else set()
    errors = []
    for item in src.iterdir():
        if item.name in ignored:
            continue
        dest = dst / item.name
        try:
            if item.is_dir():
                _copytree_safe(item, dest, ignore, logs)
            else:
                shutil.copy2(item, dest)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        logs.append(f"Skipped {len(errors)} files during source copy (path too long or inaccessible).")


def _find_sql_scripts(source_path: str, logs: list[str]) -> list[str]:
    """Find .sql files in the source project for the report."""
    found: list[str] = []
    try:
        for sql_file in Path(source_path).rglob("*.sql"):
            if any(p in sql_file.parts for p in {"bin", "obj", ".git"}):
                continue
            found.append(str(sql_file))
    except Exception:
        pass
    if found:
        logs.append(f"Found {len(found)} SQL script(s) requiring manual execution.")
    return found


def _scan_todo_comments(migrated_source: str, logs: list[str]) -> list[dict[str, Any]]:
    """Scan all .cs and .jsx/.js files for TODO comments and return structured list."""
    todo_items: list[dict[str, Any]] = []
    root = Path(migrated_source)
    extensions = {'.cs', '.jsx', '.js', '.ts', '.tsx'}
    todo_pattern = re.compile(r'//\s*TODO[:\s](.+)', re.IGNORECASE)
    # Vendor file names and folders to skip
    vendor_folders = {'ext', 'node_modules', '.git', 'bin', 'obj'}
    vendor_filenames = {
        'jquery.js', 'jquery.min.js', 'jquery-1.8.2.js', 'jquery-2.1.1.js',
        'jquery-ui-1.8.24.js', 'jquery-ui-1.8.24.min.js',
        'jquery.validate.js', 'jquery.validate-vsdoc.js',
        'jquery.unobtrusive-ajax.js', 'angular.js', 'angular-scenario.js',
        'angular-cookies.js', 'modernizr-2.6.2.js', 'knockout-2.2.0.js',
        'knockout-2.2.0.debug.js', 'bootstrap.js', 'restangular.js',
    }

    for file in sorted(root.rglob('*')):
        if file.suffix not in extensions:
            continue
        if any(p in vendor_folders for p in file.parts):
            continue
        if file.name in vendor_filenames:
            continue
        try:
            for line_num, line in enumerate(file.read_text(encoding='utf-8', errors='ignore').splitlines(), 1):
                match = todo_pattern.search(line)
                if match:
                    todo_items.append({
                        'file': str(file),
                        'line': line_num,
                        'message': match.group(1).strip(),
                    })
        except Exception:
            continue

    logs.append(f"Found {len(todo_items)} TODO items in migrated source.")
    return todo_items


# Dead files — never copied to target-code/backend/
_DEAD_FILENAMES: set[str] = {
    "Web.config", "Web.Debug.config", "Web.Release.config",
    "packages.config",
    "Global.asax",
    "BundleConfig.cs", "RouteConfig.cs", "FilterConfig.cs", "WebApiConfig.cs",
    "AccountController.cs", "AuthConfig.cs", "InitializeSimpleMembershipAttribute.cs",
    "AssemblyInfo.cs",
    "Startup.cs",
    # EF6 T4 scaffolded files — replaced by EF Core ApplicationDbContext
    "SampleModel.Context.cs", "SampleModel.Designer.cs",
    "SampleModel.Context.tt", "SampleModel.tt",
}

# Dead folders — never copied to target-code/backend/
_DEAD_FOLDERS: set[str] = {"Views", "App_Start", "bin", "obj", ".git", ".vs", "artifacts", "frontend", "ext"}

# Useful file extensions to copy
_USEFUL_EXTENSIONS: set[str] = {
    ".cs", ".csproj", ".sln", ".json", ".xml",
}


def _selective_copy_to_target(source: Path, target: Path, logs: list[str]) -> None:
    """Copy only useful files from source to target-code/backend/, skipping all dead legacy files."""
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    for item in source.rglob("*"):
        # Skip dead folders
        if any(part in _DEAD_FOLDERS for part in item.parts):
            skipped += 1
            continue
        # Skip .edmx files
        if item.suffix == ".edmx":
            skipped += 1
            continue
        # Skip dead filenames
        if item.name in _DEAD_FILENAMES:
            skipped += 1
            continue
        # Only copy useful extensions (files) or recreate directories
        if item.is_dir():
            rel = item.relative_to(source)
            (target / rel).mkdir(parents=True, exist_ok=True)
            continue
        if item.suffix not in _USEFUL_EXTENSIONS:
            skipped += 1
            continue
        rel = item.relative_to(source)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, dest)
        except Exception:
            skipped += 1
            continue
        copied += 1
    logs.append(f"Selective copy complete: {copied} files copied, {skipped} dead/irrelevant files skipped.")


def _scaffold_backend_folders(migrated_root: Path, logs: list[str]) -> None:
    """Create Services/, DTOs/, Data/ folders in the backend project if they don't exist."""
    for csproj in migrated_root.rglob("*.csproj"):
        if any(p in csproj.parts for p in {"bin", "obj"}):
            continue
        project_dir = csproj.parent
        for folder in ("Services", "DTOs", "Data"):
            target = project_dir / folder
            if not target.exists():
                target.mkdir(exist_ok=True)
                logs.append(f"Scaffolded {folder}/ in {csproj.parent.name}.")


def _setup_wwwroot(migrated_root: Path, logs: list[str]) -> None:
    """Create wwwroot and copy Content/Scripts/fonts into it for each web project."""
    static_map = {"Content": "css", "Scripts": "js", "fonts": "fonts"}
    for csproj in migrated_root.rglob("*.csproj"):
        csproj_text = csproj.read_text(encoding="utf-8", errors="ignore")
        if "Microsoft.NET.Sdk.Web" not in csproj_text:
            continue
        project_dir = csproj.parent
        wwwroot = project_dir / "wwwroot"
        wwwroot.mkdir(exist_ok=True)
        for src_folder, dest_folder in static_map.items():
            src = project_dir / src_folder
            if src.exists():
                dest = wwwroot / dest_folder
                if dest.exists():
                    shutil.rmtree(dest)
                _copytree_safe(src, dest, shutil.ignore_patterns(), logs)
                logs.append(f"Copied {src_folder} → wwwroot/{dest_folder} in {csproj.parent.name}.")


def _regenerate_sln(migrated_root: Path, projects: list[str], logs: list[str]) -> None:
    """Regenerate .sln file for SDK-style projects to avoid old-format restore failures."""
    sln_files = list(migrated_root.glob("*.sln"))
    if not sln_files:
        return
    sln_name = sln_files[0].stem
    sln_files[0].unlink()
    result = subprocess.run(
        ["dotnet", "new", "sln", "-n", sln_name],
        capture_output=True, text=True, cwd=str(migrated_root)
    )
    if result.returncode != 0:
        logs.append(f"Failed to create new sln: {result.stderr[:200]}")
        return
    for relative_project in projects:
        csproj = migrated_root / relative_project
        if csproj.exists():
            subprocess.run(
                ["dotnet", "sln", f"{sln_name}.sln", "add", str(csproj)],
                capture_output=True, text=True, cwd=str(migrated_root)
            )
    logs.append(f"Regenerated {sln_name}.sln with {len(projects)} projects.")


def _cleanup_legacy_files(root: Path, logs: list[str]) -> None:
    """Delete legacy files that are fully replaced by modern equivalents after migration."""
    # Web.config family — replaced by appsettings.json
    for f in ["Web.config", "Web.Debug.config", "Web.Release.config"]:
        for target in root.rglob(f):
            if any(p in target.parts for p in {"bin", "obj", "Views"}):
                continue
            target.unlink()
            logs.append(f"Deleted legacy config file: {target.name}.")

    # packages.config — replaced by .csproj PackageReferences
    for target in root.rglob("packages.config"):
        if any(p in target.parts for p in {"bin", "obj"}):
            continue
        target.unlink()
        logs.append(f"Deleted packages.config — replaced by .csproj PackageReferences.")

    # Global.asax — replaced by Program.cs
    for target in root.rglob("Global.asax"):
        if any(p in target.parts for p in {"bin", "obj"}):
            continue
        target.unlink()
        logs.append(f"Deleted Global.asax — replaced by Program.cs.")


def _disable_dead_app_start_files(root: Path, changed_files: list[str], logs: list[str]) -> None:
    """Delete dead App_Start files — always deleted regardless of content since they are never needed in .NET Core."""
    dead_files = {"BundleConfig.cs", "RouteConfig.cs", "FilterConfig.cs", "WebApiConfig.cs"}
    for cs_file in root.rglob("*.cs"):
        if cs_file.name not in dead_files:
            continue
        if any(part in {"bin", "obj"} for part in cs_file.parts):
            continue
        cs_file.unlink()
        logs.append(f"Deleted dead App_Start file: {cs_file.name}.")


def _transform_razor_view(cshtml_file: Path, changed_files: list[str], logs: list[str]) -> None:
    """Transform MVC5 Razor syntax to ASP.NET Core compatible syntax."""
    original = cshtml_file.read_text(encoding="utf-8", errors="ignore")
    updated = original

    # @Styles.Render → actual <link> tags
    def replace_styles(match: re.Match) -> str:
        bundle_path = match.group(0)
        # Find wwwroot/css files relative to the project
        wwwroot_css = cshtml_file.parent
        while wwwroot_css.name not in ("Views", "wwwroot") and wwwroot_css.parent != wwwroot_css:
            wwwroot_css = wwwroot_css.parent
        css_dir = wwwroot_css.parent / "wwwroot" / "css" if "Views" in str(wwwroot_css) else wwwroot_css / "css"
        tags = []
        if css_dir.exists():
            for css_file in sorted(css_dir.glob("*.css")):
                if "min" not in css_file.name or not (css_dir / css_file.name.replace(".min", "")).exists():
                    tags.append(f'<link href="~/css/{css_file.name}" rel="stylesheet" />')
        return "\n    ".join(tags) if tags else "<!-- CSS files not found, add manually -->"

    def replace_scripts(match: re.Match) -> str:
        wwwroot_js = cshtml_file.parent
        while wwwroot_js.name not in ("Views", "wwwroot") and wwwroot_js.parent != wwwroot_js:
            wwwroot_js = wwwroot_js.parent
        js_dir = wwwroot_js.parent / "wwwroot" / "js" if "Views" in str(wwwroot_js) else wwwroot_js / "js"
        tags = []
        if js_dir.exists():
            for js_file in sorted(js_dir.glob("*.js")):
                if "min" not in js_file.name and "intellisense" not in js_file.name and "vsdoc" not in js_file.name:
                    tags.append(f'<script src="~/js/{js_file.name}"></script>')
        return "\n    ".join(tags) if tags else "<!-- JS files not found, add manually -->"

    updated = re.sub(r'@Scripts\.Render\([^)]+\)', replace_scripts, updated)
    updated = re.sub(r'@Styles\.Render\([^)]+\)', replace_styles, updated)

    # @Html.AntiForgeryToken()
    updated = re.sub(
        r'@Html\.AntiForgeryToken\(\)',
        '@Html.AntiForgeryToken() @* Verify: use [ValidateAntiForgeryToken] on controller *@',
        updated
    )
    updated = re.sub(r'@section\s+scripts\s*\{', '@section Scripts {', updated)

    if updated != original:
        cshtml_file.write_text(updated, encoding="utf-8")
        changed_files.append(str(cshtml_file))
        logs.append(f"Transformed Razor view: {cshtml_file.name}.")


def _generate_program_cs_from_global_asax(global_asax: Path, logs: list[str]) -> str:
    """Generate a minimal Program.cs scaffold from Global.asax.cs for legacy MVC apps."""
    try:
        text = global_asax.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = ""

    ns_match = re.search(r'namespace\s+([\w.]+)', text)
    namespace = ns_match.group(1) if ns_match else "MyApp"
    has_mvc = "RegisterRoutes" in text or "RouteConfig" in text
    has_bundle = "RegisterBundles" in text or "BundleConfig" in text
    has_filter = "RegisterGlobalFilters" in text or "FilterConfig" in text

    # Detect actual DbContext class name from migrated source
    db_context_name = None
    project_dir = global_asax.parent
    for cs_file in project_dir.rglob("*.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        try:
            cs_text = cs_file.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'public\s+partial\s+class\s+(\w+)\s*:\s*IdentityDbContext', cs_text)
            if m:
                db_context_name = m.group(1)
                break
        except Exception:
            continue

    # If no IdentityDbContext found, auth-transformation will generate ApplicationDbContext
    # with proper DbSets after ef-migration exposes entity names — don't generate it here
    if not db_context_name:
        db_context_name = "ApplicationDbContext"

    lines = [
        "// Auto-generated Program.cs from Global.asax.cs — review before building",
        "",
        "using Microsoft.AspNetCore.Authentication.JwtBearer;",
        "using Microsoft.AspNetCore.Identity;",
        "using Microsoft.EntityFrameworkCore;",
        "using Microsoft.IdentityModel.Tokens;",
        "using System.Text;",
        "using MvcApplication1.Models;",
        "",
        "var builder = WebApplication.CreateBuilder(args);",
        "",
        "// --- Services ---",
        "builder.Services.AddControllers();",
        "builder.Services.AddEndpointsApiExplorer();",
        "builder.Services.AddSwaggerGen();",
        "builder.Services.AddHttpContextAccessor();",
        "builder.Services.AddMemoryCache();",
        "builder.Services.AddDistributedMemoryCache();",
        "builder.Services.AddSession();",
        "",
        "// --- CORS ---",
        "builder.Services.AddCors(options =>",
        "{",
        "    options.AddPolicy(\"AllowFrontend\", policy =>",
        "        policy.WithOrigins(\"http://localhost:5173\")",
        "              .AllowAnyHeader()",
        "              .AllowAnyMethod());",
        "});",
        "",
        "// --- Identity ---",
        f"builder.Services.AddDbContext<{db_context_name}>(options =>",
        "    options.UseSqlServer(builder.Configuration.GetConnectionString(\"DefaultConnection\")));",
        f"builder.Services.AddIdentity<ApplicationUser, IdentityRole>()",
        f"    .AddEntityFrameworkStores<{db_context_name}>()",
        "    .AddDefaultTokenProviders();",
        "",
        "// --- JWT Authentication ---",
        "builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)",
        "    .AddJwtBearer(options =>",
        "    {",
        "        options.TokenValidationParameters = new TokenValidationParameters",
        "        {",
        "            ValidateIssuer = true,",
        "            ValidateAudience = true,",
        "            ValidateLifetime = true,",
        "            ValidateIssuerSigningKey = true,",
        "            ValidIssuer = builder.Configuration[\"Jwt:Issuer\"],",
        "            ValidAudience = builder.Configuration[\"Jwt:Audience\"],",
        "            IssuerSigningKey = new SymmetricSecurityKey(",
        "                Encoding.UTF8.GetBytes(builder.Configuration[\"Jwt:Key\"]!))",
        "        };",
        "    });",
        "",
        "var app = builder.Build();",
        "",
        "if (app.Environment.IsDevelopment())",
        "{",
        "    app.UseSwagger();",
        "    app.UseSwaggerUI();",
        "}",
        "else",
        "{",
        "    app.UseExceptionHandler(\"/Home/Error\");",
        "    app.UseHsts();",
        "}",
        "",
        "app.UseHttpsRedirection();",
        "app.UseStaticFiles();",
        "app.UseRouting();",
        "app.UseCors(\"AllowFrontend\");",
        "app.UseAuthentication();",
        "app.UseAuthorization();",
        "app.UseSession();",
        "",
    ]
    if has_bundle:
        lines.append("// TODO: BundleConfig.RegisterBundles — replace with Vite or Webpack bundling")
    if has_filter:
        lines.append("// TODO: FilterConfig.RegisterGlobalFilters — migrate to ASP.NET Core middleware")
    lines.append("app.MapControllers();")
    lines += ["", "app.Run();"]
    logs.append(f"Scaffolded Program.cs from {global_asax.name} for namespace {namespace}.")
    return "\n".join(lines)


def _attempt_build_fix(error: dict[str, Any], migrated_root: Path, logs: list[str]) -> dict[str, Any] | None:
    """Attempt automated fix for known build error codes."""
    code = error.get("code", "")
    file_path = error.get("file", "")
    message = error.get("message", "")

    # NU1101: Unable to find package — remove it from the .csproj
    if code == "NU1101":
        pkg_match = re.search(r'Unable to find package ([\w.]+)', message)
        if pkg_match:
            pkg_name = pkg_match.group(1)
            for csproj in migrated_root.rglob("*.csproj"):
                try:
                    csproj_text = csproj.read_text(encoding="utf-8", errors="ignore")
                    # Remove PackageReference line for this package
                    updated = re.sub(
                        rf'\s*<PackageReference\s+Include="{re.escape(pkg_name)}"[^/]*/?>',
                        '',
                        csproj_text,
                        flags=re.IGNORECASE,
                    )
                    if updated != csproj_text:
                        csproj.write_text(updated, encoding="utf-8")
                        logs.append(f"Removed unfindable package {pkg_name} from {csproj.name}.")
                        return {"file": str(csproj), "code": code, "fix": f"Removed package {pkg_name} — not available on nuget.org"}
                except Exception:
                    continue
        return None

    # Extract real .cs path — error file field may contain junk text before the path
    real_path = _extract_cs_path(file_path)
    if not real_path:
        return None

    target = Path(real_path)
    if not target.exists():
        target = migrated_root / real_path
    if not target.exists():
        return None

    # CS0102: duplicate member definition — remove duplicate DbSet properties
    if code == "CS0102":
        member_match = re.search(r"already contains a definition for '(\w+)'", message)
        if member_match:
            member_name = member_match.group(1)
            for cs_file in migrated_root.rglob("*.cs"):
                if any(p in cs_file.parts for p in {"bin", "obj"}):
                    continue
                try:
                    content = cs_file.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                # Find and remove duplicate DbSet property lines
                pattern = re.compile(
                    rf'(\s*public\s+virtual\s+DbSet<{re.escape(member_name)}>\s+{re.escape(member_name)}\s*{{\s*get;\s*set;\s*}})',
                    re.MULTILINE
                )
                matches = list(pattern.finditer(content))
                if len(matches) >= 2:
                    # Remove all but the first occurrence
                    updated = content
                    for m in reversed(matches[1:]):
                        updated = updated[:m.start()] + updated[m.end():]
                    cs_file.write_text(updated, encoding="utf-8")
                    logs.append(f"Removed duplicate DbSet<{member_name}> from {cs_file.name}.")
                    return {"file": str(cs_file), "code": code, "fix": f"Removed duplicate DbSet<{member_name}>"}
        return None

    # CS0579: duplicate assembly attribute — add GenerateAssemblyInfo=false to csproj
    if code == "CS0579":
        csproj_match = re.search(r'\[([^\]]+\.csproj)\]', message)
        if csproj_match:
            csproj = Path(csproj_match.group(1))
            if csproj.exists():
                csproj_text = csproj.read_text(encoding="utf-8", errors="ignore")
                if "<GenerateAssemblyInfo>" not in csproj_text:
                    csproj_text = csproj_text.replace(
                        "</PropertyGroup>",
                        "  <GenerateAssemblyInfo>false</GenerateAssemblyInfo>\n  </PropertyGroup>",
                        1
                    )
                    csproj.write_text(csproj_text, encoding="utf-8")
                    logs.append(f"Added GenerateAssemblyInfo=false to {csproj.name} to fix CS0579.")
                    return {"file": str(csproj), "code": code, "fix": "Added GenerateAssemblyInfo=false"}
        return None

    if target.suffix != ".cs":
        return None

    try:
        source = target.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    updated = source

    # CS0234 / CS0246: missing type or namespace
    if code in ("CS0234", "CS0246"):
        ns_match = re.search(r"type or namespace name '([\w.]+)'", message)
        missing = ns_match.group(1) if ns_match else ""

        using_map = {
            "IConfiguration": "using Microsoft.Extensions.Configuration;",
            "IMemoryCache": "using Microsoft.Extensions.Caching.Memory;",
            "IHttpContextAccessor": "using Microsoft.AspNetCore.Http;",
            "JsonSerializer": "using System.Text.Json;",
            "ILogger": "using Microsoft.Extensions.Logging;",
            "Controller": "using Microsoft.AspNetCore.Mvc;",
            "ActionResult": "using Microsoft.AspNetCore.Mvc;",
            "HttpGet": "using Microsoft.AspNetCore.Mvc;",
            "HttpPost": "using Microsoft.AspNetCore.Mvc;",
            "Route": "using Microsoft.AspNetCore.Mvc;",
            "BundleCollection": None,  # remove the file — System.Web.Optimization gone
            "GlobalFilterCollection": None,  # remove — System.Web.Mvc.Filters gone
            "Optimization": None,
            "HttpApplication": None,
        }

        if missing in using_map:
            fix_using = using_map[missing]
            if fix_using is None:
                # Comment out the entire file — it's a dead legacy helper
                updated = "// This file references legacy System.Web APIs not available in .NET Core.\n// It has been disabled during migration. Review and rewrite if needed.\n/*\n" + source + "\n*/"
            elif fix_using not in updated:
                updated = fix_using + "\n" + updated

        # System.Web namespace references — comment them out
        updated = re.sub(
            r'^(using System\.Web(?:\.\w+)*;)',
            r'// \1  // Removed: System.Web not available in .NET Core',
            updated, flags=re.MULTILINE
        )

    # CS0246: HttpConfiguration — delete WebApiConfig.cs entirely
    if code == "CS0246" and "HttpConfiguration" in message:
        if target.exists():
            target.unlink()
            logs.append(f"Deleted {target.name} — HttpConfiguration not available in .NET Core.")
            return {"file": str(target), "code": code, "fix": "Deleted file — HttpConfiguration is dead legacy"}
        return None

    # CS0103: name does not exist
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


def _exclude_file_from_csproj(cs_file: Path, migrated_root: Path, logs: list[str]) -> None:
    """Add a <Compile Remove="..."> entry to the nearest .csproj so the SDK doesn't compile this file."""
    for csproj in migrated_root.rglob("*.csproj"):
        if any(p in csproj.parts for p in {"bin", "obj"}):
            continue
        try:
            relative = cs_file.relative_to(csproj.parent)
        except ValueError:
            continue
        csproj_text = csproj.read_text(encoding="utf-8", errors="ignore")
        exclude_entry = f'    <Compile Remove="{relative}" />'
        if exclude_entry in csproj_text:
            return
        # Insert before </Project>
        updated = csproj_text.replace(
            "</Project>",
            f"\n  <ItemGroup>\n{exclude_entry}\n  </ItemGroup>\n</Project>"
        )
        csproj.write_text(updated, encoding="utf-8")
        logs.append(f"Excluded {cs_file.name} from {csproj.name} compilation.")
        return


def _extract_cs_path(file_field: str) -> str:
    """Extract the first valid absolute .cs file path from a potentially malformed error file string."""
    # Try to find a Windows absolute path ending in .cs
    match = re.search(r'([A-Za-z]:\\[^\n\r]+\.cs)', file_field)
    if match:
        return match.group(1).strip()
    # Fallback: return as-is if it looks like a plain path
    stripped = file_field.strip()
    if stripped.endswith('.cs'):
        return stripped
    return ''


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

    # MSBuild compiler format: file(line,col): error|warning CODE: message
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

    # NuGet restore error format: error NU1234 : message (no file/line)
    if severity == "error":
        nu_pattern = re.compile(r'error\s+(NU\d+)\s*:?\s*(.+)', re.IGNORECASE)
        seen_codes: set[str] = {f["code"] for f in findings}
        for match in nu_pattern.finditer(output):
            code = match.group(1).upper()
            if code not in seen_codes:
                findings.append({
                    "file": "",
                    "line": 0,
                    "column": 0,
                    "code": code,
                    "message": match.group(2).strip(),
                    "severity": severity,
                })
                seen_codes.add(code)

        # Generic "error :" lines without a code (e.g. dotnet restore failures)
        generic_pattern = re.compile(r'^\s*error\s*:\s*(.+)', re.IGNORECASE | re.MULTILINE)
        for match in generic_pattern.finditer(output):
            msg = match.group(1).strip()
            if msg and not any(f["message"] == msg for f in findings):
                findings.append({
                    "file": "",
                    "line": 0,
                    "column": 0,
                    "code": "RESTORE_ERROR",
                    "message": msg,
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


def _convert_csproj_to_sdk_style(project_xml: str, target_framework: str, has_assembly_info: bool = False, project_path: Path | None = None) -> str:
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
    # Skip System.*, Microsoft.*, DotNetOpenAuth.*, WebMatrix.*, WebGrease, Antlr — all dead in .NET Core
    _dead_reference_prefixes = (
        "System", "Microsoft", "DotNetOpenAuth", "WebMatrix",
        "WebGrease", "Antlr", "Modernizr",
    )
    for match in re.finditer(r'<Reference Include="([^"]+)"', project_xml):
        raw = match.group(1).split(",")[0].strip()
        if raw and not any(raw.startswith(p) for p in _dead_reference_prefixes) and raw not in existing_refs:
            existing_refs[raw] = "*"

    # Legacy packages that must be removed — they don't exist in .NET Core
    dead_packages = {
        # ASP.NET MVC / WebPages / WebAPI — replaced by ASP.NET Core
        "Microsoft.AspNet.Mvc", "Microsoft.AspNet.Mvc.FixedDisplayModes",
        "Microsoft.AspNet.Razor", "Microsoft.AspNet.WebPages",
        "Microsoft.AspNet.WebPages.Data", "Microsoft.AspNet.WebPages.OAuth",
        "Microsoft.AspNet.WebPages.WebData",
        "Microsoft.AspNet.WebApi", "Microsoft.AspNet.WebApi.Client",
        "Microsoft.AspNet.WebApi.Core", "Microsoft.AspNet.WebApi.OData",
        "Microsoft.AspNet.WebApi.WebHost",
        "Microsoft.AspNet.Web.Optimization", "Microsoft.Web.Infrastructure",
        # OAuth — no .NET 8 equivalent, replaced by ASP.NET Core Identity
        "DotNetOpenAuth.AspNet", "DotNetOpenAuth.Core",
        "DotNetOpenAuth.OAuth.Consumer", "DotNetOpenAuth.OAuth.Core",
        "DotNetOpenAuth.OpenId.Core", "DotNetOpenAuth.OpenId.RelyingParty",
        # OData / Spatial — legacy, replaced by newer packages
        "Microsoft.Data.Edm", "Microsoft.Data.OData", "System.Spatial",
        # Frontend/bundling — not needed in API-only .NET 8
        "WebGrease", "Antlr", "Antlr3.Runtime", "Modernizr", "Respond",
        "jQuery", "jQuery.UI.Combined", "jQuery.Validation",
        "Microsoft.jQuery.Unobtrusive.Ajax", "Microsoft.jQuery.Unobtrusive.Validation",
        "knockoutjs", "bootstrap",
        # HTTP client — built into .NET Core
        "Microsoft.Net.Http",
    }

    # Package mapping: old package → (new package name, new version)
    package_mapping: dict[str, tuple[str, str]] = {
        "EntityFramework": ("Microsoft.EntityFrameworkCore.SqlServer", "8.0.0"),
        "Newtonsoft.Json": ("Newtonsoft.Json", "13.0.3"),
    }

    # Read packages.config if present and merge into refs
    project_refs: list[str] = []
    if project_path:
        packages_config = project_path.parent / "packages.config"
        if packages_config.exists():
            text = packages_config.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r'id="([^"]+)".*?version="([^"]+)"', text):
                name, version = match.group(1), match.group(2)
                if name in dead_packages:
                    continue
                # Apply mapping — replace old package with new equivalent
                if name in package_mapping:
                    new_name, new_version = package_mapping[name]
                    if new_name not in existing_refs:
                        existing_refs[new_name] = new_version
                elif name not in existing_refs:
                    existing_refs[name] = version

        # Also filter dead packages from existing_refs
        for dead in dead_packages:
            existing_refs.pop(dead, None)
        # Apply mapping to existing_refs too
        for old_name, (new_name, new_version) in package_mapping.items():
            if old_name in existing_refs:
                existing_refs.pop(old_name)
                if new_name not in existing_refs:
                    existing_refs[new_name] = new_version

        # Read project references from original csproj — most reliable source
        for match in re.finditer(
            r'<ProjectReference\s+Include="([^"]+)"',
            project_xml, re.IGNORECASE
        ):
            ref_path = match.group(1).replace("\\", os.sep).replace("/", os.sep)
            # Resolve relative to original project location, then remap to migrated root
            if project_path:
                original_ref = (project_path.parent / ref_path).resolve()
                # Find the matching csproj in migrated root
                migrated_root = project_path.parent.parent
                for sibling in migrated_root.rglob("*.csproj"):
                    if sibling.name == original_ref.name and sibling != project_path:
                        if str(sibling) not in project_refs:
                            project_refs.append(str(sibling))
                        break

    assembly_info_line = "\n    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>" if has_assembly_info else ""

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
                f'<TargetFramework>{target_framework}</TargetFramework>\n    <ImplicitUsings>enable</ImplicitUsings>\n    <Nullable>enable</Nullable>{assembly_info_line}'
            )
        elif has_assembly_info and "<GenerateAssemblyInfo>" not in updated:
            updated = updated.replace(
                "<Nullable>enable</Nullable>",
                f"<Nullable>enable</Nullable>{assembly_info_line}"
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

    proj_ref_group = ""
    if project_refs and project_path:
        proj_refs_xml = "\n".join(
            f'    <ProjectReference Include="{os.path.relpath(p, project_path.parent)}" />'
            for p in project_refs
        )
        proj_ref_group = f"\n\n  <ItemGroup>\n{proj_refs_xml}\n  </ItemGroup>"

    return (
        f'<Project Sdk="{sdk}">\n\n'
        "  <PropertyGroup>\n"
        f"    <TargetFramework>{target_framework}</TargetFramework>\n"
        "    <ImplicitUsings>enable</ImplicitUsings>\n"
        f"    <Nullable>enable</Nullable>{assembly_info_line}\n"
        "  </PropertyGroup>"
        f"{item_group}{proj_ref_group}\n\n"
        "</Project>\n"
    )
