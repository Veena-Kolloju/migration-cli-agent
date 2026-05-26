from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from migration_agent_cli.core.models import AgentExecutionContext, AgentExecutionResult


def run_dir(context: AgentExecutionContext) -> Path:
    path = Path(context.output_dir) / context.run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(path)


def write_result(context: AgentExecutionContext, result: AgentExecutionResult) -> str:
    folder = run_dir(context) / result.agent_id
    write_json(folder / "input.json", context.input_data)
    write_json(folder / "result.json", result.model_dump(by_alias=True, mode="json"))
    (folder / "logs.txt").write_text("\n".join(result.logs), encoding="utf-8")
    return str(folder)

