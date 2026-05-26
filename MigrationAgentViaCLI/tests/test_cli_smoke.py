from pathlib import Path
from typer.testing import CliRunner

from migration_agent_cli.cli import app


def fake_agentic_review(agent_title, agent_description, output):
    return {
        "provider": "test",
        "model": "test-model",
        "framework": "microsoft-agent-framework",
        "response": {"summary": f"Reviewed {agent_title}", "recommendations": [], "risks": [], "manualActions": []},
    }


def test_list_agents_outputs_repository_analysis():
    result = CliRunner().invoke(app, ["list", "agents", "--output", "json"])

    assert result.exit_code == 0
    assert "repository-analysis" in result.stdout


def test_assessment_requires_llm_configuration(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "agent",
            "assessment",
            "--input-json",
            '{"sourcePath":"samples/legacy-dotnet-app","targetFramework":"net8.0"}',
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert "LLM usage is mandatory" in result.stdout


def test_workflow_migrates_sample_copy(tmp_path, monkeypatch):
    monkeypatch.setattr("migration_agent_cli.core.agent_base.run_agentic_review", fake_agentic_review)
    monkeypatch.setattr("migration_agent_cli.orchestrator.orchestrator_agent.run_agentic_review", fake_agentic_review)
    input_file = Path("samples/input/legacy-migration-input.json")
    result = CliRunner().invoke(
        app,
        ["run", "workflow", "--input", str(input_file), "--output-dir", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0
    assert "migrated-source" in result.stdout
    assert list(tmp_path.glob("*/migrated-source/LegacyWebApp/LegacyWebApp.csproj"))
