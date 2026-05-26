from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import typer
from jsonschema import validate as jsonschema_validate
from rich.console import Console
from rich.table import Table

from migration_agent_cli.core.artifacts import write_json, write_result
from migration_agent_cli.core.models import AgentExecutionContext
from migration_agent_cli.orchestrator.orchestrator_agent import OrchestratorAgent
from migration_agent_cli.registry import all_agents, get_agent

app = typer.Typer(help="Migration Agent CLI")
list_app = typer.Typer(help="List available assets")
run_app = typer.Typer(help="Run agents or workflows")
validate_app = typer.Typer(help="Validate manifests or input files")
package_app = typer.Typer(help="Create marketplace packages")
app.add_typer(list_app, name="list")
app.add_typer(run_app, name="run")
app.add_typer(validate_app, name="validate")
app.add_typer(package_app, name="package")
console = Console()


def read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: dict) -> None:
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


@list_app.command("agents")
def list_agents(output: str = typer.Option("table", "--output", "-o")) -> None:
    agents = all_agents()
    if output == "json":
        console.print_json(data=[{"agentId": a.agent_id, "title": a.title, "description": a.description} for a in agents.values()])
        return
    table = Table(title="Available Migration Agents")
    table.add_column("Agent ID")
    table.add_column("Title")
    table.add_column("Description")
    table.add_row("orchestrator", "Orchestrator Agent", "Runs selected agents in workflow order.")
    for agent in agents.values():
        table.add_row(agent.agent_id, agent.title, agent.description)
    console.print(table)


@app.command()
def describe(agent_id: str, output: str = typer.Option("text", "--output", "-o")) -> None:
    if agent_id == "orchestrator":
        data = {"agentId": "orchestrator", "title": "Orchestrator Agent", "description": "Coordinates workflow execution."}
    else:
        agent = get_agent(agent_id)
        data = {"agentId": agent.agent_id, "title": agent.title, "description": agent.description, "capabilities": getattr(agent, "capabilities", [])}
    if output == "json":
        console.print_json(data=data)
    else:
        console.print(f"[bold]{data['title']}[/bold]\n{data['description']}")
        for capability in data.get("capabilities", []):
            console.print(f"- {capability}")


@run_app.command("agent")
def run_agent(
    agent_id: str,
    input: Optional[str] = typer.Option(None, "--input", "-i"),
    input_json: Optional[str] = typer.Option(None, "--input-json"),
    output_dir: str = typer.Option("artifacts", "--output-dir"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "--verbose"),
    format: str = typer.Option("text", "--format"),
) -> None:
    if input_json:
        data = json.loads(input_json)
    elif input:
        data = read_json(input)
    else:
        raise typer.BadParameter("Provide --input or --input-json.")
    try:
        agent = get_agent(agent_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    context = AgentExecutionContext(agent_id=agent_id, input_data=data, output_dir=output_dir, dry_run=dry_run, verbose=verbose)
    result = agent.execute(context)
    folder = write_result(context, result)
    payload = result.model_dump(mode="json")
    payload["artifactFolder"] = folder
    if format == "json":
        console.print_json(data=payload)
    else:
        console.print(f"{agent.title}: {result.status}")
        console.print(f"Artifacts: {folder}")
    if result.status == "failed":
        raise typer.Exit(1)


@run_app.command("workflow")
def run_workflow(
    input: str = typer.Option(..., "--input", "-i"),
    output_dir: Optional[str] = typer.Option(None, "--output-dir"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "--verbose"),
    format: str = typer.Option("text", "--format"),
) -> None:
    data = read_json(input)
    if data.get("sourcePath"):
        source_path = Path(data["sourcePath"])
        if not source_path.is_absolute():
            data["sourcePath"] = str((Path(input).resolve().parent / source_path).resolve())
    context = AgentExecutionContext(
        agent_id="orchestrator",
        input_data=data,
        output_dir=output_dir or data.get("outputDir", "artifacts"),
        dry_run=dry_run or data.get("dryRun", False),
        verbose=verbose,
    )
    result = OrchestratorAgent().execute(context)
    folder = write_result(context, result)
    payload = result.model_dump(mode="json")
    payload["artifactFolder"] = folder
    if format == "json":
        console.print_json(data=payload)
    else:
        console.print(f"Workflow: {result.status}")
        console.print(f"Artifacts: {folder}")
    if result.status == "failed":
        raise typer.Exit(1)


@validate_app.command("manifest")
def validate_manifest(file: str = typer.Option(..., "--file"), schema: str = typer.Option("schemas/agent-metadata.schema.json", "--schema")) -> None:
    manifest = read_json(file)
    schema_path = Path(schema)
    if schema_path.exists():
        jsonschema_validate(manifest, read_json(schema_path))
    required = ["title", "description", "icon", "capabilities", "techstack", "uiSpec", "apiSpec", "category", "subCategory"]
    missing = [key for key in required if key not in manifest]
    if missing:
        raise typer.BadParameter(f"Missing manifest fields: {', '.join(missing)}")
    console.print(f"Manifest valid: {file}")


@package_app.command("marketplace")
def package_marketplace(
    output_dir: str = typer.Option(..., "--output-dir"),
    manifest_dir: str = typer.Option("manifests", "--manifest-dir"),
    workflow_dir: str = typer.Option("workflows", "--workflow-dir"),
    include_samples: bool = typer.Option(False, "--include-samples"),
) -> None:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    for folder_name in (manifest_dir, workflow_dir):
        source = Path(folder_name)
        if source.exists():
            target = destination / source.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
    if include_samples and Path("samples").exists():
        target = destination / "samples"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree("samples", target)
    write_json(destination / "package.json", {"name": "dotnet-migration-agent", "type": "agentic-marketplace-package"})
    console.print(f"Marketplace package created: {destination}")


if __name__ == "__main__":
    app()
