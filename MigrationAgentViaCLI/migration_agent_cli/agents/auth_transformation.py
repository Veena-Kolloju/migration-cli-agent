from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from migration_agent_cli.core.agent_base import StructuredMigrationAgent
from migration_agent_cli.core.models import AgentExecutionContext


class AuthTransformationAgent(StructuredMigrationAgent):
    agent_id = "auth-transformation"
    title = "Auth Transformation Agent"
    description = "Replaces legacy SimpleMembership/DotNetOpenAuth with ASP.NET Core Identity + JWT."
    capabilities = [
        "Delete legacy auth files (AccountController, AuthConfig, InitializeSimpleMembership)",
        "Generate ApplicationUser.cs",
        "Update DbContext to IdentityDbContext",
        "Generate clean AuthController with JWT login/register",
        "Update Program.cs with Identity + JWT setup",
        "Add Identity + JWT NuGet packages",
        "Add JWT config to appsettings.json",
    ]

    def analyze(self, context: AgentExecutionContext, logs: list[str]) -> dict[str, Any]:
        migrated_root = context.shared_state.get("project-conversion", {}).get("migratedSourcePath")
        if not migrated_root:
            logs.append("No migrated source path — skipping auth transformation.")
            return _empty_result()

        root = Path(migrated_root)
        changed_files: list[str] = []
        deleted_files: list[str] = []
        generated_files: list[str] = []

        # Step 1 — Delete legacy auth files
        _delete_legacy_auth_files(root, deleted_files, logs)

        # Step 2 — Add Identity + JWT packages to .csproj
        for csproj in root.rglob("*.csproj"):
            if any(p in csproj.parts for p in {"bin", "obj"}):
                continue
            original = csproj.read_text(encoding="utf-8", errors="ignore")
            updated = _add_identity_packages(original)
            if updated != original:
                csproj.write_text(updated, encoding="utf-8")
                changed_files.append(str(csproj))
                logs.append(f"Added Identity + JWT packages to {csproj.name}.")

        # Step 3 — Generate ApplicationUser.cs
        app_user_path = _generate_application_user(root, logs)
        if app_user_path:
            generated_files.append(app_user_path)

        # Step 3b — Delete old EF6 generated files that conflict with EF Core
        ef6_dead_names = {"SampleModel.Context.cs", "SampleModel.cs", "SampleModel.Designer.cs",
                          "SampleModel.Context.tt", "SampleModel.tt"}
        for f in root.rglob("*"):
            if f.is_file() and f.name in ef6_dead_names and not any(p in f.parts for p in {"bin", "obj"}):
                f.unlink()
                deleted_files.append(str(f))
                logs.append(f"Deleted EF6 generated file: {f.name}.")

        # Step 4 — Generate fresh ApplicationDbContext with DbSet properties from EDMX entities
        edmx_entities = context.shared_state.get("ef-migration", {}).get("edmxEntityNames", [])
        app_ctx_path = _generate_application_db_context(root, edmx_entities, logs)
        if app_ctx_path:
            generated_files.append(app_ctx_path)
            changed_files.append(app_ctx_path)

        # Step 4b — Update any remaining DbContext files to extend IdentityDbContext
        for cs_file in root.rglob("*.cs"):
            if any(p in cs_file.parts for p in {"bin", "obj"}):
                continue
            if cs_file.name == "ApplicationDbContext.cs":
                continue  # already generated fresh above
            original = cs_file.read_text(encoding="utf-8", errors="ignore")
            updated = _update_db_context(original)
            if updated != original:
                cs_file.write_text(updated, encoding="utf-8")
                changed_files.append(str(cs_file))
                logs.append(f"Updated {cs_file.name} to extend IdentityDbContext.")

        # Step 5 — Generate clean AuthController.cs
        auth_ctrl_path = _generate_auth_controller(root, logs)
        if auth_ctrl_path:
            generated_files.append(auth_ctrl_path)

        # Step 6 — Update Program.cs with Identity + JWT
        for program_cs in root.rglob("Program.cs"):
            if any(p in program_cs.parts for p in {"bin", "obj"}):
                continue
            original = program_cs.read_text(encoding="utf-8", errors="ignore")
            updated = _update_program_cs_for_auth(original)
            if updated != original:
                program_cs.write_text(updated, encoding="utf-8")
                changed_files.append(str(program_cs))
                logs.append("Updated Program.cs with Identity + JWT configuration.")

        # Step 7 — Add JWT section to appsettings.json
        appsettings_path = _update_appsettings(root, logs)
        if appsettings_path:
            generated_files.append(appsettings_path)

        logs.append(
            f"Auth transformation complete. "
            f"Deleted: {len(deleted_files)}, Generated: {len(generated_files)}, Changed: {len(changed_files)}."
        )
        return {
            "deletedFiles": deleted_files,
            "generatedFiles": generated_files,
            "changedFiles": changed_files,
        }


