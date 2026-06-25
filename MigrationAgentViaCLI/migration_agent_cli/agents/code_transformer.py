from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Transformation rules: each rule is (pattern, replacement, description)
# Applied in order via regex substitution on each .cs file
# ---------------------------------------------------------------------------
CS_TRANSFORMATION_RULES: list[tuple[str, str, str]] = [
    # Gap 2 — Session-based auth checks → [Authorize] TODO comment
    (
        r'if\s*\(\s*Session\s*\[\s*["\'][^"\']*(user|login|auth)[^"\']*(\s*["\'])?\s*\]\s*==\s*null\s*\)',
        r'// TODO: Replace session auth check with [Authorize] attribute — JWT handles this automatically\n            if (false /* session check removed */)',
        "Session auth check → [Authorize] TODO",
    ),
    # ConfigurationManager → IConfiguration
    (
        r'ConfigurationManager\.AppSettings\[([^\]]+)\]',
        r'_configuration[\1]',
        "ConfigurationManager.AppSettings → IConfiguration indexer",
    ),
    (
        r'ConfigurationManager\.ConnectionStrings\[([^\]]+)\]\.ConnectionString',
        r'_configuration.GetConnectionString(\1)',
        "ConfigurationManager.ConnectionStrings → IConfiguration.GetConnectionString",
    ),
    # HttpContext.Current → IHttpContextAccessor
    (
        r'HttpContext\.Current\.User',
        r'_httpContextAccessor.HttpContext.User',
        "HttpContext.Current.User → IHttpContextAccessor",
    ),
    (
        r'HttpContext\.Current\.Request',
        r'_httpContextAccessor.HttpContext.Request',
        "HttpContext.Current.Request → IHttpContextAccessor",
    ),
    (
        r'HttpContext\.Current\.Response',
        r'_httpContextAccessor.HttpContext.Response',
        "HttpContext.Current.Response → IHttpContextAccessor",
    ),
    (
        r'HttpContext\.Current\.Session\[([^\]]+)\]',
        r'_httpContextAccessor.HttpContext.Session.GetString(\1)',
        "HttpContext.Current.Session → ISession.GetString",
    ),
    # System.Web namespace → ASP.NET Core equivalents
    (
        r'using System\.Web\.Mvc;',
        r'using Microsoft.AspNetCore.Mvc;',
        "System.Web.Mvc → Microsoft.AspNetCore.Mvc",
    ),
    (
        r'using System\.Web\.Http;',
        r'using Microsoft.AspNetCore.Mvc;',
        "System.Web.Http → Microsoft.AspNetCore.Mvc",
    ),
    (
        r'using System\.Web;',
        r'// using System.Web; // Removed: migrate to ASP.NET Core abstractions',
        "System.Web using removed",
    ),
    (
        r'using System\.Web\.Security;',
        r'// using System.Web.Security; // Removed: replaced by ASP.NET Core Identity',
        "System.Web.Security using removed",
    ),
    (
        r'using System\.Web\.Optimization;',
        r'// using System.Web.Optimization; // Removed: replaced by Vite/Webpack bundling',
        "System.Web.Optimization using removed",
    ),
    (
        r'using System\.Web\.([A-Za-z.]+);',
        r'// using System.Web.\1; // Removed: not available in .NET Core',
        "System.Web.* using removed",
    ),
    (
        r'using System\.Web\.Routing;',
        r'using Microsoft.AspNetCore.Routing;',
        "System.Web.Routing → Microsoft.AspNetCore.Routing",
    ),
    (
        r'\bDbModelBuilder\b',
        r'ModelBuilder',
        "DbModelBuilder → ModelBuilder (EF Core)",
    ),
    (
        r'using System\.Data\.Entity;',
        r'using Microsoft.EntityFrameworkCore;',
        "System.Data.Entity → Microsoft.EntityFrameworkCore",
    ),
    # EF6-only DbContext constructor taking connection string name → parameterless + OnConfiguring
    (
        r'(public\s+\w+\(\)[\r\n\s]*:[\r\n\s]*base\()"[^"]+"(\))',
        r'\1\2',
        "EF6 base(connectionString) constructor → parameterless EF Core constructor",
    ),
    # UnintentionalCodeFirstException → remove the throw entirely
    (
        r'\s*throw new UnintentionalCodeFirstException\(\);',
        r'',
        "UnintentionalCodeFirstException removed — EF6-only",
    ),
    # System.Data.EntityState → Microsoft.EntityFrameworkCore.EntityState
    (
        r'System\.Data\.EntityState',
        r'Microsoft.EntityFrameworkCore.EntityState',
        "System.Data.EntityState → Microsoft.EntityFrameworkCore.EntityState",
    ),
    # JsonRequestBehavior.AllowGet — not needed in ASP.NET Core
    (
        r',\s*JsonRequestBehavior\.AllowGet',
        r'',
        "JsonRequestBehavior.AllowGet removed — not needed in ASP.NET Core",
    ),
    # Session["key"] → HttpContext.Session.GetString("key")
    (
        r'Session\[([^\]]+)\]',
        r'HttpContext.Session.GetString(\1)',
        "Session[] → HttpContext.Session.GetString()",
    ),
    # AuthConfig.RegisterAuth() — deleted file, comment it out
    (
        r'AuthConfig\.RegisterAuth\(\);',
        r'// AuthConfig.RegisterAuth(); // Removed: replaced by ASP.NET Core Identity + JWT',
        "AuthConfig.RegisterAuth removed — replaced by Identity",
    ),
    # WebForms / MVC base classes
    (
        r'\bSystem\.Web\.UI\.Page\b',
        r'Microsoft.AspNetCore.Mvc.RazorPages.PageModel',
        "System.Web.UI.Page → PageModel",
    ),
    (
        r'\bSystem\.Web\.Mvc\.Controller\b',
        r'Microsoft.AspNetCore.Mvc.Controller',
        "System.Web.Mvc.Controller → Microsoft.AspNetCore.Mvc.Controller",
    ),
    # Response.Redirect → return Redirect
    (
        r'Response\.Redirect\(([^)]+)\);',
        r'return Redirect(\1);',
        "Response.Redirect → return Redirect()",
    ),
    # Request.QueryString → Request.Query
    (
        r'Request\.QueryString\[([^\]]+)\]',
        r'Request.Query[\1]',
        "Request.QueryString → Request.Query",
    ),
    # Request.Form → Request.Form (same but note it)
    (
        r'Request\.Form\[([^\]]+)\]',
        r'Request.Form[\1]',
        "Request.Form access (verify model binding)",
    ),
    # FormsAuthentication → ASP.NET Core Identity
    (
        r'FormsAuthentication\.SetAuthCookie\([^)]+\);',
        r'// TODO: Replace with await HttpContext.SignInAsync(CookieAuthenticationDefaults.AuthenticationScheme, principal);',
        "FormsAuthentication.SetAuthCookie → SignInAsync",
    ),
    (
        r'FormsAuthentication\.SignOut\(\);',
        r'await HttpContext.SignOutAsync(CookieAuthenticationDefaults.AuthenticationScheme);',
        "FormsAuthentication.SignOut → SignOutAsync",
    ),
    # Global.asax Application_Start → Program.cs / Startup
    (
        r'void Application_Start\(',
        r'// Migrated to Program.cs / builder.Services configuration\nvoid Application_Start(',
        "Application_Start → Program.cs note",
    ),
    # BundleConfig → note
    (
        r'BundleConfig\.RegisterBundles\([^)]+\);',
        r'// TODO: Replace BundleConfig with Webpack/Vite bundling',
        "BundleConfig → Webpack/Vite note",
    ),
    # RouteConfig → note
    (
        r'RouteConfig\.RegisterRoutes\([^)]+\);',
        r'// TODO: Routes migrated to attribute routing or minimal API endpoints',
        "RouteConfig → attribute routing note",
    ),
    # FilterConfig → note
    (
        r'FilterConfig\.RegisterGlobalFilters\([^)]+\);',
        r'// TODO: Migrate global filters to ASP.NET Core middleware or filters',
        "FilterConfig → middleware note",
    ),
    # WebApiConfig → note
    (
        r'WebApiConfig\.Register\([^)]+\);',
        r'// TODO: Web API config migrated to ASP.NET Core routing',
        "WebApiConfig → ASP.NET Core routing note",
    ),
    # Thread.CurrentPrincipal → HttpContext.User
    (
        r'Thread\.CurrentPrincipal',
        r'HttpContext.User',
        "Thread.CurrentPrincipal → HttpContext.User",
    ),
    # ObjectCache / MemoryCache (System.Runtime.Caching) → IMemoryCache
    (
        r'using System\.Runtime\.Caching;',
        r'using Microsoft.Extensions.Caching.Memory;',
        "System.Runtime.Caching → Microsoft.Extensions.Caching.Memory",
    ),
    (
        r'\bObjectCache\b',
        r'IMemoryCache',
        "ObjectCache → IMemoryCache",
    ),
    (
        r'\bMemoryCache\.Default\b',
        r'_memoryCache',
        "MemoryCache.Default → injected IMemoryCache",
    ),
]


