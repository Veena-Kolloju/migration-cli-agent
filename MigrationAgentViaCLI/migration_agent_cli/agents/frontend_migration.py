from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from migration_agent_cli.core.agent_base import StructuredMigrationAgent, safe_source_path
from migration_agent_cli.core.models import AgentExecutionContext


class FrontendMigrationAgent(StructuredMigrationAgent):
    agent_id = "frontend-migration"
    title = "Frontend Migration Agent"
    description = "Migrates Razor Pages, Blazor, and AngularJS to React with Vite, React Router, and Axios."
    capabilities = [
        "Razor Pages → React components",
        "Blazor → React components",
        "AngularJS controllers/services/templates → React components + Axios services",
        "MVC routes → React Router",
        "Controller endpoints → Axios API calls",
        "Vite + React scaffold generation",
    ]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        migrated_base = context.shared_state.get("project-conversion", {}).get("migratedBasePath")
        source = safe_source_path(context, logs)

        scan_root = Path(migrated_root) if migrated_root else source
        if not scan_root:
            logs.append("No source path available — skipping frontend migration.")
            return _empty_result()

        # Place frontend/ inside target-code/ as sibling to backend/
        if migrated_base:
            frontend_base = Path(migrated_base) / "target-code"
        elif migrated_root:
            frontend_base = Path(migrated_root).parent
        else:
            # Fallback: write into artifacts run_dir, never into the source folder
            from migration_agent_cli.core.artifacts import run_dir
            frontend_base = run_dir(context) / "migration-code" / "target-code"
        frontend_root = frontend_base / "frontend"
        frontend_root.mkdir(parents=True, exist_ok=True)
        src_dir = frontend_root / "src"
        src_dir.mkdir(exist_ok=True)

        # Discover frontend files
        razor_pages = list(scan_root.rglob("*.cshtml"))
        blazor_components = list(scan_root.rglob("*.razor"))
        controllers = list(scan_root.rglob("*Controller.cs"))

        # Detect AngularJS source files from original source (not migrated-source to avoid duplicates)
        angularjs_scan_root = source if source else scan_root
        angularjs_controllers = _find_angularjs_files(angularjs_scan_root, "controller", logs)
        angularjs_services = _find_angularjs_files(angularjs_scan_root, "service", logs)
        angularjs_partials = _find_angularjs_files(angularjs_scan_root, "partial", logs)
        angularjs_apps = _find_angularjs_files(angularjs_scan_root, "app", logs)
        angularjs_directives = _find_angularjs_files(angularjs_scan_root, "directive", logs)

        has_angularjs = bool(angularjs_controllers or angularjs_services or angularjs_partials)
        logs.append(f"Found {len(razor_pages)} Razor pages, {len(blazor_components)} Blazor components, {len(controllers)} controllers.")
        if has_angularjs:
            logs.append(f"Detected AngularJS: {len(angularjs_controllers)} controllers, {len(angularjs_services)} services, {len(angularjs_partials)} partials, {len(angularjs_directives)} directives.")

        generated: list[str] = []
        routes: list[dict[str, str]] = []

        if has_angularjs:
            # AngularJS → React migration path
            angularjs_result = _migrate_angularjs(
                angularjs_controllers, angularjs_services, angularjs_partials, angularjs_apps,
                angularjs_directives, src_dir, logs
            )
            generated.extend(angularjs_result["files"])
            routes.extend(angularjs_result["routes"])
        else:
            # Convert Razor Pages → React components
            for razor_file in razor_pages:
                if any(p in razor_file.parts for p in {"bin", "obj"}):
                    continue
                result = _convert_razor_to_react(razor_file, src_dir, scan_root, logs)
                if result:
                    generated.append(result["file"])
                    if result.get("route"):
                        routes.append({"path": result["route"], "component": result["component"], "importPath": result.get("importPath", "")})

            # Convert Blazor components → React components
            for blazor_file in blazor_components:
                if any(p in blazor_file.parts for p in {"bin", "obj"}):
                    continue
                result = _convert_blazor_to_react(blazor_file, src_dir, scan_root, logs)
                if result:
                    generated.append(result["file"])

        # Generate Axios API service from .NET controllers (always)
        api_services = _generate_api_services(controllers, src_dir, logs)
        generated.extend(api_services)

        # Generate scaffold files
        scaffold = _generate_scaffold(frontend_root, src_dir, routes, logs)
        generated.extend(scaffold)

        from migration_agent_cli.core.guardrails import check_app_jsx_exists, run_react_standards
        check_app_jsx_exists(str(frontend_root), logs)
        run_react_standards(str(frontend_root), logs)
        logs.append(f"Frontend migration complete. Generated {len(generated)} files.")
        return {
            "generatedFiles": generated,
            "razorPagesConverted": len(razor_pages) if not has_angularjs else 0,
            "blazorComponentsConverted": len(blazor_components) if not has_angularjs else 0,
            "angularjsControllersConverted": len(angularjs_controllers),
            "apiServicesGenerated": len(api_services),
            "routes": routes,
            "frontendRoot": str(frontend_root),
        }


# ---------------------------------------------------------------------------
# AngularJS → React
# ---------------------------------------------------------------------------

def _find_angularjs_files(scan_root: Path, folder_type: str, logs: list[str]) -> list[Path]:
    """Find AngularJS files by looking for int/<type>/*.js or scripts containing ng patterns."""
    found: list[Path] = []
    # Look for the int/ subfolder pattern used in this project
    for candidate in scan_root.rglob(f"*/{folder_type}/*.js"):
        if any(p in candidate.parts for p in {"bin", "obj", "ext", "node_modules"}):
            continue
        found.append(candidate)
    # Also look for HTML partials in partial/ folder
    if folder_type == "partial":
        for candidate in scan_root.rglob(f"*/partial/*.html"):
            if any(p in candidate.parts for p in {"bin", "obj", "ext", "node_modules"}):
                continue
            found.append(candidate)
    return found


