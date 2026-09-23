"""Shared data structures for Azure DevOps discovery and execution."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentRef:
    """A self-hosted Azure Pipelines agent discovered from an agent pool."""

    id: int
    name: str
    pool_id: int
    pool_name: str
    status: str
    enabled: bool
    version: str | None = None
    os_description: str | None = None
    capabilities: dict[str, str] = field(default_factory=dict)
    project: str | None = None
    pipeline_id: int | None = None
    pipeline_name: str | None = None

    @property
    def online(self) -> bool:
        return self.status.lower() == "online" and self.enabled


@dataclass(frozen=True)
class PipelineRef:
    """An Azure DevOps build pipeline candidate."""

    id: int
    name: str
    project: str
    pool_id: int | None = None
    pool_name: str | None = None


@dataclass(frozen=True)
class RunResult:
    """Result of a completed pipeline command run."""

    build_id: int
    status: str
    result: str | None
    output: str
    exit_code: int | None
    tunnel_urls: tuple[str, ...] = ()
