from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from migration_agent_cli.core.artifacts import write_result
from migration_agent_cli.core.agent_base import MigrationAgent
from migration_agent_cli.core.models import AgentExecutionContext, AgentExecutionResult
from migration_agent_cli.llm.microsoft_agent_adapter import run_agentic_review
from migration_agent_cli.registry import get_agent


class OrchestratorAgent(MigrationAgent):
    agent_id = "orchestrator"
    title = "Orchestrator Agent"
    description = "Coordinates selected .NET migration agents in workflow order."

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        started = datetime.now(timezone.utc)
        logs = ["Starting .NET migration workflow."]
        shared_state: dict[str, Any] = {}
        agent_results: list[dict[str, Any]] = []
        agent_ids = context.input_data.get("agents", [])
        continue_on_error = context.input_data.get("continueOnError", True)

        for agent_id in agent_ids:
            logs.append(f"Running {agent_id}.")
            agent = get_agent(agent_id)
            child_input = dict(context.input_data)
            child_context = AgentExecutionContext(
                run_id=context.run_id,
                agent_id=agent_id,
                input_data=child_input,
                shared_state=shared_state,
                output_dir=context.output_dir,
                dry_run=context.dry_run,
                verbose=context.verbose,
            )
            result = agent.execute(child_context)
            write_result(child_context, result)
            shared_state[agent_id] = result.output
            agent_results.append(result.model_dump(mode="json"))
            logs.extend([f"{agent_id}: {line}" for line in result.logs])
            if result.status == "failed" and not continue_on_error:
                logs.append(f"Stopping workflow because {agent_id} failed.")
                break

        failed_count = sum(1 for result in agent_results if result.get("status") == "failed")
        output = {
            "workflowStatus": "failed" if failed_count else "completed",
            "agentResults": agent_results,
            "summary": {"agentsRequested": len(agent_ids), "agentsExecuted": len(agent_results), "failed": failed_count},
        }
        if not failed_count:
            logs.append("Running mandatory Microsoft Agent Framework LLM workflow review.")
            try:
                output["agenticReview"] = run_agentic_review(self.title, self.description, output)
            except Exception as exc:
                logs.append(f"Failed {self.title}: {exc}")
                return AgentExecutionResult(
                    run_id=context.run_id,
                    agent_id=context.agent_id,
                    status="failed",
                    started_at=started,
                    completed_at=datetime.now(timezone.utc),
                    logs=logs,
                    output=output,
                    error=str(exc),
                )
        logs.append("Completed .NET migration workflow.")
        return AgentExecutionResult.completed(
            context,
            started,
            logs,
            output,
            status="failed" if failed_count else "completed",
        )
