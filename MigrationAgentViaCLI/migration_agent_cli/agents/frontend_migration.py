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
    description = "Migrates Razor Pages and Blazor components to React with Vite, React Router, and Axios."
    capabilities = [
        "Razor Pages → React components",
        "Blazor → React components",
        "MVC routes → React Router",
        "Controller endpoints → Axios API calls",
        "Vite + React scaffold generation",
    ]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        source = safe_source_path(context, logs)

        scan_root = Path(migrated_root) if migrated_root else source
        if not scan_root:
            logs.append("No source path available — skipping frontend migration.")
            return _empty_result()

        frontend_root = (Path(migrated_root) if migrated_root else scan_root) / "frontend"
        frontend_root.mkdir(parents=True, exist_ok=True)
        src_dir = frontend_root / "src"
        src_dir.mkdir(exist_ok=True)

        # Discover frontend files
        razor_pages = list(scan_root.rglob("*.cshtml"))
        blazor_components = list(scan_root.rglob("*.razor"))
        controllers = list(scan_root.rglob("*Controller.cs"))

        logs.append(f"Found {len(razor_pages)} Razor pages, {len(blazor_components)} Blazor components, {len(controllers)} controllers.")

        generated: list[str] = []
        routes: list[dict[str, str]] = []

        # Convert Razor Pages → React components
        for razor_file in razor_pages:
            if any(p in razor_file.parts for p in {"bin", "obj"}):
                continue
            result = _convert_razor_to_react(razor_file, src_dir, scan_root, logs)
            if result:
                generated.append(result["file"])
                if result.get("route"):
                    routes.append({"path": result["route"], "component": result["component"]})

        # Convert Blazor components → React components
        for blazor_file in blazor_components:
            if any(p in blazor_file.parts for p in {"bin", "obj"}):
                continue
            result = _convert_blazor_to_react(blazor_file, src_dir, scan_root, logs)
            if result:
                generated.append(result["file"])

        # Generate Axios API service from controllers
        api_services = _generate_api_services(controllers, src_dir, logs)
        generated.extend(api_services)

        # Generate scaffold files
        scaffold = _generate_scaffold(frontend_root, src_dir, routes, logs)
        generated.extend(scaffold)

        logs.append(f"Frontend migration complete. Generated {len(generated)} files.")
        return {
            "generatedFiles": generated,
            "razorPagesConverted": len(razor_pages),
            "blazorComponentsConverted": len(blazor_components),
            "apiServicesGenerated": len(api_services),
            "routes": routes,
            "frontendRoot": str(frontend_root),
        }


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
        imports.append("import api from '../services/api';")

    react_component = _build_react_component(component_name, imports, jsx_body, model_type)
    out_file.write_text(react_component, encoding="utf-8")

    logs.append(f"Converted Razor page {razor_file.name} → {out_file.name}")
    return {"file": str(out_file), "route": route, "component": component_name}


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
        for ep in endpoints:
            lines.append(f"  {ep['name']}: ({ep['params']}) =>")
            lines.append(f"    api.{ep['method']}(`{ep['url']}`{ep['data']}),")
        lines += ["};", "", f"export default {controller_name}Service;"]

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

    # package.json
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
        "        target: 'https://localhost:5001',\n"
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

    # index.html
    index_html = (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "  <head>\n"
        "    <meta charset=\"UTF-8\" />\n"
        "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
        "    <title>Migrated App</title>\n"
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

    # src/main.jsx
    main_jsx = (
        "import React from 'react';\n"
        "import ReactDOM from 'react-dom/client';\n"
        "import { BrowserRouter } from 'react-router-dom';\n"
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

    # src/App.jsx with React Router routes
    route_imports = "\n".join(
        f"import {r['component']} from './pages/{r['component']}';"
        for r in routes if r.get("component") and r.get("path")
    )
    route_elements = "\n".join(
        f"        <Route path=\"{r['path']}\" element={{<{r['component']} />}} />"
        for r in routes if r.get("component") and r.get("path")
    )
    app_jsx = (
        "import React from 'react';\n"
        "import { Routes, Route } from 'react-router-dom';\n"
        f"{route_imports}\n\n"
        "const App = () => (\n"
        "  <Routes>\n"
        f"{route_elements}\n"
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

    # .env
    env_file = frontend_root / ".env"
    env_file.write_text("VITE_API_BASE_URL=https://localhost:5001/api\n", encoding="utf-8")
    generated.append(str(env_file))

    logs.append("Generated Vite + React scaffold: package.json, vite.config.js, index.html, main.jsx, App.jsx.")
    return generated


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
    return content.strip()


def _html_to_jsx(html: str) -> str:
    """Convert HTML string to basic JSX."""
    if not html.strip():
        return "<div>{/* TODO: add content */}</div>"
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
    inner = re.sub(r'<!--', '{/*', inner)
    inner = re.sub(r'-->', '*/}', inner)

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
        "apiServicesGenerated": 0,
        "routes": [],
        "frontendRoot": None,
    }