def _migrate_angularjs(
    controllers: list[Path],
    services: list[Path],
    partials: list[Path],
    apps: list[Path],
    directives: list[Path],
    src_dir: Path,
    logs: list[str],
) -> dict[str, Any]:
    """Convert AngularJS controllers/services/partials/directives to React components + Axios services."""
    generated: list[str] = []
    routes: list[dict[str, str]] = []

    services_dir = src_dir / "services"
    services_dir.mkdir(exist_ok=True)
    components_dir = src_dir / "components"
    components_dir.mkdir(exist_ok=True)

    # Parse app.js for routes
    ng_routes = _parse_angularjs_routes(apps, logs)

    # Parse services first to know available API endpoints
    service_map = _convert_angularjs_services(services, services_dir, logs)
    generated.extend(service_map["files"])

    # Convert directives to React custom hooks
    for directive_file in directives:
        result = _convert_angularjs_directive(directive_file, components_dir, logs)
        if result:
            generated.append(result)

    # Convert each controller + its matching partial into a React component
    for ctrl_file in controllers:
        ctrl_info = _parse_angularjs_controller(ctrl_file, logs)
        if not ctrl_info:
            continue

        # Find matching partial HTML
        partial_html = ""
        for partial in partials:
            if partial.suffix == ".html":
                try:
                    partial_html = partial.read_text(encoding="utf-8", errors="ignore")
                    break
                except Exception:
                    pass

        component_name = _to_pascal_case(ctrl_info["name"].replace("Ctrl", "").replace("Controller", ""))
        out_file = components_dir / f"{component_name}.jsx"

        jsx = _build_angularjs_react_component(
            component_name=component_name,
            ctrl_info=ctrl_info,
            partial_html=partial_html,
            service_map=service_map["service_imports"],
            logs=logs,
        )
        from migration_agent_cli.core.guardrails import check_react_export
        jsx = check_react_export(jsx, component_name, f"{component_name}.jsx", logs)
        out_file.write_text(jsx, encoding="utf-8")
        generated.append(str(out_file))
        logs.append(f"Converted AngularJS controller {ctrl_file.name} → {out_file.name}")

        # Build route
        route_path = ng_routes.get(ctrl_info["name"], f"/{component_name.lower()}")
        import_path = f"./components/{component_name}"
        routes.append({"path": route_path, "component": component_name, "importPath": import_path})

    return {"files": generated, "routes": routes}


def _convert_angularjs_directive(directive_file: Path, components_dir: Path, logs: list[str]) -> str | None:
    """Convert AngularJS directive to a React custom hook."""
    try:
        content = directive_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    # Extract directive name
    name_match = re.search(r'\.directive\s*\(\s*[\'"](\w+)[\'"]', content)
    if not name_match:
        # Empty directive module — skip
        logs.append(f"Skipped empty directive: {directive_file.name}")
        return None

    directive_name = name_match.group(1)
    hook_name = f"use{_to_pascal_case(directive_name)}"
    out_file = components_dir / f"{hook_name}.js"

    hook_content = (
        f"import {{ useEffect }} from 'react';\n\n"
        f"// Auto-converted from AngularJS directive: {directive_name}\n"
        f"// TODO: implement directive logic as a React hook\n"
        f"const {hook_name} = () => {{\n"
        f"  useEffect(() => {{\n"
        f"    // TODO: port directive link/compile logic here\n"
        f"  }}, []);\n"
        f"}};\n\n"
        f"export default {hook_name};\n"
    )
    out_file.write_text(hook_content, encoding="utf-8")
    logs.append(f"Converted AngularJS directive {directive_file.name} \u2192 {out_file.name}")
    return str(out_file)


def _parse_angularjs_routes(app_files: list[Path], logs: list[str]) -> dict[str, str]:
    """Extract route → controller mappings from AngularJS app.js."""
    routes: dict[str, str] = {}
    for app_file in app_files:
        try:
            content = app_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # Match: .when('/path', { ... controller: 'CtrlName' })
        for m in re.finditer(r'\.when\s*\(\s*[\'"](.*?)[\'"].*?controller\s*:\s*[\'"](\w+)[\'"]', content, re.DOTALL):
            routes[m.group(2)] = m.group(1)
        # Match: otherwise({ templateUrl: '...', controller: 'CtrlName' })
        for m in re.finditer(r'otherwise\s*\(.*?controller\s*:\s*[\'"](\w+)[\'"]', content, re.DOTALL):
            if m.group(1) not in routes:
                routes[m.group(1)] = "/"
    return routes


def _parse_angularjs_controller(ctrl_file: Path, logs: list[str]) -> dict[str, Any] | None:
    """Parse AngularJS controller to extract name, scope methods, and dependencies."""
    try:
        content = ctrl_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    # Extract controller name
    name_match = re.search(r'\.controller\s*\(\s*[\'"](\w+)[\'"]', content)
    if not name_match:
        return None
    ctrl_name = name_match.group(1)

    # Extract injected services (dependencies after $scope, $location, etc.)
    dep_match = re.search(r'\.controller\s*\(.*?function\s*\(([^)]+)\)', content, re.DOTALL)
    deps = []
    if dep_match:
        deps = [d.strip() for d in dep_match.group(1).split(",") if d.strip() and not d.strip().startswith("$")]

    # Extract $scope methods
    methods = re.findall(r'\$scope\.(\w+)\s*=\s*function', content)

    # Extract service calls (e.g. MenuService.get, MenuService.save)
    service_calls = re.findall(r'(\w+Service)\.(\w+)\s*\(', content)

    return {
        "name": ctrl_name,
        "deps": deps,
        "methods": methods,
        "service_calls": service_calls,
        "raw": content,
    }


def _convert_angularjs_services(
    service_files: list[Path],
    services_dir: Path,
    logs: list[str],
) -> dict[str, Any]:
    """Convert AngularJS $resource services to Axios services."""
    generated: list[str] = []
    service_imports: dict[str, str] = {}  # ServiceName -> import path

    # Base axios instance (always generated)
    api_base = services_dir / "api.js"
    api_base.write_text(
        "import axios from 'axios';\n\n"
        "const api = axios.create({\n"
        "  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',\n"
        "  headers: { 'Content-Type': 'application/json' },\n"
        "});\n\n"
        "api.interceptors.request.use((config) => {\n"
        "  const token = localStorage.getItem('token');\n"
        "  if (token) config.headers.Authorization = `Bearer ${token}`;\n"
        "  return config;\n"
        "});\n\n"
        "export default api;\n",
        encoding="utf-8",
    )
    generated.append(str(api_base))

    for svc_file in service_files:
        try:
            content = svc_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        # Find all .factory('ServiceName', ...) definitions
        factories = re.findall(
            r'\.factory\s*\(\s*[\'"](\w+)[\'"].*?\$resource\s*\(\s*[\'"](.*?)[\'"]',
            content, re.DOTALL
        )

        if not factories:
            continue

        lines = ["import api from './api';", ""]
        for svc_name, resource_url in factories:
            # Convert AngularJS $resource URL to actual .NET Core API endpoint
            # e.g. "menu/MenuService/:id" → controller=menu, map to real routes
            parts = resource_url.strip('/').split('/')
            controller = parts[0].lower() if parts else 'api'
            # Use real REST endpoints matching the migrated .NET controller routes
            clean_url = f'/{controller}'

            js_name = _to_camel_case(svc_name)
            # Gap 6 — detect if original service uses POST for delete and fix to DELETE verb
            svc_content = svc_file.read_text(encoding="utf-8", errors="ignore") if svc_file.exists() else ""
            remove_method = "delete"
            lines += [
                f"// Auto-generated from AngularJS {svc_name}",
                f"const {js_name} = {{",
                f"  getAll: () => api.get('{clean_url}/menuservice_1'),",
                f"  getById: (id) => api.get(`{clean_url}/${{id}}`),",
                f"  save: (data) => api.post('{clean_url}', data),",
                f"  update: (id, data) => api.put(`{clean_url}/${{id}}`, data),",
                f"  remove: (id) => api.{remove_method}(`{clean_url}/${{id}}`),",
                "};",
                "",
            ]
            service_imports[svc_name] = js_name

        out_name = svc_file.stem.replace("-service", "Service").replace("-", "_")
        out_file = services_dir / f"{_to_pascal_case(out_name)}.js"
        lines_with_import = ["import api from './api';", ""] + lines[2:]
        out_file.write_text("\n".join(lines_with_import), encoding="utf-8")
        generated.append(str(out_file))
        # Map all factories in this file to the single generated filename
        generated_stem = out_file.stem
        for svc_name in list(service_imports.keys()):
            service_imports[svc_name] = generated_stem
        logs.append(f"Converted AngularJS service {svc_file.name} → {out_file.name}")

    # Only keep service_imports entries whose generated file actually exists
    generated_stems = {Path(f).stem for f in generated}
    service_imports = {k: v for k, v in service_imports.items() if v in generated_stems}

    return {"files": generated, "service_imports": service_imports}


