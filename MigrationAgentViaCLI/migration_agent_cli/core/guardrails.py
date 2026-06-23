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
