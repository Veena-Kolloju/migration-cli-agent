# .NET Migration Agent VS Code Extension

This extension is intentionally thin. It calls the `migration-agent` CLI so VS Code and command prompt execution share the same runtime.

## Commands

- `Migration Agent: List Agents`
- `Migration Agent: Run Repository Analysis`
- `Migration Agent: Run Full Workflow`
- `Migration Agent: Open Artifacts Folder`

## Required CLI

Install the Python project from the repository root:

```powershell
pip install -e .
```

