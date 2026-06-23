from __future__ import annotations

from migration_agent_cli.agents.assessment import AssessmentAgent
from migration_agent_cli.agents.code_analysis import CodeAnalysisAgent
from migration_agent_cli.agents.dependency_analysis import DependencyAnalysisAgent
from migration_agent_cli.agents.repository_analysis import RepositoryAnalysisAgent
from migration_agent_cli.agents.frontend_migration import FrontendMigrationAgent
from migration_agent_cli.agents.api_transformation import ApiTransformationAgent
from migration_agent_cli.agents.ef_migration import EfMigrationAgent
from migration_agent_cli.agents.auth_transformation import AuthTransformationAgent
from migration_agent_cli.agents.simple_agents import (
    BuildFixAgent,
    BuildValidationAgent,
    CodeTransformationAgent,
    ConfigurationMigrationAgent,
    ProjectConversionAgent,
    ReportGenerationAgent,
    TestValidationAgent,
)
from migration_agent_cli.core.agent_base import MigrationAgent


def all_agents() -> dict[str, MigrationAgent]:
    agents: list[MigrationAgent] = [
        RepositoryAnalysisAgent(),
        AssessmentAgent(),
        DependencyAnalysisAgent(),
        CodeAnalysisAgent(),
        ProjectConversionAgent(),
        EfMigrationAgent(),
        ApiTransformationAgent(),
        AuthTransformationAgent(),
        CodeTransformationAgent(),
        ConfigurationMigrationAgent(),
        FrontendMigrationAgent(),
        BuildValidationAgent(),
        BuildFixAgent(),
        TestValidationAgent(),
        ReportGenerationAgent(),
    ]
    return {agent.agent_id: agent for agent in agents}


def get_agent(agent_id: str) -> MigrationAgent:
    agents = all_agents()
    if agent_id not in agents:
        valid = ", ".join(sorted(agents))
        raise KeyError(f"Unknown agent '{agent_id}'. Valid agents: {valid}")
    return agents[agent_id]