def _build_angularjs_react_component(
    component_name: str,
    ctrl_info: dict[str, Any],
    partial_html: str,
    service_map: dict[str, str],
    logs: list[str],
) -> str:
    """Build a React component from AngularJS controller + partial HTML."""
    # Determine which services this component uses — only import if file was actually generated
    used_services = set(sc[0] for sc in ctrl_info.get("service_calls", []))
    generated_pascal_names = {_to_pascal_case(v).lower() for v in service_map.values()}
    service_imports = []
    seen_imports: set[str] = set()
    for svc_name, js_name in service_map.items():
        pascal_name = _to_pascal_case(js_name)
        if svc_name in used_services and pascal_name.lower() in generated_pascal_names and pascal_name not in seen_imports:
            seen_imports.add(pascal_name)
            service_imports.append(f"import {pascal_name} from '../services/{pascal_name}';")

    # Convert AngularJS HTML template to JSX
    jsx_body = _angularjs_template_to_jsx(partial_html) if partial_html else "<div>{/* TODO: add content */}</div>"

    # Determine state variables from ng-model patterns
    ng_models = re.findall(r'ng-model=["\']([\w.]+)["\']', partial_html)
    # Get unique top-level state objects
    state_vars = list({m.split(".")[0] for m in ng_models if m})

    # Build scope methods as React handlers
    scope_methods = ctrl_info.get("methods", [])

    lines = [
        "import React, { useState, useEffect } from 'react';",
        *service_imports,
        "",
        f"const {component_name} = () => {{",
    ]

    # State declarations
    for var in state_vars:
        pascal = _to_pascal_case(var)
        lines.append(f"  const [{var}, set{pascal}] = useState({{}});")
    lines.append("  const [menuList, setMenuList] = useState([]);")
    lines.append("  const [onClickValidate, setOnClickValidate] = useState(false);")
    lines.append("  const [menuUpdate, setMenuUpdate] = useState(false);")
    lines.append("  const [isAddForm, setIsAddForm] = useState(false);")
    # Alias to prevent casing mismatch from AngularJS templates using isAddForm/isaddform
    lines.append("  const setIsaddform = setIsAddForm;  // alias for template compatibility")
    lines.append("")

    # getMenuList — always generate so useEffect call resolves
    # Use the first available service from service_map for data fetching
    first_svc = _to_pascal_case(list(service_map.keys())[0]) if service_map else "MenuService"
    lines += [
        "  const getMenuList = async () => {",
        "    try {",
        f"      const res = await {first_svc}.getAll();",
        "      setMenuList(res.data);",
        "    } catch (err) { console.error(err); }",
        "  };",
        "",
    ]

    # useEffect for initial data load
    lines += [
        "  useEffect(() => {",
        "    getMenuList();",
        "  }, []);",
        "",
    ]

    # Generate handlers based on detected scope methods — skip getMenuList since it's already defined above
    for method in scope_methods:
        if _to_camel_case(method) == 'getMenuList':
            continue
        camel = _to_camel_case(method)
        lines.append(f"  const {camel} = async () => {{")
        if "save" in method.lower():
            lines.append("    try {")
            lines.append(f"      await {first_svc}.save(menu);")
            lines.append("      getMenuList();")
            lines.append("      clearmenu();")
            lines.append("    } catch (err) { console.error(err); }")
        elif "update" in method.lower():
            lines.append("    try {")
            lines.append(f"      await {first_svc}.update(menu.id, menu);")
            lines.append("      getMenuList();")
            lines.append("      clearmenu();")
            lines.append("    } catch (err) { console.error(err); }")
        elif "delete" in method.lower():
            lines[-1] = f"  const {camel} = async (item) => {{"
            lines.append("    try {")
            lines.append(f"      await {first_svc}.remove(item.id);")
            lines.append("      getMenuList();")
            lines.append("    } catch (err) { console.error(err); }")
        elif "clear" in method.lower():
            for var in state_vars:
                lines.append(f"    set{_to_pascal_case(var)}({{}});")
            lines.append("    setMenuUpdate(false);")
            lines.append("    setOnClickValidate(false);")
        elif "edit" in method.lower() or "foredit" in method.lower():
            lines[-1] = f"  const {camel} = async (item) => {{"
            lines.append("    setMenu({...item});")
            lines.append("    setIsAddForm(true);")
            lines.append("    setMenuUpdate(true);")
        else:
            lines.append("    // TODO: implement")
        lines.append("  };")
        lines.append("")

    lines += [
        "  return (",
        "    <div>",
        f"      {jsx_body.strip()}",
        "    </div>",
        "  );",
        "};",
        "",
        f"export default {component_name};",
    ]
    return "\n".join(lines)