# ---------------------------------------------------------------------------
# Startup.cs → minimal Program.cs detection
# ---------------------------------------------------------------------------
STARTUP_PATTERN = re.compile(
    r'public\s+class\s+Startup\b',
    re.MULTILINE,
)


def transform_cs_file(source: str, file_path: Path, logs: list[str]) -> tuple[str, list[dict[str, Any]]]:
    """Apply all transformation rules to a single C# file. Returns (transformed_source, applied_fixes)."""
    from migration_agent_cli.core.guardrails import check_cs_file

    result = source
    applied: list[dict[str, Any]] = []

    for pattern, replacement, description in CS_TRANSFORMATION_RULES:
        new_result, count = re.subn(pattern, replacement, result)
        if count:
            applied.append({
                "file": str(file_path),
                "rule": description,
                "occurrences": count,
            })
            result = new_result

    result = _deduplicate_usings(result)
    result = check_cs_file(source, result, file_path.name, logs)
    return result, applied


def _deduplicate_usings(source: str) -> str:
    """Remove duplicate using directives, keeping the first occurrence."""
    lines = source.splitlines(keepends=True)
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('using ') and stripped.endswith(';') and '//' not in stripped:
            if stripped in seen:
                continue
            seen.add(stripped)
        output.append(line)
    return ''.join(output)


