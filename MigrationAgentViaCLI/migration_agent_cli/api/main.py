from __future__ import annotations

from fastapi import FastAPI, HTTPException

from migration_agent_cli.core.artifacts import write_result
from migration_agent_cli.core.models import AgentExecutionContext
from migration_agent_cli.orchestrator.orchestrator_agent import OrchestratorAgent
from migration_agent_cli.registry import all_agents, get_agent

app = FastAPI(title="Migration Agent API", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/agents")
def agents() -> list[dict]:
    return [{"agentId": agent.agent_id, "title": agent.title, "description": agent.description} for agent in all_agents().values()]


@app.get("/agents/{agent_id}")
def describe_agent(agent_id: str) -> dict:
    try:
        agent = get_agent(agent_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"agentId": agent.agent_id, "title": agent.title, "description": agent.description, "capabilities": getattr(agent, "capabilities", [])}


@app.post("/agents/{agent_id}/run")
def run_agent(agent_id: str, payload: dict) -> dict:
    try:
        agent = get_agent(agent_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    context = AgentExecutionContext(agent_id=agent_id, input_data=payload, output_dir=payload.get("outputDir", "artifacts"), dry_run=payload.get("dryRun", False))
    result = agent.execute(context)
    folder = write_result(context, result)
    data = result.model_dump(mode="json")
    data["artifactFolder"] = folder
    return data


@app.post("/workflow/run")
def run_workflow(payload: dict) -> dict:
    context = AgentExecutionContext(agent_id="orchestrator", input_data=payload, output_dir=payload.get("outputDir", "artifacts"), dry_run=payload.get("dryRun", False))
    result = OrchestratorAgent().execute(context)
    folder = write_result(context, result)
    data = result.model_dump(mode="json")
    data["artifactFolder"] = folder
    return data