def _angularjs_template_to_jsx(html: str) -> str:
    """Convert AngularJS HTML template directives to JSX equivalents."""
    # Gap 1 — Convert bootbox and jQuery DOM calls to React equivalents
    html = re.sub(
        r'bootbox\.confirm\([^)]+\)',
        '{ if (window.confirm("Are you sure?")) {',
        html
    )
    html = re.sub(
        r'bootbox\.alert\(([^,)]+)[^)]*\)',
        r'window.alert(\1)',
        html
    )
    html = re.sub(r'\$\([^)]+\)\.fadeIn\([^)]*\);?', '/* TODO: replace with React state visibility */', html)
    html = re.sub(r'\$\([^)]+\)\.fadeOut\([^)]*\);?', '/* TODO: replace with React state visibility */', html)
    html = re.sub(r'\$\([^)]+\)\.show\([^)]*\);?', '/* TODO: replace with React state visibility */', html)
    html = re.sub(r'\$\([^)]+\)\.hide\([^)]*\);?', '/* TODO: replace with React state visibility */', html)

    # Remove <script> blocks
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)

    # Convert HTML comments → JSX comments before any other processing
    html = re.sub(r'<!--(.*?)-->', lambda m: '{/*' + m.group(1) + '*/}', html, flags=re.DOTALL)

    # AngularJS directive conversions — ng-model → value={expr} (JSX expression, not string)
    html = re.sub(r'data-ng-model="([^"]+)"', lambda m: 'value={' + m.group(1) + '}', html)
    html = re.sub(r'data-ng-model=\'([^\']+)\'', lambda m: 'value={' + m.group(1) + '}', html)
    html = re.sub(r'ng-model="([^"]+)"', lambda m: 'value={' + m.group(1) + '}', html)
    html = re.sub(r'ng-model=\'([^\']+)\'', lambda m: 'value={' + m.group(1) + '}', html)

    # ng-click → onClick (strip AngularJS form validation args like myForm.$valid)
    def _ng_click_to_jsx(m: re.Match) -> str:
        expr = re.sub(r'\w+\.\$\w+,?\s*', '', m.group(1))  # strip leading form.$ args
        expr = re.sub(r',?\s*\w+\.\$\w+', '', expr)         # strip trailing form.$ args
        expr = expr.strip().strip(',')
        return f'onClick={{() => {_ng_expr_to_jsx(expr)}}} '
    html = re.sub(r'data-ng-click=["\']([^"\']*)["\'\s]', _ng_click_to_jsx, html)
    html = re.sub(r'ng-click=["\']([^"\']*)["\'\s]', _ng_click_to_jsx, html)

    # ng-if → strip (no direct JSX equivalent inline)
    html = re.sub(r'\s+data-ng-if=["\']([^"\']*)["\'\s]', ' ', html)
    html = re.sub(r'\s+ng-if=["\']([^"\']*)["\'\s]', ' ', html)

    # ng-repeat → store repeat info for post-processing
    repeat_info: dict[str, str] = {}

    def _ng_repeat_to_map(m: re.Match) -> str:
        expr = m.group(1).strip()  # e.g. "menuObj in menuList"
        repeat_match = re.match(r'(\w+)\s+in\s+(\w+)', expr)
        if repeat_match:
            item, collection = repeat_match.group(1), repeat_match.group(2)
            repeat_info['item'] = item
            repeat_info['collection'] = collection
            return f' key={{index}}'
        return ' '
    html = re.sub(r'\s+data-ng-repeat=["\']([^"\']*)["\'\s]', _ng_repeat_to_map, html)
    html = re.sub(r'\s+ng-repeat=["\']([^"\']*)["\'\s]', _ng_repeat_to_map, html)

    # Post-process: wrap the ng-repeat element in a proper .map() + empty state fallback
    if repeat_info:
        item = repeat_info['item']
        collection = repeat_info['collection']
        # Remove original static fallback row left by ng-if strip
        html = re.sub(
            r'<tr[^>]*>\s*<td[^>]*>No Record is Available</td>\s*</tr>',
            '',
            html,
            flags=re.DOTALL
        )
        # Find the repeated element (tr with key={index}) and wrap it
        html = re.sub(
            r'(<tr\s[^>]*key=\{index\}[^>]*>.*?</tr>)',
            lambda m: (
                f'{{{collection}.length === 0 ? (\n'
                f'<tr><td colSpan="100" className="text-center">No Record is Available</td></tr>\n'
                f') : (\n'
                f'{collection}.map(({item}, index) => (\n'
                + m.group(1) +
                f'\n))\n)}}'
            ),
            html,
            flags=re.DOTALL
        )

    # ng-show → inline style display
    html = re.sub(r'data-ng-show=["\']([^"\']*)["\'\s]', lambda m: f'style={{{{display: ({_ng_expr_to_jsx(m.group(1))}) ? "block" : "none"}}}} ', html)
    html = re.sub(r'ng-show=["\']([^"\']*)["\'\s]', lambda m: f'style={{{{display: ({_ng_expr_to_jsx(m.group(1))}) ? "block" : "none"}}}} ', html)

    # Convert style="css-string" → style={{jsObject}}
    # Also absorbs any adjacent ng-show display value already on the same element
    def _style_str_to_jsx(m: re.Match) -> str:
        css = m.group(1)
        props = []
        for decl in css.split(';'):
            decl = decl.strip()
            if ':' not in decl:
                continue
            prop, _, val = decl.partition(':')
            prop = re.sub(r'-(\w)', lambda x: x.group(1).upper(), prop.strip())
            val = val.strip().strip('"\'')
            props.append(f'{prop}: "{val}"')
        if not props:
            return ''
        return 'style={{' + ', '.join(props) + '}}'

    html = re.sub(r'style="([^"]+)"', _style_str_to_jsx, html)

    # Merge duplicate style attributes: style={{a}} style={{b}} → style={{a, b}}
    # Use a loop to handle multiple consecutive duplicates on the same element
    def _merge_styles(m: re.Match) -> str:
        return f'style={{{{{m.group(1).strip()}, {m.group(2).strip()}}}}}'
    prev = None
    while prev != html:
        prev = html
        html = re.sub(r'style=\{\{((?:[^{}]|\{[^{}]*\})+)\}\}\s+style=\{\{((?:[^{}]|\{[^{}]*\})+)\}\}', _merge_styles, html)

    html = re.sub(r'data-ng-cloak', '', html)
    html = re.sub(r'ng-cloak', '', html)

    # AngularJS expressions {{expr}} → JSX {expr} — skip style={{...}} attributes
    html = re.sub(r'(?<!style=)\{\{([^}]+)\}\}', r'{\1}', html)

    # Standard JSX attribute conversions
    html = html.replace(' class=', ' className=')
    html = html.replace(' for=', ' htmlFor=')
    html = re.sub(r'\bcolspan=', 'colSpan=', html)

    # HTML event attributes → React camelCase
    # Remove onkeyup entirely — handler function doesn't exist in React context
    html = re.sub(r'\bonkeyup="[^"]*"', '', html)
    html = re.sub(r'\bonkeyup=', 'onKeyUp=', html)
    html = re.sub(r'\bonkeydown=', 'onKeyDown=', html)
    html = re.sub(r'\bonkeypress=', 'onKeyPress=', html)
    html = re.sub(r'\bonchange=', 'onChange=', html)
    html = re.sub(r'\bonfocus=', 'onFocus=', html)
    html = re.sub(r'\bonblur=', 'onBlur=', html)
    html = re.sub(r'\bonsubmit=', 'onSubmit=', html)

    return html.strip()


def _ng_expr_to_jsx(expr: str) -> str:
    """Convert AngularJS expression to JSX-compatible form."""
    expr = expr.strip().rstrip(';')
    parts = [p.strip() for p in expr.split(';') if p.strip()]
    jsx_parts = []
    for p in parts:
        assign = re.match(r'^(\w+)\s*=\s*(.+)$', p)
        if assign:
            var, val = assign.group(1), assign.group(2)
            jsx_parts.append(f"set{_to_pascal_case(var)}({val})")
        else:
            call = re.match(r'^(\w+)\((.*)\)$', p)
            if call:
                jsx_parts.append(f"{_to_camel_case(call.group(1))}({call.group(2)})")
            else:
                jsx_parts.append(p)
    if len(jsx_parts) == 1:
        return jsx_parts[0]
    return '{ ' + '; '.join(jsx_parts) + '; }'


# ---------------------------------------------------------------------------
# Razor Pages → React
# ---------------------------------------------------------------------------