# ---------------------------------------------------------------------------
# Step 1 — Delete legacy auth files
# ---------------------------------------------------------------------------

def _delete_legacy_auth_files(root: Path, deleted_files: list[str], logs: list[str]) -> None:
    """Delete files that are entirely built on SimpleMembership/DotNetOpenAuth."""
    # Files to delete by name
    legacy_filenames = {
        "AccountController.cs",
        "AuthConfig.cs",
        "InitializeSimpleMembershipAttribute.cs",
    }
    for cs_file in root.rglob("*.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        if cs_file.name in legacy_filenames:
            cs_file.unlink()
            deleted_files.append(str(cs_file))
            logs.append(f"Deleted legacy auth file: {cs_file.name}.")


# ---------------------------------------------------------------------------
# Step 2 — Add Identity + JWT packages
# ---------------------------------------------------------------------------

def _add_identity_packages(csproj_xml: str) -> str:
    packages_to_add = [
        ('Microsoft.AspNetCore.Identity.EntityFrameworkCore', '8.0.0'),
        ('Microsoft.AspNetCore.Authentication.JwtBearer', '8.0.0'),
        ('System.IdentityModel.Tokens.Jwt', '7.3.1'),
        ('Microsoft.EntityFrameworkCore.Tools', '8.0.0'),
        ('Microsoft.EntityFrameworkCore.Design', '8.0.0'),
    ]
    updated = csproj_xml
    for pkg_name, version in packages_to_add:
        if pkg_name not in updated:
            # EF Tools and Design need PrivateAssets
            if pkg_name in ('Microsoft.EntityFrameworkCore.Tools', 'Microsoft.EntityFrameworkCore.Design'):
                ref = (
                    f'    <PackageReference Include="{pkg_name}" Version="{version}">\n'
                    f'      <PrivateAssets>all</PrivateAssets>\n'
                    f'    </PackageReference>'
                )
            else:
                ref = f'    <PackageReference Include="{pkg_name}" Version="{version}" />'
            if "<ItemGroup>" in updated:
                updated = updated.replace("<ItemGroup>", f"<ItemGroup>\n{ref}", 1)
            else:
                updated = updated.replace(
                    "</Project>",
                    f"\n  <ItemGroup>\n{ref}\n  </ItemGroup>\n</Project>"
                )
    return updated


# ---------------------------------------------------------------------------
# Step 3 — Generate ApplicationUser.cs
# ---------------------------------------------------------------------------

def _generate_application_db_context(root: Path, entity_names: list[str], logs: list[str]) -> str | None:
    """Generate a fresh ApplicationDbContext with DbSet properties for all EDMX entities."""
    namespace = _detect_namespace(root)
    out_path = root / "Models" / "ApplicationDbContext.cs"
    out_path.parent.mkdir(exist_ok=True)

    # Deduplicate entity names to avoid duplicate DbSet properties
    seen: set[str] = set()
    unique_entities = [e for e in entity_names if not (e in seen or seen.add(e))]

    dbsets = ""
    for entity in unique_entities:
        dbsets += f"\n        public virtual DbSet<{entity}> {entity} {{ get; set; }}"

    content = f"""using Microsoft.AspNetCore.Identity.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore;

namespace {namespace}.Models
{{
    public class ApplicationDbContext : IdentityDbContext<ApplicationUser>
    {{
        public ApplicationDbContext(DbContextOptions<ApplicationDbContext> options)
            : base(options) {{ }}{dbsets}
    }}
}}
"""
    out_path.write_text(content, encoding="utf-8")
    logs.append(f"Generated ApplicationDbContext.cs with {len(unique_entities)} DbSet(s): {', '.join(unique_entities) or 'none'}.")
    return str(out_path)


def _generate_application_user(root: Path, logs: list[str]) -> str | None:
    # Detect namespace from any existing .cs file
    namespace = _detect_namespace(root)
    out_path = root / "Models" / "ApplicationUser.cs"
    out_path.parent.mkdir(exist_ok=True)

    content = f"""using Microsoft.AspNetCore.Identity;

namespace {namespace}.Models
{{
    /// <summary>
    /// Application user — extends ASP.NET Core Identity.
    /// Add custom user fields here (e.g. FullName, Department).
    /// </summary>
    public class ApplicationUser : IdentityUser
    {{
        // TODO: Add custom user properties here if needed
        // public string FullName {{ get; set; }} = string.Empty;
    }}
}}
"""
    out_path.write_text(content, encoding="utf-8")
    logs.append(f"Generated ApplicationUser.cs in Models/.")
    return str(out_path)


# ---------------------------------------------------------------------------
# Step 4 — Update DbContext to extend IdentityDbContext
# ---------------------------------------------------------------------------

def _update_db_context(source: str) -> str:
    # Only touch files that define a class inheriting DbContext — not controllers that use it
    if "IdentityDbContext" in source:
        return source
    if not re.search(r'class\s+\w+\s*:\s*DbContext', source):
        return source

    updated = source

    # Add Identity using if not present
    if "Microsoft.AspNetCore.Identity.EntityFrameworkCore" not in updated:
        updated = "using Microsoft.AspNetCore.Identity.EntityFrameworkCore;\n" + updated

    # Replace: public partial class XxxContext : DbContext
    # With:    public partial class XxxContext : IdentityDbContext<ApplicationUser>
    updated = re.sub(
        r'(public\s+(?:partial\s+)?class\s+\w+Context\s*:\s*)DbContext',
        r'\1IdentityDbContext<ApplicationUser>',
        updated
    )

    # Add ApplicationUser using if not present
    if "ApplicationUser" in updated and ".Models" not in updated:
        ns_match = re.search(r'namespace\s+([\w.]+)', updated)
        if ns_match:
            ns = ns_match.group(1)
            updated = f"using {ns}.Models;\n" + updated

    return updated


# ---------------------------------------------------------------------------
# Step 5 — Generate AuthController.cs
# ---------------------------------------------------------------------------

def _generate_auth_controller(root: Path, logs: list[str]) -> str | None:
    namespace = _detect_namespace(root)
    controllers_dir = root / "Controllers"
    controllers_dir.mkdir(exist_ok=True)
    out_path = controllers_dir / "AuthController.cs"

    content = f"""using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Text;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Identity;
using Microsoft.AspNetCore.Mvc;
using Microsoft.IdentityModel.Tokens;
using {namespace}.Models;

namespace {namespace}.Controllers
{{
    [ApiController]
    [Route("api/[controller]")]
    public class AuthController : ControllerBase
    {{
        private readonly UserManager<ApplicationUser> _userManager;
        private readonly SignInManager<ApplicationUser> _signInManager;
        private readonly IConfiguration _configuration;

        public AuthController(
            UserManager<ApplicationUser> userManager,
            SignInManager<ApplicationUser> signInManager,
            IConfiguration configuration)
        {{
            _userManager = userManager;
            _signInManager = signInManager;
            _configuration = configuration;
        }}

        // POST /api/auth/register
        [HttpPost("register")]
        [AllowAnonymous]
        public async Task<IActionResult> Register([FromBody] RegisterDto dto)
        {{
            if (!ModelState.IsValid)
                return BadRequest(ModelState);

            var user = new ApplicationUser
            {{
                UserName = dto.Email,
                Email = dto.Email,
            }};

            var result = await _userManager.CreateAsync(user, dto.Password);
            if (!result.Succeeded)
                return BadRequest(result.Errors);

            var token = _generateJwtToken(user);
            return Ok(new {{ token, email = user.Email }});
        }}

        // POST /api/auth/login
        [HttpPost("login")]
        [AllowAnonymous]
        public async Task<IActionResult> Login([FromBody] LoginDto dto)
        {{
            if (!ModelState.IsValid)
                return BadRequest(ModelState);

            var user = await _userManager.FindByEmailAsync(dto.Email);
            if (user == null)
                return Unauthorized(new {{ message = "Invalid email or password." }});

            var result = await _signInManager.CheckPasswordSignInAsync(user, dto.Password, lockoutOnFailure: false);
            if (!result.Succeeded)
                return Unauthorized(new {{ message = "Invalid email or password." }});

            var token = _generateJwtToken(user);
            return Ok(new {{ token, email = user.Email }});
        }}

        // GET /api/auth/profile
        [HttpGet("profile")]
        [Authorize]
        public async Task<IActionResult> Profile()
        {{
            var userId = User.FindFirstValue(ClaimTypes.NameIdentifier);
            var user = await _userManager.FindByIdAsync(userId!);
            if (user == null)
                return NotFound();

            return Ok(new {{ email = user.Email, userName = user.UserName }});
        }}

        // POST /api/auth/change-password
        [HttpPost("change-password")]
        [Authorize]
        public async Task<IActionResult> ChangePassword([FromBody] ChangePasswordDto dto)
        {{
            var userId = User.FindFirstValue(ClaimTypes.NameIdentifier);
            var user = await _userManager.FindByIdAsync(userId!);
            if (user == null)
                return NotFound();

            var result = await _userManager.ChangePasswordAsync(user, dto.CurrentPassword, dto.NewPassword);
            if (!result.Succeeded)
                return BadRequest(result.Errors);

            return Ok(new {{ message = "Password changed successfully." }});
        }}

        // ---------------------------------------------------------------------------
        // JWT token generator
        // ---------------------------------------------------------------------------

        private string _generateJwtToken(ApplicationUser user)
        {{
            var key = new SymmetricSecurityKey(
                Encoding.UTF8.GetBytes(_configuration["Jwt:Key"]!));
            var credentials = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);

            var claims = new[]
            {{
                new Claim(JwtRegisteredClaimNames.Sub, user.Id),
                new Claim(JwtRegisteredClaimNames.Email, user.Email!),
                new Claim(ClaimTypes.NameIdentifier, user.Id),
            }};

            var expiry = DateTime.UtcNow.AddMinutes(
                double.Parse(_configuration["Jwt:ExpiryMinutes"] ?? "60"));

            var token = new JwtSecurityToken(
                issuer: _configuration["Jwt:Issuer"],
                audience: _configuration["Jwt:Audience"],
                claims: claims,
                expires: expiry,
                signingCredentials: credentials
            );

            return new JwtSecurityTokenHandler().WriteToken(token);
        }}
    }}

    // ---------------------------------------------------------------------------
    // DTOs
    // ---------------------------------------------------------------------------

    public record RegisterDto(string Email, string Password);
    public record LoginDto(string Email, string Password);
    public record ChangePasswordDto(string CurrentPassword, string NewPassword);
}}
"""
    out_path.write_text(content, encoding="utf-8")
    logs.append("Generated AuthController.cs with register/login/profile/change-password endpoints.")
    return str(out_path)


# ---------------------------------------------------------------------------
# Step 6 — Update Program.cs for Identity + JWT
# ---------------------------------------------------------------------------

def _update_program_cs_for_auth(source: str) -> str:
    updated = source

    # Add required usings
    usings_needed = [
        "using Microsoft.AspNetCore.Authentication.JwtBearer;",
        "using Microsoft.AspNetCore.Identity;",
        "using Microsoft.IdentityModel.Tokens;",
        "using System.Text;",
    ]
    for u in usings_needed:
        if u not in updated:
            updated = u + "\n" + updated

    # Detect DbContext class name from existing registration
    ctx_match = re.search(r'AddDbContext<(\w+)>', updated)
    ctx_name = ctx_match.group(1) if ctx_match else "ApplicationDbContext"

    # Add Identity registration after AddControllers if not present
    if "AddIdentity" not in updated and "AddDefaultIdentity" not in updated:
        identity_block = (
            f"\nbuilder.Services.AddIdentity<ApplicationUser, IdentityRole>(options =>\n"
            "{\n"
            "    options.Password.RequireDigit = true;\n"
            "    options.Password.RequiredLength = 6;\n"
            "    options.Password.RequireNonAlphanumeric = false;\n"
            "})\n"
            f".AddEntityFrameworkStores<{ctx_name}>()\n"
            ".AddDefaultTokenProviders();\n"
        )
        updated = updated.replace(
            "builder.Services.AddControllers();",
            "builder.Services.AddControllers();" + identity_block
        )

    # Add JWT authentication if not present
    if "AddJwtBearer" not in updated:
        jwt_block = (
            "\nbuilder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)\n"
            "    .AddJwtBearer(options =>\n"
            "    {\n"
            "        options.TokenValidationParameters = new TokenValidationParameters\n"
            "        {\n"
            "            ValidateIssuer = true,\n"
            "            ValidateAudience = true,\n"
            "            ValidateLifetime = true,\n"
            "            ValidateIssuerSigningKey = true,\n"
            "            ValidIssuer = builder.Configuration[\"Jwt:Issuer\"],\n"
            "            ValidAudience = builder.Configuration[\"Jwt:Audience\"],\n"
            "            IssuerSigningKey = new SymmetricSecurityKey(\n"
            "                Encoding.UTF8.GetBytes(builder.Configuration[\"Jwt:Key\"]!))\n"
            "        };\n"
            "    });\n"
        )
        updated = updated.replace(
            "builder.Services.AddControllers();",
            "builder.Services.AddControllers();" + jwt_block
        )

    # Add ApplicationUser using if not there
    if "using" in updated and "ApplicationUser" not in updated:
        ns_match = re.search(r'namespace\s+([\w.]+)', updated)
        if not ns_match:
            # Try to detect from existing DbContext registration
            ns_match2 = re.search(r'AddDbContext<\w+>', updated)
            if ns_match2:
                updated = "// TODO: Add 'using YourNamespace.Models;' for ApplicationUser\n" + updated
        else:
            ns = ns_match.group(1)
            using_line = f"using {ns}.Models;"
            if using_line not in updated:
                updated = using_line + "\n" + updated

    # Add UseAuthentication before UseAuthorization if not present
    if "UseAuthentication" not in updated:
        updated = updated.replace(
            "app.UseAuthorization();",
            "app.UseAuthentication();\napp.UseAuthorization();"
        )

    return updated


# ---------------------------------------------------------------------------
# Step 7 — Update appsettings.json
# ---------------------------------------------------------------------------

def _update_appsettings(root: Path, logs: list[str]) -> str | None:
    import json

    appsettings_path = root / "appsettings.json"

    # Read existing or start fresh
    if appsettings_path.exists():
        try:
            data = json.loads(appsettings_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            data = {}
    else:
        data = {}

    # Add JWT section if not present
    if "Jwt" not in data:
        data["Jwt"] = {
            "Key": "CHANGE-THIS-TO-A-SECRET-KEY-MIN-32-CHARS",
            "Issuer": "MvcApplication1",
            "Audience": "MvcApplication1",
            "ExpiryMinutes": "60"
        }

    # Add ConnectionStrings placeholder if not present
    if "ConnectionStrings" not in data:
        data["ConnectionStrings"] = {
            "DefaultConnection": "Server=YOUR_SERVER;Database=YOUR_DB;User Id=YOUR_USER;Password=YOUR_PASSWORD;TrustServerCertificate=True"
        }

    from migration_agent_cli.core.guardrails import check_json, check_secrets
    content = json.dumps(data, indent=2)
    if not check_json(content, "appsettings.json", logs):
        return None
    check_secrets(content, "appsettings.json", logs)
    appsettings_path.write_text(content, encoding="utf-8")
    logs.append("Updated appsettings.json with Jwt and ConnectionStrings sections.")
    return str(appsettings_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_namespace(root: Path) -> str:
    for cs_file in root.rglob("*.cs"):
        if any(p in cs_file.parts for p in {"bin", "obj"}):
            continue
        try:
            content = cs_file.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r'namespace\s+([\w.]+)', content)
            if match:
                # Return only the root namespace segment (e.g. MvcApplication1)
                return match.group(1).split(".")[0]
        except Exception:
            continue
    return "MvcApplication1"


def _empty_result() -> dict[str, Any]:
    return {
        "deletedFiles": [],
        "generatedFiles": [],
        "changedFiles": [],
    }
