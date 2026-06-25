from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from migration_agent_cli.core.agent_base import StructuredMigrationAgent
from migration_agent_cli.core.models import AgentExecutionContext


class ApiTransformationAgent(StructuredMigrationAgent):
    agent_id = "api-transformation"
    title = "API Transformation Agent"
    description = "Converts MVC controllers to REST API controllers and removes Views for API-only projects."
    capabilities = [
        "MVC Controller → REST ApiController",
        "ActionResult → IActionResult with JSON",
        "Remove Razor Views",
        "Add Swagger/OpenAPI",
        "Add CORS configuration",
        "Update Program.cs for API mode",
    ]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        if not migrated_root:
            logs.append("No migrated source path — skipping API transformation.")
            return {"transformedControllers": [], "removedViews": [], "changedFiles": []}

        root = Path(migrated_root)
        target_framework = context.input_data.get("targetFramework", "net8.0")
        transformed_controllers: list[str] = []
        removed_views: list[str] = []
        changed_files: list[str] = []

        # Transform controllers to REST API controllers
        for cs_file in root.rglob("*Controller.cs"):
            if any(p in cs_file.parts for p in {"bin", "obj"}):
                continue
            original = cs_file.read_text(encoding="utf-8", errors="ignore")
            transformed = _transform_controller_to_api(original, cs_file.stem, logs)
            if transformed != original:
                cs_file.write_text(transformed, encoding="utf-8")
                changed_files.append(str(cs_file))
                transformed_controllers.append(cs_file.name)
                logs.append(f"Transformed {cs_file.name} → REST API controller.")

        # Remove Views folder — not needed for API
        for views_dir in root.rglob("Views"):
            if views_dir.is_dir() and not any(p in views_dir.parts for p in {"bin", "obj", "frontend"}):
                import shutil
                shutil.rmtree(views_dir)
                removed_views.append(str(views_dir))
                logs.append(f"Removed Views folder: {views_dir.name}.")

        # Update .csproj — add Swashbuckle for Swagger
        for csproj in root.rglob("*.csproj"):
            if any(p in csproj.parts for p in {"bin", "obj"}):
                continue
            original = csproj.read_text(encoding="utf-8", errors="ignore")
            updated = _add_swagger_package(original)
            if updated != original:
                csproj.write_text(updated, encoding="utf-8")
                changed_files.append(str(csproj))
                logs.append(f"Added Swashbuckle.AspNetCore to {csproj.name}.")

        # Update Program.cs for API mode
        for program_cs in root.rglob("Program.cs"):
            if any(p in program_cs.parts for p in {"bin", "obj"}):
                continue
            original = program_cs.read_text(encoding="utf-8", errors="ignore")
            updated = _update_program_cs_for_api(original, logs)
            if updated != original:
                program_cs.write_text(updated, encoding="utf-8")
                changed_files.append(str(program_cs))
                logs.append(f"Updated Program.cs for REST API mode.")

        logs.append(f"API transformation complete. Controllers: {len(transformed_controllers)}, Views removed: {len(removed_views)}.")
        return {
            "transformedControllers": transformed_controllers,
            "removedViews": removed_views,
            "changedFiles": changed_files,
        }