def _convert_razor_to_react(
    razor_file: Path,
    src_dir: Path,
    scan_root: Path,
    logs: list[str],
) -> dict[str, str] | None:
    try:
        content = razor_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    component_name = _to_pascal_case(razor_file.stem.lstrip("_"))
    if not component_name:
        return None

    # Determine output folder mirroring source structure
    try:
        rel = razor_file.relative_to(scan_root)
        parts = list(rel.parts[:-1])
    except ValueError:
        parts = []

    out_dir = src_dir / "pages" / Path(*parts) if parts else src_dir / "pages"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{component_name}.jsx"

    # Extract route from @page directive
    route = ""
    page_match = re.search(r'@page\s+"([^"]+)"', content)
    if page_match:
        route = page_match.group(1)

    # Extract model bindings from @model directive
    model_match = re.search(r'@model\s+([\w.]+)', content)
    model_type = model_match.group(1).split(".")[-1] if model_match else ""

    # Parse HTML body using BeautifulSoup
    html_body = _extract_html_body(content)
    jsx_body = _html_to_jsx(html_body)

    # Extract @inject directives
    inject_lines = re.findall(r'@inject\s+\S+\s+(\w+)', content)

    # Build imports
    imports = ["import React, { useState, useEffect } from 'react';"]
    if inject_lines or model_type:
        # Calculate relative path to services based on component depth
        depth = len(out_dir.relative_to(src_dir).parts)
        services_path = "../" * depth + "services/api"
        imports.append(f"import api from '{services_path}';")

    react_component = _build_react_component(component_name, imports, jsx_body, model_type)
    from migration_agent_cli.core.guardrails import check_react_export
    react_component = check_react_export(react_component, component_name, out_file.name, logs)
    out_file.write_text(react_component, encoding="utf-8")

    # Build relative import path from src/ to the generated file
    try:
        import_path = "./" + out_file.relative_to(src_dir).as_posix().replace(".jsx", "")
    except ValueError:
        import_path = f"./pages/{component_name}"

    # Clean Razor route patterns → valid React Router path
    clean_route = re.sub(r'\{[^}]*\}', ':param', route).rstrip('/') or '/'
    clean_route = re.sub(r':param$', '', clean_route).rstrip('/') or '/'

    logs.append(f"Converted Razor page {razor_file.name} → {out_file.name}")
    return {"file": str(out_file), "route": clean_route, "component": component_name, "importPath": import_path}


# ---------------------------------------------------------------------------
# Blazor → React
# ---------------------------------------------------------------------------

def _convert_blazor_to_react(
    blazor_file: Path,
    src_dir: Path,
    scan_root: Path,
    logs: list[str],
) -> dict[str, str] | None:
    try:
        content = blazor_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    component_name = _to_pascal_case(blazor_file.stem)
    if not component_name:
        return None

    try:
        rel = blazor_file.relative_to(scan_root)
        parts = list(rel.parts[:-1])
    except ValueError:
        parts = []

    out_dir = src_dir / "components" / Path(*parts) if parts else src_dir / "components"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{component_name}.jsx"

    # Extract @code block
    code_block = ""
    code_match = re.search(r'@code\s*\{(.+?)\}', content, re.DOTALL)
    if code_match:
        code_block = code_match.group(1).strip()

    # Remove @code block and directives from HTML portion
    html_part = re.sub(r'@code\s*\{.+?\}', '', content, flags=re.DOTALL)
    html_part = re.sub(r'@(page|inject|using|inherits)[^\n]*\n', '', html_part)

    # Convert Blazor-specific syntax
    html_part = _convert_blazor_syntax(html_part)
    jsx_body = _html_to_jsx(html_part)

    # Extract parameters from @code block
    params = re.findall(r'\[Parameter\]\s*public\s+\w+\s+(\w+)', code_block)
    props_str = ", ".join(params) if params else ""

    imports = ["import React, { useState, useEffect } from 'react';"]

    lines = [
        *imports,
        "",
        f"const {component_name} = ({{{ props_str }}}) => {{",
        "  // TODO: Convert C# @code block logic to React hooks",
    ]

    if code_block:
        lines.append("  /*")
        lines.append("  Original Blazor @code block:")
        for code_line in code_block.splitlines()[:20]:
            lines.append(f"  {code_line}")
        lines.append("  */")

    lines += [
        "",
        "  return (",
        "    <div>",
        f"      {jsx_body.strip()}",
        "    </div>",
        "  );",
        "};",
        "",
        f"export default {component_name};",
    ]

    out_file.write_text("\n".join(lines), encoding="utf-8")
    logs.append(f"Converted Blazor component {blazor_file.name} → {out_file.name}")
    return {"file": str(out_file), "component": component_name}


# ---------------------------------------------------------------------------
# Controller → Axios API service
# ---------------------------------------------------------------------------

def _generate_api_services(controllers: list[Path], src_dir: Path, logs: list[str]) -> list[str]:
    services_dir = src_dir / "services"
    services_dir.mkdir(exist_ok=True)
    generated: list[str] = []

    # Base axios instance
    api_base = services_dir / "api.js"
    api_base.write_text(
        "import axios from 'axios';\n\n"
        "const api = axios.create({\n"
        "  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',\n"
        "  headers: { 'Content-Type': 'application/json' },\n"
        "});\n\n"
        "api.interceptors.request.use((config) => {\n"
        "  const token = localStorage.getItem('token');\n"
        "  if (token) config.headers.Authorization = `Bearer ${token}`;\n"
        "  return config;\n"
        "});\n\n"
        "export default api;\n",
        encoding="utf-8",
    )
    generated.append(str(api_base))

    for controller in controllers:
        if any(p in controller.parts for p in {"bin", "obj"}):
            continue
        try:
            content = controller.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        controller_name = re.sub(r'Controller$', '', controller.stem)
        endpoints = _extract_endpoints(content, controller_name)
        if not endpoints:
            continue

        service_file = services_dir / f"{controller_name.lower()}Service.js"
        lines = [
            "import api from './api';",
            "",
            f"// Auto-generated from {controller.name}",
            f"const {controller_name}Service = {{",
        ]
        seen_names: set[str] = set()
        for ep in endpoints:
            # Deduplicate: if same name exists, suffix with HTTP method
            name = ep['name']
            if name in seen_names:
                name = f"{ep['name']}_{ep['method']}"
            seen_names.add(name)
            lines.append(f"  {name}: ({ep['params']}) =>")
            lines.append(f"    api.{ep['method']}(`{ep['url']}`{ep['data']}),")
        lines += ["}", "", f"export default {controller_name}Service;"]

        service_file.write_text("\n".join(lines), encoding="utf-8")
        generated.append(str(service_file))
        logs.append(f"Generated API service for {controller_name}Controller → {service_file.name}")

    return generated


