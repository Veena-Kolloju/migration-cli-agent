from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from migration_agent_cli.core.agent_base import StructuredMigrationAgent
from migration_agent_cli.core.models import AgentExecutionContext


class EfMigrationAgent(StructuredMigrationAgent):
    agent_id = "ef-migration"
    title = "Entity Framework Migration Agent"
    description = "Migrates Entity Framework 6 (EDMX) to Entity Framework Core."
    capabilities = [
        "EF6 DbContext → EF Core DbContext",
        "EDMX files removed",
        "System.Data.Entity → Microsoft.EntityFrameworkCore",
        "Add EF Core NuGet packages",
        "Update Program.cs with DbContext registration",
    ]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        if not migrated_root:
            logs.append("No migrated source path — skipping EF migration.")
            return {"migratedContexts": [], "removedEdmxFiles": [], "changedFiles": []}

        root = Path(migrated_root)
        migrated_contexts: list[str] = []
        removed_edmx: list[str] = []
        changed_files: list[str] = []

        # Find and remove EDMX files
        for edmx_file in root.rglob("*.edmx"):
            if any(p in edmx_file.parts for p in {"bin", "obj"}):
                continue
            edmx_file.unlink()
            removed_edmx.append(edmx_file.name)
            logs.append(f"Removed EDMX file: {edmx_file.name}.")

            # Remove associated .tt and .Designer.cs files
            for associated in [
                edmx_file.with_suffix(".edmx.diagram"),
                edmx_file.parent / f"{edmx_file.stem}.Context.tt",
                edmx_file.parent / f"{edmx_file.stem}.tt",
                edmx_file.parent / f"{edmx_file.stem}.Designer.cs",
            ]:
                if associated.exists():
                    associated.unlink()
                    logs.append(f"Removed associated file: {associated.name}.")

        # Migrate DbContext files — scan ALL .cs files for classes inheriting DbContext
        for cs_file in root.rglob("*.cs"):
            if any(p in cs_file.parts for p in {"bin", "obj"}):
                continue
            try:
                original = cs_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if "DbContext" not in original:
                continue
            # Must have a class that inherits DbContext (directly or via IdentityDbContext)
            if not re.search(r'class\s+\w+\s*:\s*(?:IdentityDbContext|DbContext)', original):
                continue
            migrated = _migrate_dbcontext(original, cs_file.stem, logs)
            if migrated != original:
                cs_file.write_text(migrated, encoding="utf-8")
                changed_files.append(str(cs_file))
                migrated_contexts.append(cs_file.name)
                logs.append(f"Migrated DbContext: {cs_file.name} → EF Core.")

        # Migrate any other CS files using System.Data.Entity
        for cs_file in root.rglob("*.cs"):
            if any(p in cs_file.parts for p in {"bin", "obj"}):
                continue
            if cs_file.name.endswith(".Context.cs"):
                continue
            try:
                original = cs_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if "System.Data.Entity" in original:
                updated = _migrate_entity_references(original)
                if updated != original:
                    cs_file.write_text(updated, encoding="utf-8")
                    changed_files.append(str(cs_file))
                    logs.append(f"Updated EF6 references in {cs_file.name}.")

        # Add EF Core packages to .csproj
        for csproj in root.rglob("*.csproj"):
            if any(p in csproj.parts for p in {"bin", "obj"}):
                continue
            original = csproj.read_text(encoding="utf-8", errors="ignore")
            updated = _add_efcore_packages(original)
            if updated != original:
                csproj.write_text(updated, encoding="utf-8")
                changed_files.append(str(csproj))
                logs.append(f"Added EF Core packages to {csproj.name}.")

        # Update Program.cs to register DbContext
        if migrated_contexts:
            context_name = _extract_context_name(migrated_contexts[0])
            for program_cs in root.rglob("Program.cs"):
                if any(p in program_cs.parts for p in {"bin", "obj"}):
                    continue
                original = program_cs.read_text(encoding="utf-8", errors="ignore")
                updated = _register_dbcontext_in_program(original, context_name)
                if updated != original:
                    program_cs.write_text(updated, encoding="utf-8")
                    changed_files.append(str(program_cs))
                    logs.append(f"Registered {context_name} in Program.cs.")

        logs.append(f"EF migration complete. Contexts migrated: {len(migrated_contexts)}, EDMX removed: {len(removed_edmx)}.")
        return {
            "migratedContexts": migrated_contexts,
            "removedEdmxFiles": removed_edmx,
            "changedFiles": changed_files,
        }