def _transform_controller_to_api(source: str, filename: str, logs: list[str]) -> str:
    updated = source

    # Gap 3 — Resolve duplicate method names before transformation
    updated = _resolve_duplicate_method_names(updated, logs)

    # Replace EF6 field-instantiated DbContext with DI constructor injection
    updated = _replace_ef6_context_with_di(updated, logs)

    # Detect root namespace and inject Models using so ApplicationDbContext + entity types resolve
    ns_match = re.search(r'namespace\s+([\w.]+)', updated)
    if ns_match:
        root_ns = ns_match.group(1).split(".")[0]
        models_using = f"using {root_ns}.Models;"
        if models_using not in updated:
            updated = models_using + "\n" + updated

    # Add using statements for API
    api_usings = [
        "using Microsoft.AspNetCore.Mvc;",
        "using Microsoft.AspNetCore.Cors;",
    ]
    for using in api_usings:
        if using not in updated:
            updated = using + "\n" + updated

    # Remove System.Web.Mvc using if present (already handled by code-transformation)
    updated = re.sub(r'using System\.Web\.Mvc;\n?', '', updated)

    # Add [ApiController] and [Route] attributes if not present
    if "[ApiController]" not in updated:
        updated = re.sub(
            r'(public\s+class\s+\w+Controller\s*:)',
            r'[ApiController]\n[Route("api/[controller]")]\n\1',
            updated
        )

    # Change base class from Controller to ControllerBase (API controllers don't need View support)
    updated = re.sub(
        r':\s*Controller\b',
        ': ControllerBase',
        updated
    )

    # Convert return View(model) → return Ok(model)
    updated = re.sub(r'return\s+View\(([^)]*)\);', r'return Ok(\1);', updated)

    # Convert return View() → return Ok()
    updated = re.sub(r'return\s+View\(\);', r'return Ok();', updated)

    # Convert return RedirectToAction(...) → return StatusCode(302)
    updated = re.sub(
        r'return\s+RedirectToAction\([^)]*\);',
        r'return StatusCode(302);  // TODO: Handle redirect in frontend',
        updated
    )

    # Convert return Json(data) → return Ok(data)
    updated = re.sub(r'return\s+Json\(([^)]*)\);', r'return Ok(\1);', updated)

    # Convert return HttpNotFound() → return NotFound()
    updated = re.sub(r'return\s+HttpNotFound\(\);', r'return NotFound();', updated)

    # Convert return new HttpStatusCodeResult(400) → return BadRequest()
    updated = re.sub(r'return\s+new\s+HttpStatusCodeResult\(400[^)]*\);', r'return BadRequest();', updated)

    # Remove ViewBag usage — comment it out
    updated = re.sub(
        r'ViewBag\.(\w+)\s*=\s*[^;]+;',
        r'// TODO: ViewBag.\1 removed — pass data via return Ok()',
        updated
    )

    # Change ActionResult / JsonResult return type to IActionResult
    updated = re.sub(r'\bActionResult\b', 'IActionResult', updated)
    updated = re.sub(r'\bJsonResult\b', 'IActionResult', updated)

    # Add distinct HTTP attributes to public methods without one.
    # Track GET methods per-class to avoid ambiguous routes across controllers.
    seen_get_routes: set[str] = set()

    def _add_http_attribute(m: re.Match) -> str:
        method_sig = m.group(0)
        preceding = updated[:m.start()]
        # Already has an HTTP attribute immediately before it
        if re.search(r'\[Http(Get|Post|Put|Delete|Patch)[^\]]*\]\s*$', preceding.rstrip()):
            return method_sig
        # Extract method name to infer HTTP verb
        method_name_match = re.search(r'public\s+(?:async\s+)?(?:IActionResult|Task<IActionResult>|void|Task)\s+(\w+)', method_sig)
        method_name = method_name_match.group(1) if method_name_match else 'action'
        method_lower = method_name.lower()
        # Infer verb from method name
        if any(x in method_lower for x in ['delete', 'remove']):
            return f'[HttpDelete("{{id}}")]\n        ' + method_sig
        if any(x in method_lower for x in ['post', 'save', 'create', 'add', 'insert']):
            return f'[HttpPost]\n        ' + method_sig
        if any(x in method_lower for x in ['put', 'update', 'edit']):
            return f'[HttpPut("{{{method_lower}_id}}")]\n        ' + method_sig
        # Default: GET — first method in this controller gets plain [HttpGet], rest get named routes
        if method_lower not in seen_get_routes and len(seen_get_routes) == 0:
            seen_get_routes.add(method_lower)
            return '[HttpGet]\n        ' + method_sig
        else:
            seen_get_routes.add(method_lower)
            return f'[HttpGet("{method_lower}")]\n        ' + method_sig

    updated = re.sub(
        r'public\s+(?:async\s+)?(?:IActionResult|Task<IActionResult>|void|Task)\s+\w+\s*\([^)]*\)\s*\{',
        _add_http_attribute,
        updated
    )

    return updated