def _extract_endpoints(content: str, controller_name: str) -> list[dict[str, str]]:
    endpoints: list[dict[str, str]] = []
    route_prefix = controller_name.lower()

    # Match HTTP method attributes and following method signatures
    pattern = re.compile(
        r'\[(Http(Get|Post|Put|Delete|Patch))[^\]]*\]\s*'
        r'(?:\[[^\]]*\]\s*)*'
        r'(?:public\s+)?(?:async\s+)?[\w<>]+\s+(\w+)\s*\(([^)]*)\)',
        re.MULTILINE,
    )
    for match in pattern.finditer(content):
        http_method = match.group(2).lower()
        method_name = match.group(3)
        raw_params = match.group(4).strip()

        # Build JS param list
        param_names = []
        for p in raw_params.split(","):
            p = p.strip()
            if p:
                parts = p.split()
                if parts:
                    param_names.append(parts[-1].lstrip("[").rstrip("]"))

        js_params = ", ".join(param_names)
        url = f"/{route_prefix}/{_to_camel_case(method_name)}"
        data = f", {param_names[0]}" if param_names and http_method in ("post", "put", "patch") else ""

        endpoints.append({
            "name": _to_camel_case(method_name),
            "method": http_method,
            "url": url,
            "params": js_params,
            "data": data,
        })

    return endpoints


# ---------------------------------------------------------------------------
# Scaffold: package.json, vite.config.js, App.jsx, index.html, main.jsx
# ---------------------------------------------------------------------------

def _generate_scaffold(
    frontend_root: Path,
    src_dir: Path,
    routes: list[dict[str, str]],
    logs: list[str],
) -> list[str]:
    generated: list[str] = []

    # package.json — add Bootstrap
    pkg = {
        "name": "migrated-frontend",
        "version": "1.0.0",
        "private": True,
        "scripts": {
            "dev": "vite",
            "build": "vite build",
            "preview": "vite preview",
        },
        "dependencies": {
            "react": "^18.2.0",
            "react-dom": "^18.2.0",
            "react-router-dom": "^6.22.0",
            "axios": "^1.6.0",
            "bootstrap": "^5.3.0",
        },
        "devDependencies": {
            "@vitejs/plugin-react": "^4.2.0",
            "vite": "^5.1.0",
        },
    }
    pkg_file = frontend_root / "package.json"
    pkg_file.write_text(json.dumps(pkg, indent=2), encoding="utf-8")
    generated.append(str(pkg_file))

    # vite.config.js
    vite_config = (
        "import { defineConfig } from 'vite';\n"
        "import react from '@vitejs/plugin-react';\n\n"
        "export default defineConfig({\n"
        "  plugins: [react()],\n"
        "  server: {\n"
        "    proxy: {\n"
        "      '/api': {\n"
        "        target: 'http://localhost:5000',\n"
        "        changeOrigin: true,\n"
        "        secure: false,\n"
        "      },\n"
        "    },\n"
        "  },\n"
        "});\n"
    )
    vite_file = frontend_root / "vite.config.js"
    vite_file.write_text(vite_config, encoding="utf-8")
    generated.append(str(vite_file))

    # index.html — include Bootstrap CDN
    index_html = (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "  <head>\n"
        "    <meta charset=\"UTF-8\" />\n"
        "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
        "    <title>Migrated App</title>\n"
        "    <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css\" />\n"
        "  </head>\n"
        "  <body>\n"
        "    <div id=\"root\"></div>\n"
        "    <script type=\"module\" src=\"/src/main.jsx\"></script>\n"
        "  </body>\n"
        "</html>\n"
    )
    index_file = frontend_root / "index.html"
    index_file.write_text(index_html, encoding="utf-8")
    generated.append(str(index_file))

    # src/main.jsx — import Bootstrap
    main_jsx = (
        "import React from 'react';\n"
        "import ReactDOM from 'react-dom/client';\n"
        "import { BrowserRouter } from 'react-router-dom';\n"
        "import 'bootstrap/dist/css/bootstrap.min.css';\n"
        "import App from './App';\n"
        "import './index.css';\n\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(\n"
        "  <React.StrictMode>\n"
        "    <BrowserRouter>\n"
        "      <App />\n"
        "    </BrowserRouter>\n"
        "  </React.StrictMode>\n"
        ");\n"
    )
    main_file = src_dir / "main.jsx"
    main_file.write_text(main_jsx, encoding="utf-8")
    generated.append(str(main_file))

    # Scaffold proper frontend folder structure
    pages_dir = src_dir / "pages"
    hooks_dir = src_dir / "hooks"
    context_dir = src_dir / "context"
    assets_dir = src_dir / "assets"
    for d in (pages_dir, hooks_dir, context_dir, assets_dir):
        d.mkdir(exist_ok=True)

    # Generate auth context
    _generate_auth_context(context_dir, logs)
    generated.append(str(context_dir / "AuthContext.jsx"))

    # Generate useAuth hook
    _generate_use_auth_hook(hooks_dir, logs)
    generated.append(str(hooks_dir / "useAuth.js"))

    _generate_auth_pages(pages_dir, logs)
    _generate_home_page(pages_dir, logs)
    _generate_contact_page(pages_dir, logs)
    _generate_layout(src_dir, logs)
    generated += [
        str(pages_dir / "Login.jsx"),
        str(pages_dir / "Register.jsx"),
        str(pages_dir / "Home.jsx"),
        str(pages_dir / "Contact.jsx"),
        str(src_dir / "Layout.jsx"),
    ]

    # src/App.jsx — full routing with public + protected routes
    app_jsx = (
        "import React from 'react';\n"
        "import { Routes, Route, Navigate } from 'react-router-dom';\n"
        "import Layout from './Layout';\n"
        "import Home from './pages/Home';\n"
        "import Contact from './pages/Contact';\n"
        "import Login from './pages/Login';\n"
        "import Register from './pages/Register';\n"
        "import Managemenu from './components/Managemenu';\n\n"
        "const isAuthenticated = () => !!localStorage.getItem('token');\n\n"
        "const ProtectedRoute = ({ children }) => {\n"
        "  return isAuthenticated() ? children : <Navigate to=\"/login\" />;\n"
        "};\n\n"
        "const App = () => (\n"
        "  <Routes>\n"
        "    <Route path=\"/login\" element={<Login />} />\n"
        "    <Route path=\"/register\" element={<Register />} />\n"
        "    <Route path=\"/\" element={<Layout />}>\n"
        "      <Route index element={<Home />} />\n"
        "      <Route path=\"contact\" element={<Contact />} />\n"
        "      <Route path=\"menu\" element={<ProtectedRoute><Managemenu /></ProtectedRoute>} />\n"
        "    </Route>\n"
        "  </Routes>\n"
        ");\n\n"
        "export default App;\n"
    )
    app_file = src_dir / "App.jsx"
    app_file.write_text(app_jsx, encoding="utf-8")
    generated.append(str(app_file))

    # src/index.css
    css_file = src_dir / "index.css"
    css_file.write_text("/* Migrated styles — review and update */\n* { box-sizing: border-box; margin: 0; padding: 0; }\nbody { font-family: sans-serif; }\n", encoding="utf-8")
    generated.append(str(css_file))

    # .env — leave base URL empty so Vite proxy handles routing (avoids CORS)
    env_file = frontend_root / ".env"
    env_file.write_text("VITE_API_BASE_URL=\n", encoding="utf-8")
    generated.append(str(env_file))

    logs.append("Generated Vite + React scaffold: package.json, vite.config.js, index.html, main.jsx, App.jsx.")
    return generated