def _migrate_dbcontext(source: str, filename: str, logs: list[str]) -> str:
    updated = source

    # Replace using System.Data.Entity → Microsoft.EntityFrameworkCore
    updated = updated.replace(
        "using System.Data.Entity;",
        "using Microsoft.EntityFrameworkCore;"
    )
    updated = re.sub(r'using System\.Data\.Entity\.[^;]+;\n?', '', updated)

    # Replace EF6 DbContext constructor pattern
    # Old: public SampleModelContext() : base("name=SampleModelContext") {}
    # New: public SampleModelContext(DbContextOptions<SampleModelContext> options) : base(options) {}
    context_name_match = re.search(r'public\s+class\s+(\w+)\s*:', source)
    context_name = context_name_match.group(1) if context_name_match else "AppDbContext"

    updated = re.sub(
        r'public\s+' + re.escape(context_name) + r'\s*\(\s*\)\s*[\r\n\s]*:\s*base\s*\([^)]*\)[\r\n\s]*\{[^}]*\}',
        f'public {context_name}() : base() {{ }}',
        updated,
        flags=re.MULTILINE | re.DOTALL
    )

    # Add OnConfiguring fallback for design-time (dotnet ef migrations)
    if 'OnConfiguring' not in updated:
        on_configuring = (
            '\n        protected override void OnConfiguring(DbContextOptionsBuilder optionsBuilder)\n'
            '        {\n'
            '            if (!optionsBuilder.IsConfigured)\n'
            '            {\n'
            '                optionsBuilder.UseSqlServer("Server=YOUR_SERVER;Database=YOUR_DB;TrustServerCertificate=True");\n'
            '            }\n'
            '        }\n'
        )
        # Insert before the closing brace of the class
        updated = re.sub(r'(\n\s*public\s+DbSet)', on_configuring + r'\1', updated, count=1)

    # Remove UnintentionalCodeFirstException throw — EF6 only
    updated = re.sub(r'[ \t]*throw new UnintentionalCodeFirstException\(\);[\r\n]*', '', updated)

    # Remove Database.SetInitializer calls
    updated = re.sub(r'Database\.SetInitializer[^;]+;\n?', '', updated)

    # Remove [DbConfigurationType(...)] attribute
    updated = re.sub(r'\[DbConfigurationType[^\]]*\]\n?', '', updated)

    # Replace ObjectResult<T> → IEnumerable<T>
    updated = re.sub(r'ObjectResult<(\w+)>', r'IEnumerable<\1>', updated)

    # Add using for IEnumerable if needed
    if "IEnumerable" in updated and "using System.Collections.Generic;" not in updated:
        updated = "using System.Collections.Generic;\n" + updated

    # Add EF Core using
    if "using Microsoft.EntityFrameworkCore;" not in updated:
        updated = "using Microsoft.EntityFrameworkCore;\n" + updated

    return updated


def _migrate_entity_references(source: str) -> str:
    updated = source
    updated = updated.replace(
        "using System.Data.Entity;",
        "using Microsoft.EntityFrameworkCore;"
    )
    updated = re.sub(r'using System\.Data\.Entity\.[^;]+;\n?', '', updated)
    return updated


def _add_efcore_packages(csproj_xml: str) -> str:
    if "EntityFrameworkCore" in csproj_xml:
        return csproj_xml

    # Remove old EF6 reference
    csproj_xml = re.sub(
        r'\s*<Reference Include="EntityFramework[^"]*"[^/]*/>\n?',
        '',
        csproj_xml
    )
    csproj_xml = re.sub(
        r'\s*<Reference Include="EntityFramework">[^<]*(?:<[^/][^>]*>[^<]*</[^>]*>)*[^<]*</Reference>\n?',
        '',
        csproj_xml
    )

    ef_packages = (
        '    <PackageReference Include="Microsoft.EntityFrameworkCore" Version="8.0.0" />\n'
        '    <PackageReference Include="Microsoft.EntityFrameworkCore.SqlServer" Version="8.0.0" />\n'
        '    <PackageReference Include="Microsoft.EntityFrameworkCore.Tools" Version="8.0.0">\n'
        '      <PrivateAssets>all</PrivateAssets>\n'
        '    </PackageReference>\n'
        '    <PackageReference Include="Microsoft.EntityFrameworkCore.Design" Version="8.0.0">\n'
        '      <IncludeAssets>runtime; build; native; contentfiles; analyzers; buildtransitive</IncludeAssets>\n'
        '      <PrivateAssets>all</PrivateAssets>\n'
        '    </PackageReference>'
    )

    if "<ItemGroup>" in csproj_xml:
        return csproj_xml.replace("<ItemGroup>", f"<ItemGroup>\n{ef_packages}", 1)
    return csproj_xml.replace(
        "</Project>",
        f"\n  <ItemGroup>\n{ef_packages}\n  </ItemGroup>\n</Project>"
    )


def _extract_context_name(filename: str) -> str:
    return filename.replace(".Context.cs", "").replace(".cs", "")


def _register_dbcontext_in_program(source: str, context_name: str) -> str:
    if context_name in source or "DbContext" in source:
        return source

    db_registration = (
        f"builder.Services.AddDbContext<{context_name}>(options =>\n"
        f"    options.UseSqlServer(builder.Configuration.GetConnectionString(\"DefaultConnection\")));"
    )

    using_line = "using Microsoft.EntityFrameworkCore;"
    if using_line not in source:
        source = using_line + "\n" + source

    return source.replace(
        "builder.Services.AddControllers();",
        f"{db_registration}\nbuilder.Services.AddControllers();"
    )