def _replace_ef6_context_with_di(source: str, logs: list[str]) -> str:
    """Replace EF6 field-instantiated DbContext with DI constructor injection of ApplicationDbContext."""
    # Match: SomeDbContextName db = new SomeDbContextName();
    # or:    private SomeDbContextName db = new SomeDbContextName();
    field_pattern = re.compile(
        r'(private\s+)?(?P<ctx_type>\w+)\s+(?P<field>\w+)\s*=\s*new\s+(?P=ctx_type)\s*\(\s*\);',
        re.MULTILINE
    )

    # Find controller class name for constructor generation
    class_match = re.search(r'public\s+class\s+(\w+Controller)', source)
    class_name = class_match.group(1) if class_match else None

    updated = source
    for m in field_pattern.finditer(source):
        ctx_type = m.group('ctx_type')
        field_name = m.group('field')
        # Skip if already ApplicationDbContext or if type looks like a non-DbContext (e.g. plain models)
        if ctx_type in ('ApplicationDbContext', 'string', 'int', 'bool', 'List'):
            continue
        if not class_name:
            continue
        # Replace the field instantiation with a readonly field + constructor
        di_injection = (
            f"private readonly ApplicationDbContext {field_name};\n"
            f"        public {class_name}(ApplicationDbContext context) {{ {field_name} = context; }}"
        )
        updated = updated.replace(m.group(0), di_injection, 1)
        logs.append(f"Replaced '{ctx_type} {field_name} = new {ctx_type}()' with DI-injected ApplicationDbContext.")
        break  # one DbContext field per controller

    return updated


def _resolve_duplicate_method_names(source: str, logs: list[str]) -> str:
    """Detect and rename duplicate public method names to avoid CS0111 build errors."""
    # Find all public method signatures with their positions
    pattern = re.compile(
        r'(public\s+(?:async\s+)?(?:[\w<>]+)\s+(\w+)\s*\([^)]*\))',
        re.MULTILINE
    )
    name_counts: dict[str, int] = {}
    for m in pattern.finditer(source):
        name = m.group(2)
        name_counts[name] = name_counts.get(name, 0) + 1

    updated = source
    for name, count in name_counts.items():
        if count < 2:
            continue
        # Rename by inferring verb from context — GET vs POST duplicate
        # Replace second occurrence: add verb suffix based on [HttpPost]/[HttpGet] above it
        occurrences = [(m.start(), m.group(0), m.group(2)) for m in pattern.finditer(updated) if m.group(2) == name]
        for idx, (pos, full_sig, method_name) in enumerate(occurrences):
            if idx == 0:
                continue  # keep first as-is
            preceding = updated[:pos]
            if re.search(r'\[HttpPost\]', preceding[-300:]):
                new_name = f"Save{method_name}" if not method_name.lower().startswith("save") else f"{method_name}Post"
            elif re.search(r'\[HttpPut\]', preceding[-300:]):
                new_name = f"Update{method_name}"
            elif re.search(r'\[HttpDelete\]', preceding[-300:]):
                new_name = f"Delete{method_name}"
            else:
                new_name = f"{method_name}_{idx}"
            new_sig = full_sig.replace(f" {method_name}(", f" {new_name}(", 1)
            updated = updated.replace(full_sig, new_sig, 1)
            logs.append(f"Renamed duplicate method '{method_name}' → '{new_name}' to avoid CS0111.")
    return updated