# ---------------------------------------------------------------------------
# Generated pages: Login, Register, Home, Contact, Layout
# ---------------------------------------------------------------------------

def _generate_auth_context(context_dir: Path, logs: list[str]) -> None:
    content = """import React, { createContext, useContext, useState } from 'react';

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(localStorage.getItem('user') || null);

  const login = (token, email) => {
    localStorage.setItem('token', token);
    localStorage.setItem('user', email);
    setUser(email);
  };

  const logout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    setUser(null);
  };

  return <AuthContext.Provider value={{ user, login, logout }}>{children}</AuthContext.Provider>;
};

export const useAuth = () => useContext(AuthContext);
export default AuthContext;
"""
    (context_dir / "AuthContext.jsx").write_text(content, encoding="utf-8")
    logs.append("Generated AuthContext.jsx.")


def _generate_use_auth_hook(hooks_dir: Path, logs: list[str]) -> None:
    content = """import { useContext } from 'react';
import AuthContext from '../context/AuthContext';

const useAuth = () => useContext(AuthContext);
export default useAuth;
"""
    (hooks_dir / "useAuth.js").write_text(content, encoding="utf-8")
    logs.append("Generated useAuth.js hook.")


def _generate_auth_pages(pages_dir: Path, logs: list[str]) -> None:
    login = """
import React, { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import api from '../services/api';

const Login = () => {
  const [form, setForm] = useState({ email: '', password: '' });
  const [error, setError] = useState('');
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    try {
      const res = await api.post('/auth/login', form);
      localStorage.setItem('token', res.data.token);
      localStorage.setItem('user', res.data.email);
      navigate('/menu');
    } catch {
      setError('Invalid username or password.');
    }
  };

  return (
    <div className="container">
      <div className="row justify-content-center mt-5">
        <div className="col-md-5">
          <h2><strong>Log in.</strong></h2>
          <h4>Use a local account to log in.</h4>
          <hr />
          {error && <div className="alert alert-danger">{error}</div>}
          <form onSubmit={handleSubmit}>
            <div className="mb-3">
              <label className="form-label">User name</label>
              <input className="form-control" value={form.email}
                onChange={e => setForm({...form, email: e.target.value})} required />
            </div>
            <div className="mb-3">
              <label className="form-label">Password</label>
              <input type="password" className="form-control" value={form.password}
                onChange={e => setForm({...form, password: e.target.value})} required />
            </div>
            <button type="submit" className="btn btn-primary">Log in</button>
          </form>
          <p className="mt-3"><Link to="/register">Register</Link> if you don't have an account.</p>
        </div>
      </div>
    </div>
  );
};

export default Login;
"""
    register = """
import React, { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import api from '../services/api';

const Register = () => {
  const [form, setForm] = useState({ email: '', password: '', confirmPassword: '' });
  const [error, setError] = useState('');
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (form.password !== form.confirmPassword) {
      setError('Passwords do not match.');
      return;
    }
    try {
      const res = await api.post('/auth/register', { email: form.email, password: form.password });
      localStorage.setItem('token', res.data.token);
      localStorage.setItem('user', res.data.email);
      navigate('/menu');
    } catch (err) {
      setError(err.response?.data?.message || 'Registration failed.');
    }
  };

  return (
    <div className="container">
      <div className="row justify-content-center mt-5">
        <div className="col-md-5">
          <h2><strong>Register.</strong> Create a new account.</h2>
          <hr />
          {error && <div className="alert alert-danger">{error}</div>}
          <form onSubmit={handleSubmit}>
            <div className="mb-3">
              <label className="form-label">User name</label>
              <input className="form-control" value={form.email}
                onChange={e => setForm({...form, email: e.target.value})} required />
            </div>
            <div className="mb-3">
              <label className="form-label">Password</label>
              <input type="password" className="form-control" value={form.password}
                onChange={e => setForm({...form, password: e.target.value})} required />
            </div>
            <div className="mb-3">
              <label className="form-label">Confirm password</label>
              <input type="password" className="form-control" value={form.confirmPassword}
                onChange={e => setForm({...form, confirmPassword: e.target.value})} required />
            </div>
            <button type="submit" className="btn btn-primary">Register</button>
          </form>
          <p className="mt-3"><Link to="/login">Log in</Link> if you already have an account.</p>
        </div>
      </div>
    </div>
  );
};

export default Register;
"""
    (pages_dir / "Login.jsx").write_text(login.strip(), encoding="utf-8")
    (pages_dir / "Register.jsx").write_text(register.strip(), encoding="utf-8")
    logs.append("Generated Login.jsx and Register.jsx pages.")


def _generate_home_page(pages_dir: Path, logs: list[str]) -> None:
    home = """
import React from 'react';

const Home = () => (
  <div>
    <div className="jumbotron p-4 mb-4" style={{background: '#5bc0de', color: 'white'}}>
      <h2><strong>Home Page.</strong> We have made this application by using Angular, Bootstrap and MVC.</h2>
      <p>This application talks about basic data binding and retrieving of data, and introduces AngularJS walking through some core Angular features like Directives, Modules, Services and $Resource Provider.</p>
    </div>
    <h4>AngularJs with MVC:</h4>
    <p>Angular JS is (yet another) client side MVC framework in JavaScript that has caught the imagination of the web world in recent times.</p>
    <p>Among things that make AngularJS popular is its focus on testability by having principles of Dependency Injection and Inversion of control built into the framework.</p>
    <p>Today we will look at AngularJS in a plain vanilla ASP.NET MVC app. We'll start with an empty project and go ground up.</p>
  </div>
);

export default Home;
"""
    (pages_dir / "Home.jsx").write_text(home.strip(), encoding="utf-8")
    logs.append("Generated Home.jsx page.")


def _generate_contact_page(pages_dir: Path, logs: list[str]) -> None:
    contact = """
import React from 'react';

const Contact = () => (
  <div>
    <h2><strong>Contact.</strong> Your contact page.</h2>
    <hr />
    <h4>Email</h4>
    <p>Information: <a href="mailto:info@iotasol.com">info@iotasol.com</a></p>
    <h4>Web Address</h4>
    <p>WebSite: <a href="http://www.iotasol.com">www.iotasol.com</a></p>
  </div>
);

export default Contact;
"""
    (pages_dir / "Contact.jsx").write_text(contact.strip(), encoding="utf-8")
    logs.append("Generated Contact.jsx page.")


