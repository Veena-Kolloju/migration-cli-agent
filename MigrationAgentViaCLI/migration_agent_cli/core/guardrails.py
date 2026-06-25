from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


_SECRET_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'Password=[^;\'\"]{4,}',
        r'pwd=[^;\'\"]{4,}',
        r'api[_-]?key\s*=\s*["\'][\w\-]{10,}',
        r'secret\s*=\s*["\'][\w\-]{10,}',
    ]
]


def check_cs_file(source: str, transformed: str, file_name: str, logs: list[str]) -> str:
    """Run all C# guardrail checks. Returns transformed source or reverts if critical issue found."""

    # 1. Namespace removed
    if "namespace " in source and "namespace " not in transformed:
        logs.append(f"GUARDRAIL: namespace removed from {file_name} — reverting transformation.")
        return source

    # 2. Brace balance
    if transformed.count("{") != transformed.count("}"):
        logs.append(f"GUARDRAIL: Unbalanced braces in {file_name} — reverting transformation.")
        return source

    # 3. Shrinkage > 30%
    original_lines = len(source.splitlines())
    new_lines = len(transformed.splitlines())
    if original_lines > 10 and new_lines < original_lines * 0.7:
        logs.append(f"GUARDRAIL: {file_name} shrank by >30% after transformation — review manually.")

    # 4. Secret detection
    check_secrets(transformed, file_name, logs)

    return transformed


