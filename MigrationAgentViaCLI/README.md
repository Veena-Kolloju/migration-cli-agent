# MigrationAgentViaCLI

Python-first .NET Migration Agent solution that can run from Command Prompt, PowerShell, FastAPI, or a VS Code extension wrapper.

## Stack

- Python execution engine
- Typer CLI
- FastAPI service
- Microsoft Agent Framework dependency: `agent-framework`
- Groq LLM adapter: `groq`
- Pydantic contracts
- VS Code extension shell that calls the CLI

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .[dev]
```

If you install into the user Python instead of a virtual environment, Windows may place
`migration-agent.exe` under `%APPDATA%\Python\Python314\Scripts`. If that folder is not
on `PATH`, run commands with `python -m migration_agent_cli.cli` instead.

## CLI

```powershell
migration-agent list agents
migration-agent describe repository-analysis
migration-agent run agent repository-analysis --input samples\input\repository-analysis-input.json
migration-agent run workflow --input samples\input\workflow-input.json
migration-agent validate manifest --file manifests\repository-analysis.agent.json
migration-agent package marketplace --output-dir artifacts\marketplace
```

Equivalent module form:

```powershell
python -m migration_agent_cli.cli list agents
python -m migration_agent_cli.cli run workflow --input samples\input\legacy-migration-input.json --format json
```

## Test a legacy application migration

Edit or create a workflow input JSON with your legacy app path:

```json
{
  "workflowId": "dotnet-migration",
  "sourcePath": "C:\\path\\to\\legacy-app",
  "targetFramework": "net8.0",
  "agents": [
    "repository-analysis",
    "assessment",
    "dependency-analysis",
    "code-analysis",
    "project-conversion",
    "configuration-migration",
    "report-generation"
  ],
  "outputDir": "artifacts",
  "dryRun": false,
  "continueOnError": true
}
```

Then run:

```powershell
python -m migration_agent_cli.cli run workflow --input .\my-migration-input.json --format json
```

The original source is not overwritten. When `dryRun` is `false`, the converted copy is
created under `artifacts\<run-id>\migrated-source`, with a report at
`artifacts\<run-id>\migration-report.md`.

## FastAPI

```powershell
python run_fastapi.py
```

Open `http://127.0.0.1:8065/docs`.

## Mandatory Agentic Runtime

This solution runs each migration agent through Microsoft Agent Framework and an
LLM review step. Agent runs fail if the LLM provider is not configured.

Default provider is Groq through the Microsoft Agent Framework OpenAI-compatible
chat client:

```powershell
$env:GROQ_API_KEY="your-key"
$env:GROQ_MODEL="llama-3.3-70b-versatile"
```

Command Prompt equivalent:

```bat
set GROQ_API_KEY=your-key
set GROQ_MODEL=llama-3.3-70b-versatile
```

You can also use OpenAI:

```bat
set MIGRATION_AGENT_LLM_PROVIDER=openai
set OPENAI_API_KEY=your-key
set OPENAI_MODEL=gpt-4.1-mini
```