def generate_program_cs(startup_path: Path, logs: list[str]) -> str | None:
    """
    If a Startup.cs exists, generate a minimal Program.cs scaffold
    that replaces the old WebHost pattern.
    """
    try:
        startup_text = startup_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    # Extract namespace
    ns_match = re.search(r'namespace\s+([\w.]+)', startup_text)
    namespace = ns_match.group(1) if ns_match else "MyApp"

    # Detect services registered in ConfigureServices
    has_db = "AddDbContext" in startup_text
    has_identity = "AddIdentity" in startup_text or "AddDefaultIdentity" in startup_text
    has_mvc = "AddMvc" in startup_text or "AddControllersWithViews" in startup_text
    has_razor = "AddRazorPages" in startup_text
    has_swagger = "AddSwaggerGen" in startup_text
    has_cors = "AddCors" in startup_text

    lines = [
        "// Auto-generated Program.cs — review and complete before building",
        "// Original Startup.cs has been preserved for reference",
        "",
        "var builder = WebApplication.CreateBuilder(args);",
        "",
        "// Services",
    ]

    if has_db:
        lines.append("builder.Services.AddDbContext<ApplicationDbContext>(options =>");
        lines.append("    options.UseSqlServer(builder.Configuration.GetConnectionString(\"DefaultConnection\")));")
    if has_identity:
        lines.append("builder.Services.AddDefaultIdentity<IdentityUser>(options => options.SignIn.RequireConfirmedAccount = true)")
        lines.append("    .AddEntityFrameworkStores<ApplicationDbContext>();")
    if has_mvc:
        lines.append("builder.Services.AddControllersWithViews();")
    if has_razor:
        lines.append("builder.Services.AddRazorPages();")
    if has_swagger:
        lines.append("builder.Services.AddEndpointsApiExplorer();")
        lines.append("builder.Services.AddSwaggerGen();")
    if has_cors:
        lines.append("builder.Services.AddCors(options => { /* TODO: configure CORS policies */ });")

    lines += [
        "builder.Services.AddHttpContextAccessor();",
        "builder.Services.AddMemoryCache();",
        "",
        "var app = builder.Build();",
        "",
        "// Middleware",
        "if (!app.Environment.IsDevelopment())",
        "{",
        "    app.UseExceptionHandler(\"/Error\");",
        "    app.UseHsts();",
        "}",
        "",
        "app.UseHttpsRedirection();",
        "app.UseStaticFiles();",
        "app.UseRouting();",
    ]

    if has_cors:
        lines.append("app.UseCors();")
    if has_identity:
        lines.append("app.UseAuthentication();")

    lines += [
        "app.UseAuthorization();",
        "",
    ]

    if has_mvc:
        lines.append("app.MapControllerRoute(name: \"default\", pattern: \"{controller=Home}/{action=Index}/{id?}\");")
    if has_razor:
        lines.append("app.MapRazorPages();")
    if has_swagger:
        lines += [
            "if (app.Environment.IsDevelopment())",
            "{",
            "    app.UseSwagger();",
            "    app.UseSwaggerUI();",
            "}",
        ]

    lines += ["", "app.Run();"]
    return "\n".join(lines)