def check_secrets(content: str, file_name: str, logs: list[str]) -> None:
    """Warn if hardcoded secrets detected in generated content."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            logs.append(f"GUARDRAIL: Possible hardcoded secret in {file_name} — review before committing.")
            break


def check_json(content: str, file_name: str, logs: list[str]) -> bool:
    """Validate JSON content. Returns True if valid."""
    try:
        json.loads(content)
        return True
    except json.JSONDecodeError as e:
        logs.append(f"GUARDRAIL: Invalid JSON in {file_name}: {e} — skipping write.")
        return False


def check_xml(content: str, file_name: str, logs: list[str]) -> bool:
    """Validate XML content. Returns True if valid."""
    try:
        ET.fromstring(content)
        return True
    except ET.ParseError as e:
        logs.append(f"GUARDRAIL: Invalid XML in {file_name}: {e} — skipping write.")
        return False


def check_react_export(content: str, component_name: str, file_name: str, logs: list[str]) -> str:
    """Ensure React component has a default export."""
    if f"export default {component_name}" not in content:
        logs.append(f"GUARDRAIL: Missing default export in {file_name} — adding it.")
        return content + f"\nexport default {component_name};\n"
    return content


def check_program_cs_exists(migrated_root: str, logs: list[str]) -> None:
    """Warn if Program.cs was not generated — app will not compile without it."""
    matches = list(Path(migrated_root).rglob("Program.cs"))
    matches = [p for p in matches if not any(x in p.parts for x in {"bin", "obj"})]
    if not matches:
        logs.append("GUARDRAIL: Program.cs not found in migrated source — app will not compile. Review code-transformation output.")


def check_target_framework(migrated_root: str, expected: str, logs: list[str]) -> None:
    """Warn if any .csproj still has the wrong TargetFramework after conversion."""
    for csproj in Path(migrated_root).rglob("*.csproj"):
        if any(x in csproj.parts for x in {"bin", "obj"}):
            continue
        content = csproj.read_text(encoding="utf-8", errors="ignore")
        if f"<TargetFramework>{expected}</TargetFramework>" not in content:
            logs.append(f"GUARDRAIL: {csproj.name} does not target {expected} — project conversion may have failed.")


def check_app_jsx_exists(frontend_root: str, logs: list[str]) -> None:
    """Warn if App.jsx was not generated — React frontend will be blank without it."""
    app_jsx = Path(frontend_root) / "src" / "App.jsx"
    if not app_jsx.exists():
        logs.append("GUARDRAIL: App.jsx not found in frontend/src — React app will not render. Review frontend-migration output.")


def check_report_status_accuracy(overall_status: str, build_error_count: int, logs: list[str]) -> None:
    """Warn if report says completed but build errors still exist."""
    if overall_status == "completed" and build_error_count > 0:
        logs.append(f"GUARDRAIL: Report overallStatus is 'completed' but {build_error_count} build errors exist — review before deploying.")


# ---------------------------------------------------------------------------
# C# Coding Standards
# ---------------------------------------------------------------------------

def check_controller_attributes(migrated_root: str, logs: list[str]) -> None:
    """Check every generated controller has [ApiController] and [Route] attributes."""
    for cs_file in Path(migrated_root).rglob("*Controller.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        content = cs_file.read_text(encoding="utf-8", errors="ignore")
        if "ControllerBase" not in content and "Controller" not in content:
            continue
        if "[ApiController]" not in content:
            logs.append(f"STANDARD: {cs_file.name} is missing [ApiController] attribute — required for .NET 8 REST controllers.")
        if "[Route" not in content:
            logs.append(f"STANDARD: {cs_file.name} is missing [Route] attribute — controller endpoints will not be reachable.")


def check_controller_methods_have_http_attributes(migrated_root: str, logs: list[str]) -> None:
    """Check every public controller method has an HTTP verb attribute."""
    pattern = re.compile(r'public\s+(?:async\s+)?(?:IActionResult|Task<IActionResult>)\s+(\w+)\s*\(', re.MULTILINE)
    http_attr = re.compile(r'\[Http(Get|Post|Put|Delete|Patch)')
    for cs_file in Path(migrated_root).rglob("*Controller.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        content = cs_file.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(content):
            preceding = content[:match.start()].rstrip()
            if not http_attr.search(preceding[-200:]):
                logs.append(f"STANDARD: {cs_file.name} method '{match.group(1)}' is missing HTTP verb attribute — endpoint will not be mapped.")


def check_authentication_middleware_order(migrated_root: str, logs: list[str]) -> None:
    """Check UseAuthentication comes before UseAuthorization in Program.cs."""
    for program_cs in Path(migrated_root).rglob("Program.cs"):
        if any(p in program_cs.parts for p in {"bin", "obj"}):
            continue
        content = program_cs.read_text(encoding="utf-8", errors="ignore")
        auth_index = content.find("app.UseAuthentication()")
        authz_index = content.find("app.UseAuthorization()")
        if auth_index != -1 and authz_index != -1 and auth_index > authz_index:
            logs.append(f"STANDARD: {program_cs.name} — UseAuthentication() must come BEFORE UseAuthorization(). Current order will cause auth to silently fail.")


def check_program_cs_build_order(migrated_root: str, logs: list[str]) -> None:
    """Check builder.Build() is called before app.Run() in Program.cs."""
    for program_cs in Path(migrated_root).rglob("Program.cs"):
        if any(p in program_cs.parts for p in {"bin", "obj"}):
            continue
        content = program_cs.read_text(encoding="utf-8", errors="ignore")
        build_index = content.find("builder.Build()")
        run_index = content.find("app.Run()")
        if build_index == -1:
            logs.append(f"STANDARD: {program_cs.name} — builder.Build() is missing. App cannot start without it.")
        elif run_index != -1 and build_index > run_index:
            logs.append(f"STANDARD: {program_cs.name} — builder.Build() must come BEFORE app.Run().")


# ---------------------------------------------------------------------------
# Security Best Practices
# ---------------------------------------------------------------------------

def check_authorize_on_controllers(migrated_root: str, logs: list[str]) -> None:
    """Check controllers have [Authorize] or explicit [AllowAnonymous]."""
    for cs_file in Path(migrated_root).rglob("*Controller.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        content = cs_file.read_text(encoding="utf-8", errors="ignore")
        if "AuthController" in cs_file.name:
            continue
        if "[Authorize]" not in content and "[AllowAnonymous]" not in content:
            logs.append(f"STANDARD: {cs_file.name} has no [Authorize] or [AllowAnonymous] — all endpoints are publicly accessible by default.")


def check_jwt_placeholder_key(migrated_root: str, logs: list[str]) -> None:
    """Warn if JWT key is still the placeholder value in appsettings.json."""
    for appsettings in Path(migrated_root).rglob("appsettings.json"):
        if any(p in appsettings.parts for p in {"bin", "obj"}):
            continue
        try:
            data = json.loads(appsettings.read_text(encoding="utf-8", errors="ignore"))
            jwt_key = data.get("Jwt", {}).get("Key", "")
            if "CHANGE-THIS" in jwt_key or jwt_key == "":
                logs.append(f"STANDARD: {appsettings.name} — JWT Key is still a placeholder. Replace with a strong secret before deploying.")
        except Exception:
            pass


def check_cors_wildcard(migrated_root: str, logs: list[str]) -> None:
    """Warn if CORS policy uses AllowAnyOrigin — not safe for production."""
    for program_cs in Path(migrated_root).rglob("Program.cs"):
        if any(p in program_cs.parts for p in {"bin", "obj"}):
            continue
        content = program_cs.read_text(encoding="utf-8", errors="ignore")
        if "AllowAnyOrigin" in content:
            logs.append(f"STANDARD: {program_cs.name} — CORS policy uses AllowAnyOrigin(). Restrict to specific origins before deploying to production.")


# ---------------------------------------------------------------------------
# Design Pattern Standards
# ---------------------------------------------------------------------------

def check_no_new_dbcontext_in_controllers(migrated_root: str, logs: list[str]) -> None:
    """Check DbContext is not directly instantiated with 'new' inside controllers."""
    pattern = re.compile(r'new\s+\w+Context\s*\(', re.MULTILINE)
    for cs_file in Path(migrated_root).rglob("*Controller.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        content = cs_file.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(content):
            logs.append(f"STANDARD: {cs_file.name} — DbContext should be injected via constructor, not instantiated with 'new'. Use Dependency Injection pattern.")


def check_constructor_injection(migrated_root: str, logs: list[str]) -> None:
    """Check services are not created with 'new' inside controllers — should use DI."""
    pattern = re.compile(r'=\s*new\s+\w+Service\s*\(', re.MULTILINE)
    for cs_file in Path(migrated_root).rglob("*Controller.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        content = cs_file.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(content):
            logs.append(f"STANDARD: {cs_file.name} — Services should be injected via constructor (Dependency Injection), not created with 'new'.")


# ---------------------------------------------------------------------------
# React Best Practices
# ---------------------------------------------------------------------------

def check_react_api_calls_use_service(frontend_root: str, logs: list[str]) -> None:
    """Check React components use the central api.js service, not direct axios calls."""
    pattern = re.compile(r'axios\.(get|post|put|delete)\s*\(')
    src_dir = Path(frontend_root) / "src"
    if not src_dir.exists():
        return
    for jsx_file in src_dir.rglob("*.jsx"):
        if "services" in jsx_file.parts:
            continue
        try:
            content = jsx_file.read_text(encoding="utf-8", errors="ignore")
            if pattern.search(content):
                logs.append(f"STANDARD: {jsx_file.name} — Direct axios calls detected. Use the central api.js service instead for consistent auth headers and base URL.")
        except Exception:
            continue


def check_react_error_handling(frontend_root: str, logs: list[str]) -> None:
    """Check React components have try/catch around API calls."""
    src_dir = Path(frontend_root) / "src" / "components"
    if not src_dir.exists():
        return
    for jsx_file in src_dir.rglob("*.jsx"):
        try:
            content = jsx_file.read_text(encoding="utf-8", errors="ignore")
            has_api_call = "await" in content and ("api." in content or "Service." in content)
            has_try_catch = "try {" in content or "try{" in content
            if has_api_call and not has_try_catch:
                logs.append(f"STANDARD: {jsx_file.name} — API calls found without try/catch error handling. Add error handling to prevent unhandled promise rejections.")
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Entry points — called per agent
# ---------------------------------------------------------------------------

def run_csharp_standards(migrated_root: str, logs: list[str]) -> None:
    """Run all C# standards and best practice checks."""
    check_controller_attributes(migrated_root, logs)
    check_controller_methods_have_http_attributes(migrated_root, logs)
    check_authentication_middleware_order(migrated_root, logs)
    check_program_cs_build_order(migrated_root, logs)
    check_authorize_on_controllers(migrated_root, logs)
    check_jwt_placeholder_key(migrated_root, logs)
    check_cors_wildcard(migrated_root, logs)
    check_no_new_dbcontext_in_controllers(migrated_root, logs)
    check_constructor_injection(migrated_root, logs)