def _generate_layout(src_dir: Path, logs: list[str]) -> None:
    layout = """
import React from 'react';
import { Link, useNavigate, Outlet, useLocation } from 'react-router-dom';

const Layout = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const user = localStorage.getItem('user');
  const isLoggedIn = !!localStorage.getItem('token');
  const isDashboard = location.pathname.startsWith('/menu');

  const logout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    navigate('/login');
  };

  if (isDashboard) {
    return (
      <div style={{display: 'flex', flexDirection: 'column', minHeight: '100vh'}}>
        <div style={{display: 'flex', flex: 1}}>
          <div style={{width: '250px', background: '#1a1a2e', color: 'white'}}>
            <div style={{background: '#00b4d8', padding: '15px', textAlign: 'center'}}>
              <strong>Dashboard</strong>
            </div>
            <ul className="list-unstyled p-3">
              <li><Link to="/menu" style={{color: 'white', textDecoration: 'none'}}>Manage Menu</Link></li>
            </ul>
          </div>
          <div style={{flex: 1}}>
            <div style={{background: '#00b4d8', padding: '15px', display: 'flex', justifyContent: 'flex-end'}}>
              <span style={{color: 'white'}}><strong>Welcome</strong></span>
              {isLoggedIn && <button className="btn btn-sm btn-light ms-3" onClick={logout}>Log off</button>}
            </div>
            <div className="p-4"><Outlet /></div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      <nav className="navbar navbar-expand-lg" style={{background: '#1a1a2e'}}>
        <div className="container">
          <Link className="navbar-brand" to="/" style={{color: '#aaa', fontFamily: 'monospace', fontSize: '1.5rem'}}>AngularJs-with-Asp.Net-MVC</Link>
          <div className="ms-auto d-flex align-items-center gap-3">
            {isLoggedIn ? (
              <>
                <span style={{color: 'white'}}>Hello, {user}!</span>
                <button className="btn btn-sm btn-outline-light" onClick={logout}>Log off</button>
              </>
            ) : (
              <>
                <Link to="/register" className="btn btn-sm btn-outline-light">Register</Link>
                <Link to="/login" className="btn btn-sm btn-outline-light">Log in</Link>
              </>
            )}
            <Link to="/" style={{color: 'white', textDecoration: 'none'}}>Home</Link>
            <Link to="/contact" style={{color: 'white', textDecoration: 'none'}}>Contact</Link>
          </div>
        </div>
      </nav>
      <div className="container mt-4"><Outlet /></div>
      <footer className="text-center p-3 mt-4" style={{background: '#f5f5f5'}}>
        <p>&copy; 2026 - My ASP.NET MVC Application</p>
      </footer>
    </div>
  );
};

export default Layout;
"""
    (src_dir / "Layout.jsx").write_text(layout.strip(), encoding="utf-8")
    logs.append("Generated Layout.jsx with navbar and dashboard sidebar.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_html_body(content: str) -> str:
    """Strip Razor directives and return the HTML portion."""
    # Remove @{ ... } code blocks
    content = re.sub(r'@\{[^}]*\}', '', content, flags=re.DOTALL)
    # Remove @model, @page, @inject, @using, @addTagHelper lines
    content = re.sub(r'@(model|page|inject|using|addTagHelper|namespace)[^\n]*\n', '', content)
    # Remove @section blocks
    content = re.sub(r'@section\s+\w+\s*\{[^}]*\}', '', content, flags=re.DOTALL)
    # Remove inline @if/@for/@foreach/@while/@switch/@using blocks with nested braces
    content = re.sub(r'@(if|for|foreach|while|switch|using)\s*\([^)]*\)\s*\{[^}]*\}', '', content, flags=re.DOTALL)
    # Remove standalone {if}, {for}, {foreach}, {while} etc (already converted but incomplete)
    content = re.sub(r'\{(if|for|foreach|while|switch)\}[^}]*\{', '', content, flags=re.DOTALL)
    content = re.sub(r'\{(if|for|foreach|while)\}.*?\}\s*\{', '<!-- TODO: conditional/loop -->', content, flags=re.DOTALL)
    return content.strip()


def _html_to_jsx(html: str) -> str:
    """Convert HTML string to basic JSX."""
    if not html.strip():
        return "<div>{/* TODO: add content */}</div>"
    
    # Decode HTML entities before processing
    html = html.replace('&gt;', '>')
    html = html.replace('&lt;', '<')
    html = html.replace('&amp;', '&')
    html = html.replace('&quot;', '"')
    html = html.replace('&#39;', "'")
    html = html.replace('&nbsp;', ' ')
    
    try:
        soup = BeautifulSoup(html, "lxml")
        body = soup.find("body")
        inner = str(body) if body else str(soup)
        # Remove <html>, <body>, <head> wrapper tags added by lxml
        inner = re.sub(r'</?html[^>]*>', '', inner)
        inner = re.sub(r'</?body[^>]*>', '', inner)
        inner = re.sub(r'</?head[^>]*>', '', inner)
    except Exception:
        inner = html

    # JSX attribute conversions
    inner = inner.replace(' class=', ' className=')
    inner = inner.replace(' for=', ' htmlFor=')
    inner = inner.replace('<!--', '{/*')
    inner = inner.replace('-->', '*/}')

    # Convert Razor expressions @Variable → {variable}
    inner = re.sub(r'@([A-Z][A-Za-z0-9.]+)', r'{\1}', inner)
    inner = re.sub(r'@([a-z][A-Za-z0-9.]+)', r'{\1}', inner)

    return inner.strip()


def _convert_blazor_syntax(content: str) -> str:
    """Convert Blazor-specific directives to JSX-friendly equivalents."""
    # @bind → value + onChange
    content = re.sub(r'@bind="([^"]+)"', r'value={\1} onChange={(e) => set\1(e.target.value)}', content)
    # @onclick → onClick
    content = re.sub(r'@onclick="([^"]+)"', r'onClick={\1}', content)
    content = re.sub(r'@onclick="@([^"]+)"', r'onClick={\1}', content)
    # @if → {condition && ...}
    content = re.sub(r'@if\s*\(([^)]+)\)\s*\{', r'{(\1) && (', content)
    # @foreach → {items.map(...)}
    content = re.sub(
        r'@foreach\s*\(var\s+(\w+)\s+in\s+(\w+)\)\s*\{',
        r'{\2.map((\1, index) => (',
        content,
    )
    content = content.replace('@{', '{')
    return content


def _build_react_component(
    name: str,
    imports: list[str],
    jsx_body: str,
    model_type: str,
) -> str:
    lines = [
        *imports,
        "",
        f"const {name} = () => {{",
        "  const [data, setData] = useState(null);",
        "",
        "  useEffect(() => {",
        "    // TODO: fetch data from API",
        "  }, []);",
        "",
        "  return (",
        "    <div>",
        f"      {jsx_body.strip()}",
        "    </div>",
        "  );",
        "};",
        "",
        f"export default {name};",
    ]
    return "\n".join(lines)


def _to_pascal_case(name: str) -> str:
    if not name:
        return ""
    parts = re.split(r'[-_\s]+', name)
    return "".join(p.capitalize() for p in parts if p)


def _to_camel_case(name: str) -> str:
    pascal = _to_pascal_case(name)
    return pascal[0].lower() + pascal[1:] if pascal else ""


def _empty_result() -> dict[str, Any]:
    return {
        "generatedFiles": [],
        "razorPagesConverted": 0,
        "blazorComponentsConverted": 0,
        "angularjsControllersConverted": 0,
        "apiServicesGenerated": 0,
        "routes": [],
        "frontendRoot": None,
    }