def _add_swagger_package(csproj_xml: str) -> str:
    if "Swashbuckle" in csproj_xml:
        return csproj_xml
    swagger_ref = '    <PackageReference Include="Swashbuckle.AspNetCore" Version="6.5.0" />'
    if "<ItemGroup>" in csproj_xml:
        return csproj_xml.replace(
            "<ItemGroup>",
            f"<ItemGroup>\n{swagger_ref}",
            1
        )
    return csproj_xml.replace(
        "</Project>",
        f"\n  <ItemGroup>\n{swagger_ref}\n  </ItemGroup>\n</Project>"
    )


def _update_program_cs_for_api(source: str, logs: list[str]) -> str:
    updated = source

    # Replace AddControllersWithViews → AddControllers
    updated = updated.replace(
        "builder.Services.AddControllersWithViews();",
        "builder.Services.AddControllers();"
    )

    # Replace AddRazorPages → remove
    updated = re.sub(r'builder\.Services\.AddRazorPages\(\);\n?', '', updated)

    # Add Swagger if not present
    if "AddSwaggerGen" not in updated:
        updated = updated.replace(
            "builder.Services.AddControllers();",
            "builder.Services.AddControllers();\n"
            "builder.Services.AddEndpointsApiExplorer();\n"
            "builder.Services.AddSwaggerGen(c =>\n"
            "{\n"
            "    c.AddSecurityDefinition(\"Bearer\", new Microsoft.OpenApi.Models.OpenApiSecurityScheme\n"
            "    {\n"
            "        Name = \"Authorization\",\n"
            "        Type = Microsoft.OpenApi.Models.SecuritySchemeType.ApiKey,\n"
            "        Scheme = \"Bearer\",\n"
            "        BearerFormat = \"JWT\",\n"
            "        In = Microsoft.OpenApi.Models.ParameterLocation.Header,\n"
            "    });\n"
            "    c.AddSecurityRequirement(new Microsoft.OpenApi.Models.OpenApiSecurityRequirement\n"
            "    {\n"
            "        {\n"
            "            new Microsoft.OpenApi.Models.OpenApiSecurityScheme\n"
            "            {\n"
            "                Reference = new Microsoft.OpenApi.Models.OpenApiReference\n"
            "                { Type = Microsoft.OpenApi.Models.ReferenceType.SecurityScheme, Id = \"Bearer\" }\n"
            "            }, new string[] {}\n"
            "        }\n"
            "    });\n"
            "});"
        )

    # Add CORS if not present
    if "AddCors" not in updated:
        updated = updated.replace(
            "builder.Services.AddControllers();",
            "builder.Services.AddCors(options =>\n"
            "{\n"
            "    options.AddPolicy(\"AllowFrontend\", policy =>\n"
            "        policy.WithOrigins(\"http://localhost:5173\")\n"
            "              .AllowAnyHeader()\n"
            "              .AllowAnyMethod());\n"
            "});\n"
            "builder.Services.AddControllers();"
        )

    # Add Swagger middleware if not present
    if "UseSwagger" not in updated:
        updated = updated.replace(
            "app.UseHttpsRedirection();",
            "app.UseSwagger();\n"
            "app.UseSwaggerUI();\n"
            "app.UseHttpsRedirection();"
        )

    # Add CORS middleware if not present
    if "app.UseCors" not in updated:
        updated = updated.replace(
            "app.UseAuthorization();",
            "app.UseCors(\"AllowFrontend\");\n"
            "app.UseAuthorization();"
        )

    # Replace MapRazorPages → MapControllers
    updated = re.sub(r'app\.MapRazorPages\(\);\n?', '', updated)
    updated = re.sub(r'app\.MapBlazorHub\(\);\n?', '', updated)
    updated = re.sub(r'app\.MapFallbackToPage\([^)]*\);\n?', '', updated)

    # Add MapControllers if not present
    if "MapControllers" not in updated:
        updated = updated.replace(
            "app.Run();",
            "app.MapControllers();\n\napp.Run();"
        )

    return updated