def run_react_standards(frontend_root: str, logs: list[str]) -> None:
    """Run all React standards and best practice checks."""
    check_react_api_calls_use_service(frontend_root, logs)
    check_react_error_handling(frontend_root, logs)


# ---------------------------------------------------------------------------
# 1. ConnectionStrings Validation
# ---------------------------------------------------------------------------

def check_connection_strings_exist(migrated_root: str, logs: list[str]) -> None:
    """Warn if ConnectionStrings section is missing from appsettings.json."""
    for appsettings in Path(migrated_root).rglob("appsettings.json"):
        if any(p in appsettings.parts for p in {"bin", "obj"}):
            continue
        try:
            data = json.loads(appsettings.read_text(encoding="utf-8", errors="ignore"))
            if "ConnectionStrings" not in data:
                logs.append(f"GUARDRAIL: {appsettings.name} — ConnectionStrings section missing. App will crash at startup without a database connection string.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2. DbSet Existence Check
# ---------------------------------------------------------------------------

def check_dbset_exists(migrated_root: str, logs: list[str]) -> None:
    """Warn if migrated DbContext has no DbSet — EF migration will produce nothing."""
    for cs_file in Path(migrated_root).rglob("*.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        try:
            content = cs_file.read_text(encoding="utf-8", errors="ignore")
            if "DbContext" in content and re.search(r'class\s+\w+\s*:\s*(?:IdentityDbContext|DbContext)', content):
                if "DbSet<" not in content:
                    logs.append(f"GUARDRAIL: {cs_file.name} — DbContext has no DbSet<> properties. EF Core migration will produce an empty database schema.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 3. JWT ExpiryMinutes Check
# ---------------------------------------------------------------------------

def check_jwt_expiry_set(migrated_root: str, logs: list[str]) -> None:
    """Warn if JWT ExpiryMinutes is missing or zero in appsettings.json."""
    for appsettings in Path(migrated_root).rglob("appsettings.json"):
        if any(p in appsettings.parts for p in {"bin", "obj"}):
            continue
        try:
            data = json.loads(appsettings.read_text(encoding="utf-8", errors="ignore"))
            expiry = data.get("Jwt", {}).get("ExpiryMinutes", "")
            if not expiry or str(expiry) == "0":
                logs.append(f"STANDARD: {appsettings.name} — JWT ExpiryMinutes is not set. Tokens will never expire — security risk.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 4. Async/Await Check
# ---------------------------------------------------------------------------

def check_async_controllers(migrated_root: str, logs: list[str]) -> None:
    """Warn if controller methods doing data access are not async."""
    pattern = re.compile(r'public\s+(?!async)\s*IActionResult\s+\w+\s*\([^)]*\)', re.MULTILINE)
    data_keywords = ["DbContext", "_context", "_db", "await", "Repository"]
    for cs_file in Path(migrated_root).rglob("*Controller.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        try:
            content = cs_file.read_text(encoding="utf-8", errors="ignore")
            has_data_access = any(kw in content for kw in data_keywords)
            if has_data_access and pattern.search(content):
                logs.append(f"STANDARD: {cs_file.name} — Non-async controller methods detected with data access. Use async/await for .NET 8 best practices.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 5. Input Validation Check
# ---------------------------------------------------------------------------

def check_input_validation(migrated_root: str, logs: list[str]) -> None:
    """Warn if POST/PUT controller methods are missing input validation."""
    post_pattern = re.compile(r'\[Http(Post|Put)\].*?public\s+(?:async\s+)?(?:Task<)?IActionResult', re.DOTALL)
    for cs_file in Path(migrated_root).rglob("*Controller.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        try:
            content = cs_file.read_text(encoding="utf-8", errors="ignore")
            if post_pattern.search(content):
                if "ModelState.IsValid" not in content and "[Required]" not in content:
                    logs.append(f"STANDARD: {cs_file.name} — POST/PUT methods found without ModelState.IsValid or [Required] validation. Add input validation for OWASP compliance.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 6. Dependency Vulnerability Check
# ---------------------------------------------------------------------------

def check_dependency_vulnerabilities(migrated_root: str, logs: list[str]) -> None:
    """Run dotnet list package --vulnerable and warn if any found."""
    import subprocess
    for csproj in Path(migrated_root).rglob("*.csproj"):
        if any(p in csproj.parts for p in {"bin", "obj"}):
            continue
        try:
            result = subprocess.run(
                ["dotnet", "list", str(csproj), "package", "--vulnerable"],
                capture_output=True, text=True, timeout=60, cwd=str(csproj.parent)
            )
            output = result.stdout + result.stderr
            if "critical" in output.lower() or "high" in output.lower():
                logs.append(f"GUARDRAIL: {csproj.name} — Vulnerable NuGet packages detected (critical/high severity). Run 'dotnet list package --vulnerable' for details.")
            elif "moderate" in output.lower() or "low" in output.lower():
                logs.append(f"STANDARD: {csproj.name} — Vulnerable NuGet packages detected (moderate/low severity). Review before deploying.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 7. Token & Cost Reporting
# ---------------------------------------------------------------------------

def report_llm_usage(agent_title: str, agentic_review: dict, logs: list[str]) -> dict:
    """Extract and report token usage from LLM response if available."""
    usage = agentic_review.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
    if total_tokens:
        logs.append(f"LLM_USAGE: {agent_title} — prompt_tokens={prompt_tokens}, completion_tokens={completion_tokens}, total={total_tokens}.")
    elif agentic_review.get("provider") == "skipped":
        logs.append(f"LLM_USAGE: {agent_title} — skipped (mechanical agent), tokens=0.")
    return {
        "agent": agent_title,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "totalTokens": total_tokens,
    }


# ---------------------------------------------------------------------------
# 8. Audit Trail
# ---------------------------------------------------------------------------

def write_audit_trail(run_id: str, output_dir: str, agent_results: list[dict], logs: list[str]) -> str:
    """Write consolidated audit trail for the migration run."""
    from datetime import datetime, timezone

    # Aggregate token usage across all agents
    total_prompt_tokens = 0
    total_completion_tokens = 0
    per_agent_tokens: list[dict] = []
    for r in agent_results:
        usage = r.get("output", {}).get("agenticReview", {}).get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        total_prompt_tokens += pt
        total_completion_tokens += ct
        per_agent_tokens.append({
            "agentId": r.get("agent_id"),
            "promptTokens": pt,
            "completionTokens": ct,
            "totalTokens": pt + ct,
        })

    total_tokens = total_prompt_tokens + total_completion_tokens

    audit = {
        "runId": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tokenConsumption": {
            "totalPromptTokens": total_prompt_tokens,
            "totalCompletionTokens": total_completion_tokens,
            "totalTokens": total_tokens,
            "perAgent": per_agent_tokens,
        },
        "agentsSummary": [
            {
                "agentId": r.get("agent_id"),
                "status": r.get("status"),
                "startedAt": r.get("started_at"),
                "completedAt": r.get("completed_at"),
                "logCount": len(r.get("logs", [])),
                "guardrailWarnings": [l for l in r.get("logs", []) if l.startswith("GUARDRAIL:") or l.startswith("STANDARD:")],
            }
            for r in agent_results
        ],
        "totalGuardrailWarnings": sum(
            1 for r in agent_results
            for l in r.get("logs", [])
            if l.startswith("GUARDRAIL:") or l.startswith("STANDARD:")
        ),
    }
    audit_path = Path(output_dir) / "audit.json"
    audit_path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    logs.append(f"Audit trail written to {audit_path}. Total tokens used: {total_tokens}.")
    return str(audit_path)
