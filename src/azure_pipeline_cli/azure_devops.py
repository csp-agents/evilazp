"""Small Azure DevOps REST adapter used by the interactive command shell."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from typing import Any

from .models import AgentRef, PipelineRef, RunResult

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is a declared runtime dependency.
    certifi = None

QUEUE_VARIABLES = ["targetPool", "targetAgent", "commandB64", "runId"]
QUEUE_VARIABLE_DEFAULTS = {
    "targetPool": "Default",
    "targetAgent": "",
    "commandB64": "V3JpdGUtSG9zdCAnYXpwLXNoZWxsIHJlYWR5Jw==",
    "runId": "default",
}
EMPTY_GIT_OBJECT_ID = "0" * 40
PROCESS_TEMPLATE_IDS = {
    "Basic": "b8a3a935-7e91-48b8-a94c-606d37c3e9f2",
    "CMMI": "27450541-8e31-4150-9947-dc59f998fc01",
    "Agile": "adcc42ab-9882-485e-a3ed-7678f01f66bc",
    "Scrum": "6b724908-ef14-45cf-84f8-768b5384da45",
}
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}
OUTPUT_WITHOUT_EXIT_GRACE_SECONDS = 0.5
BUILD_COMPLETED_LOG_GRACE_SECONDS = 2.0


class AzureDevOpsError(RuntimeError):
    """Raised when Azure DevOps CLI or REST calls fail."""


class AzureDevOpsClient:
    """Azure DevOps operations needed by the interactive shell."""

    def __init__(self, organization: str, pat: str | None = None) -> None:
        self.organization = normalize_org_url(organization)
        self.org_name = org_name_from_url(self.organization)
        self.pat = pat or os.environ.get("AZURE_DEVOPS_EXT_PAT")
        if not self.pat:
            raise AzureDevOpsError("PAT is required via --pat or AZURE_DEVOPS_EXT_PAT.")
        self.ssl_context = ssl.create_default_context(cafile=certifi.where() if certifi else None)
        self._pool_cache: list[dict[str, Any]] | None = None
        self._agent_cache: dict[int, list[dict[str, Any]]] = {}
        self._marked_log_cache: dict[tuple[str, int, str], list[int]] = {}

    def list_projects(self) -> list[str]:
        data = self.rest_json("/_apis/projects", {"api-version": "7.1"})
        projects = data.get("value", data if isinstance(data, list) else [])
        return sorted(project["name"] for project in projects if project.get("name"))

    def create_project(self, name: str, visibility: str = "private", process: str = "Basic") -> Any:
        process_template_id = PROCESS_TEMPLATE_IDS.get(process, process)
        operation = self.rest_json(
            "/_apis/projects",
            {"api-version": "7.1"},
            method="POST",
            body={
                "name": name,
                "visibility": visibility,
                "capabilities": {
                    "versioncontrol": {"sourceControlType": "Git"},
                    "processTemplate": {"templateTypeId": process_template_id},
                },
            },
        )
        operation_id = operation.get("id")
        if operation_id:
            self.wait_for_operation(str(operation_id), timeout=300, poll_interval=5)
        return operation

    def get_project(self, name: str) -> dict[str, Any] | None:
        for project in self.rest_json("/_apis/projects", {"api-version": "7.1"}).get("value", []):
            if project.get("name") == name:
                return project
        return None

    def ensure_project(self, name: str, visibility: str = "private", process: str = "Basic") -> dict[str, Any] | None:
        existing = self.get_project(name)
        if existing:
            return existing
        self.create_project(name, visibility, process)
        return self.get_project(name)

    def delete_project(self, name: str) -> None:
        project = self.get_project(name)
        if not project:
            raise AzureDevOpsError(f"Project not found: {name}")
        operation = self.rest_json(
            f"/_apis/projects/{urllib.parse.quote(str(project['id']))}",
            {"api-version": "7.1"},
            method="DELETE",
        )
        operation_id = operation.get("id") if isinstance(operation, dict) else None
        if operation_id:
            self.wait_for_operation(str(operation_id), timeout=300, poll_interval=5)

    def wait_for_operation(self, operation_id: str, timeout: float, poll_interval: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            operation = self.rest_json(
                f"/_apis/operations/{operation_id}",
                {"api-version": "7.1"},
            )
            status = str(operation.get("status") or "").lower()
            if status in {"succeeded", "failed", "cancelled"}:
                if status != "succeeded":
                    raise AzureDevOpsError(f"Azure DevOps operation {operation_id} ended with {status}.")
                return operation
            if time.monotonic() >= deadline:
                raise AzureDevOpsError(f"Timed out waiting for operation {operation_id}.")
            time.sleep(poll_interval)

    def list_pools(self) -> list[dict[str, Any]]:
        if self._pool_cache is not None:
            return self._pool_cache
        data = self.rest_json("/_apis/distributedtask/pools", {"api-version": "7.1"})
        self._pool_cache = data if isinstance(data, list) else data.get("value", [])
        return self._pool_cache

    def clear_pool_cache(self) -> None:
        self._pool_cache = None

    def get_pool(self, name: str) -> dict[str, Any] | None:
        name_lower = name.lower()
        for pool in self.list_pools():
            pool_name = str(pool.get("name") or "")
            if pool_name.lower() == name_lower or str(pool.get("id")) == name:
                return pool
        return None

    def create_pool(self, name: str, pool_type: str = "automation") -> Any:
        created = self.rest_json(
            "/_apis/distributedtask/pools",
            {"api-version": "7.1"},
            method="POST",
            body={"name": name, "poolType": pool_type},
        )
        self._pool_cache = None
        return created

    def ensure_pool(self, name: str, pool_type: str = "automation") -> dict[str, Any]:
        self.clear_pool_cache()
        existing = self.get_pool(name)
        if existing:
            return existing
        return self.create_pool(name, pool_type)

    def delete_pool(self, name: str) -> None:
        pool = self.get_pool(name)
        if not pool:
            raise AzureDevOpsError(f"Agent pool not found: {name}")
        pool_id = int(pool["id"])
        self.rest_request(
            f"/_apis/distributedtask/pools/{pool_id}",
            {"api-version": "7.1"},
            method="DELETE",
        )
        self._pool_cache = None
        self._agent_cache.pop(pool_id, None)

    def list_agents(self, pool_id: int) -> list[dict[str, Any]]:
        if pool_id in self._agent_cache:
            return self._agent_cache[pool_id]
        data = self.rest_json(
            f"/_apis/distributedtask/pools/{pool_id}/agents",
            {"api-version": "7.1", "includeCapabilities": "true"},
        )
        self._agent_cache[pool_id] = data if isinstance(data, list) else data.get("value", [])
        return self._agent_cache[pool_id]

    def delete_agent(self, pool_id: int, agent_id: int) -> None:
        """Remove an Azure Pipelines agent registration from a pool."""

        self.rest_request(
            f"/_apis/distributedtask/pools/{pool_id}/agents/{agent_id}",
            {"api-version": "7.1"},
            method="DELETE",
        )
        self._agent_cache.pop(pool_id, None)

    def clear_agent_cache(self) -> None:
        """Force the next agent discovery to call Azure DevOps again."""

        self._agent_cache = {}

    def discover_agents(self, pool_filter: str | None = None) -> list[AgentRef]:
        agents: list[AgentRef] = []
        pool_filter_lower = pool_filter.lower() if pool_filter else None
        pools = [self.get_pool(pool_filter)] if pool_filter else self.list_pools()
        for pool in [item for item in pools if item]:
            pool_id = int(pool["id"])
            pool_name = pool.get("name") or str(pool_id)
            if pool_filter and pool_filter_lower not in {pool_name.lower(), str(pool_id)}:
                continue
            for agent in self.list_agents(pool_id):
                capabilities = normalize_capabilities(agent.get("systemCapabilities") or {})
                agents.append(
                    AgentRef(
                        id=int(agent["id"]),
                        name=str(agent.get("name") or agent["id"]),
                        pool_id=pool_id,
                        pool_name=pool_name,
                        status=str(agent.get("status") or "unknown"),
                        enabled=bool(agent.get("enabled", True)),
                        version=str(agent["version"]) if agent.get("version") else None,
                        os_description=str(agent["osDescription"]) if agent.get("osDescription") else None,
                        capabilities=capabilities,
                    )
                )
        return sorted(agents, key=lambda item: (item.name.lower(), item.pool_name.lower()))

    def resolve_pool_name(self, pool_or_agent: str) -> str:
        pool = self.get_pool(pool_or_agent)
        if pool:
            return str(pool.get("name") or pool_or_agent)
        for agent in self.discover_agents(None):
            if agent.name.lower() == pool_or_agent.lower():
                return agent.pool_name
        return pool_or_agent

    def list_pipelines(self, project: str) -> list[PipelineRef]:
        definitions = self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/build/definitions",
            {"api-version": "7.1"},
        )
        values = definitions.get("value", [])
        pipelines: list[PipelineRef] = []
        for item in values:
            queue = item.get("queue") or {}
            pool = queue.get("pool") or {}
            pipelines.append(
                PipelineRef(
                    id=int(item["id"]),
                    name=str(item.get("name") or item["id"]),
                    project=project,
                    pool_id=int(pool["id"]) if pool.get("id") is not None else None,
                    pool_name=str(pool["name"]) if pool.get("name") else None,
                )
            )
        return pipelines

    def create_repo(self, project: str, name: str) -> Any:
        return self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/git/repositories",
            {"api-version": "7.1"},
            method="POST",
            body={"name": name},
        )

    def list_repos(self, project: str) -> list[dict[str, Any]]:
        data = self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/git/repositories",
            {"api-version": "7.1"},
        )
        return data.get("value", data if isinstance(data, list) else [])

    def get_repo(self, project: str, name: str) -> dict[str, Any] | None:
        for repo in self.list_repos(project):
            if repo.get("name") == name or str(repo.get("id")) == name:
                return repo
        return None

    def ensure_repo(self, project: str, name: str) -> dict[str, Any]:
        existing = self.get_repo(project, name)
        if existing:
            return existing
        return self.create_repo(project, name)

    def get_ref(self, project: str, repo_id: str, branch: str) -> dict[str, Any] | None:
        data = self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/git/repositories/{repo_id}/refs",
            {
                "api-version": "7.1",
                "filter": f"heads/{branch}",
            },
        )
        refs = data.get("value", [])
        return refs[0] if refs else None

    def push_file(
        self,
        project: str,
        repo_id: str,
        branch: str,
        path: str,
        content: str,
        message: str = "add azure pipeline cli runner",
    ) -> dict[str, Any]:
        ref = self.get_ref(project, repo_id, branch)
        old_object_id = ref.get("objectId") if ref else EMPTY_GIT_OBJECT_ID
        change_type = "add"
        if old_object_id != EMPTY_GIT_OBJECT_ID and self.file_exists(project, repo_id, branch, path):
            change_type = "edit"
        return self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/git/repositories/{repo_id}/pushes",
            {"api-version": "7.1"},
            method="POST",
            body={
                "refUpdates": [
                    {
                        "name": f"refs/heads/{branch}",
                        "oldObjectId": old_object_id,
                    }
                ],
                "commits": [
                    {
                        "comment": message,
                        "changes": [
                            {
                                "changeType": change_type,
                                "item": {"path": "/" + path.lstrip("/")},
                                "newContent": {
                                    "content": content,
                                    "contentType": "rawtext",
                                },
                            }
                        ],
                    }
                ],
            },
        )

    def file_exists(self, project: str, repo_id: str, branch: str, path: str) -> bool:
        try:
            self.rest_text(
                f"/{urllib.parse.quote(project)}/_apis/git/repositories/{repo_id}/items",
                {
                    "api-version": "7.1",
                    "path": "/" + path.lstrip("/"),
                    "versionDescriptor.versionType": "branch",
                    "versionDescriptor.version": branch,
                },
            )
        except AzureDevOpsError as exc:
            if "REST 404" in str(exc):
                return False
            raise
        return True

    def list_queues(self, project: str) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 60
        while True:
            try:
                data = self.rest_json(
                    f"/{urllib.parse.quote(project)}/_apis/distributedtask/queues",
                    {"api-version": "7.1"},
                )
                return data.get("value", data if isinstance(data, list) else [])
            except AzureDevOpsError as exc:
                if "REST 404" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(3)

    def get_queue_for_pool(self, project: str, pool_name: str) -> dict[str, Any] | None:
        pool_name_lower = pool_name.lower()
        for queue in self.list_queues(project):
            pool = queue.get("pool") or {}
            name = str(pool.get("name") or "")
            if name.lower() == pool_name_lower or str(pool.get("id")) == pool_name:
                return queue
        return None

    def ensure_queue_for_pool(self, project: str, pool_name: str) -> dict[str, Any]:
        queue = self.get_queue_for_pool(project, pool_name)
        if queue:
            return queue
        pool = self.get_pool(pool_name)
        if not pool:
            raise AzureDevOpsError(f"Agent pool not found: {pool_name}")
        return self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/distributedtask/queues",
            {"api-version": "7.1", "authorizePipelines": "true"},
            method="POST",
            body={
                "name": str(pool.get("name") or pool_name),
                "pool": {"id": int(pool["id"])},
            },
        )

    def authorize_pipeline_queue(self, project: str, queue_id: int | str, pipeline_id: int) -> None:
        self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/pipelines/pipelinePermissions/queue/{queue_id}",
            {"api-version": "7.1-preview.1"},
            method="PATCH",
            body={"pipelines": [{"id": pipeline_id, "authorized": True}]},
        )

    def open_queue_access(self, project: str, pool_name: str) -> dict[str, Any]:
        queue = self.ensure_queue_for_pool(project, pool_name)
        queue_id = queue["id"]
        return self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/pipelines/pipelinePermissions/queue/{queue_id}",
            {"api-version": "7.1-preview.1"},
            method="PATCH",
            body={
                "resource": {"type": "queue", "id": str(queue_id)},
                "allPipelines": {"authorized": True},
                "pipelines": [],
            },
        )

    def create_pipeline(
        self,
        project: str,
        name: str,
        repository: str,
        branch: str,
        yml_path: str,
        pool_name: str,
    ) -> Any:
        repo = self.get_repo(project, repository)
        if not repo:
            raise AzureDevOpsError(f"Repository not found: {repository}")
        queue = self.ensure_queue_for_pool(project, pool_name)
        return self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/build/definitions",
            {"api-version": "7.1"},
            method="POST",
            body={
                "name": name,
                "path": "\\",
                "type": "build",
                "quality": "definition",
                "queue": queue,
                "process": {"type": 2, "yamlFilename": yml_path},
                "repository": {
                    "id": repo["id"],
                    "name": repo["name"],
                    "type": "TfsGit",
                    "url": repo.get("url"),
                    "defaultBranch": f"refs/heads/{branch}",
                    "clean": None,
                    "checkoutSubmodules": False,
                },
                "variables": {
                    name: {"value": QUEUE_VARIABLE_DEFAULTS[name], "allowOverride": True, "isSecret": False}
                    for name in QUEUE_VARIABLES
                },
                "triggers": [],
            },
        )

    def ensure_pipeline(
        self,
        project: str,
        name: str,
        repository: str,
        branch: str,
        yml_path: str,
        pool_name: str,
        agent_name: str | None = None,
    ) -> PipelineRef:
        queue = self.get_queue_for_pool(project, pool_name)
        for pipeline in self.list_pipelines(project):
            if pipeline.name == name or str(pipeline.id) == name:
                self.ensure_runner_variables(project, pipeline.id, pool_name, agent_name)
                if queue and queue.get("id"):
                    self.authorize_pipeline_queue(project, queue["id"], pipeline.id)
                return pipeline
        created = self.create_pipeline(project, name, repository, branch, yml_path, pool_name)
        pipeline = PipelineRef(
            id=int(created["id"]),
            name=str(created.get("name") or name),
            project=project,
            pool_id=(created.get("queue") or {}).get("pool", {}).get("id"),
            pool_name=(created.get("queue") or {}).get("pool", {}).get("name"),
        )
        self.ensure_runner_variables(project, pipeline.id, pool_name, agent_name)
        created_queue = created.get("queue") or queue or {}
        if created_queue.get("id"):
            self.authorize_pipeline_queue(project, created_queue["id"], pipeline.id)
        return pipeline

    def delete_pipeline(self, project: str, pipeline_id: int) -> None:
        self.rest_request(
            f"/{urllib.parse.quote(project)}/_apis/build/definitions/{pipeline_id}",
            {"api-version": "7.1"},
            method="DELETE",
        )

    def ensure_runner_variables(
        self,
        project: str,
        pipeline_id: int,
        pool_name: str,
        agent_name: str | None = None,
    ) -> None:
        defaults = {
            **QUEUE_VARIABLE_DEFAULTS,
            "targetPool": pool_name,
            "targetAgent": agent_name or "",
        }
        self.ensure_queue_variables(project, pipeline_id, defaults)

    def ensure_queue_variables(
        self,
        project: str,
        pipeline_id: int,
        defaults: dict[str, str] | list[str],
    ) -> None:
        if isinstance(defaults, list):
            defaults = {name: QUEUE_VARIABLE_DEFAULTS[name] for name in defaults}
        definition = self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/build/definitions/{pipeline_id}",
            {"api-version": "7.1", "includeAllProperties": "true"},
        )
        variables = definition.setdefault("variables", {})
        for name, default_value in defaults.items():
            variable = variables.setdefault(name, {})
            if variable.get("value") in (None, "", "placeholder") or name in {"targetPool", "targetAgent"}:
                variable["value"] = default_value
            variable["allowOverride"] = True
            variable["isSecret"] = False
        self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/build/definitions/{pipeline_id}",
            {"api-version": "7.1"},
            method="PUT",
            body=definition,
        )

    def find_pipeline_candidates(
        self,
        agent: AgentRef,
        project_filter: str | None = None,
        pipeline_filter: str | None = None,
    ) -> list[PipelineRef]:
        projects = [project_filter] if project_filter else self.list_projects()
        exact_matches: list[PipelineRef] = []
        fallback_matches: list[PipelineRef] = []
        for project in projects:
            for pipeline in self.list_pipelines(project):
                if pipeline_filter and pipeline_filter not in {pipeline.name, str(pipeline.id)}:
                    continue
                if pipeline_filter:
                    exact_matches.append(pipeline)
                    continue
                if (
                    pipeline.pool_id == agent.pool_id
                    or pipeline.pool_name == agent.pool_name
                ):
                    exact_matches.append(pipeline)
                    continue
                if self.pipeline_has_queue_variables(project, pipeline.id):
                    fallback_matches.append(pipeline)
        candidates = exact_matches or fallback_matches
        return sorted(candidates, key=lambda item: (item.project.lower(), item.name.lower()))

    def pipeline_has_queue_variables(self, project: str, pipeline_id: int) -> bool:
        try:
            definition = self.rest_json(
                f"/{urllib.parse.quote(project)}/_apis/build/definitions/{pipeline_id}",
                {"api-version": "7.1", "includeAllProperties": "true"},
            )
        except AzureDevOpsError:
            return False
        variables = definition.get("variables") or {}
        return all(name in variables for name in QUEUE_VARIABLES)

    def run_pipeline_command(
        self,
        pipeline: PipelineRef,
        agent: AgentRef,
        command_b64: str,
        run_id: str,
        timeout: float,
        poll_interval: float,
    ) -> RunResult:
        command_text = decode_command(command_b64)
        build_id = self.queue_pipeline_command(pipeline, agent, command_b64, run_id)
        print(f"[build {build_id}] waiting for completion")
        return self.wait_pipeline_command(
            pipeline=pipeline,
            build_id=build_id,
            run_id=run_id,
            command_text=command_text,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    def queue_pipeline_command(
        self,
        pipeline: PipelineRef,
        agent: AgentRef,
        command_b64: str,
        run_id: str,
    ) -> int:
        run = self.rest_json(
            f"/{urllib.parse.quote(pipeline.project)}/_apis/build/builds",
            {"api-version": "7.1"},
            method="POST",
            body={
                "definition": {"id": pipeline.id},
                "variables": {
                    "targetPool": {"value": agent.pool_name},
                    "targetAgent": {"value": agent.name},
                    "commandB64": {"value": command_b64},
                    "runId": {"value": run_id},
                },
                "parameters": json.dumps(
                    {
                        "targetPool": agent.pool_name,
                        "targetAgent": agent.name,
                        "commandB64": command_b64,
                        "runId": run_id,
                    }
                ),
            },
        )
        return int(run.get("id") or run.get("buildNumber"))

    def wait_pipeline_command(
        self,
        pipeline: PipelineRef,
        build_id: int,
        run_id: str,
        command_text: str | None,
        timeout: float,
        poll_interval: float,
    ) -> RunResult:
        output, exit_code, tunnel_urls = self.wait_for_marked_output_and_tunnels(
            pipeline.project,
            build_id,
            run_id,
            command_text,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        build = self.get_build(pipeline.project, build_id)
        return RunResult(
            build_id=build_id,
            status=str(build.get("status") or ""),
            result=build.get("result"),
            output=output,
            exit_code=exit_code,
            tunnel_urls=tuple(tunnel_urls),
        )

    def wait_for_marked_output_and_tunnels(
        self,
        project: str,
        build_id: int,
        run_id: str,
        command_text: str | None = None,
        timeout: float = 30,
        poll_interval: float = 1,
    ) -> tuple[str, int | None, list[str]]:
        deadline = time.monotonic() + timeout
        last_output = ""
        last_exit_code: int | None = None
        last_tunnel_urls: list[str] = []
        first_output_at: float | None = None
        first_completed_at: float | None = None
        while True:
            now = time.monotonic()
            output, exit_code, tunnel_urls = self.fetch_marked_output_and_tunnels(project, build_id, run_id, command_text)
            if exit_code is not None:
                return output, exit_code, tunnel_urls
            if output:
                if first_output_at is None:
                    first_output_at = now
                elif now - first_output_at >= OUTPUT_WITHOUT_EXIT_GRACE_SECONDS:
                    return output, exit_code, tunnel_urls
            build = self.get_build(project, build_id)
            build_result = str(build.get("result") or "").lower()
            if build.get("status") == "completed":
                if not output:
                    if first_completed_at is None:
                        first_completed_at = now
                    if now - first_completed_at < BUILD_COMPLETED_LOG_GRACE_SECONDS:
                        time.sleep(poll_interval)
                        continue
                if build_result == "succeeded":
                    return output, 0, tunnel_urls
                if build_result in {"failed", "canceled"}:
                    return output, 1, tunnel_urls
                return output, exit_code, tunnel_urls
            last_output = output
            last_exit_code = exit_code
            last_tunnel_urls = tunnel_urls
            if now >= deadline:
                return last_output, last_exit_code, last_tunnel_urls
            time.sleep(poll_interval)

    def wait_for_marked_output(
        self,
        project: str,
        build_id: int,
        run_id: str,
        command_text: str | None = None,
        timeout: float = 30,
        poll_interval: float = 1,
    ) -> tuple[str, int | None]:
        deadline = time.monotonic() + timeout
        last_output = ""
        last_exit_code: int | None = None
        first_output_at: float | None = None
        first_completed_at: float | None = None
        while True:
            now = time.monotonic()
            output, exit_code = self.fetch_marked_output(project, build_id, run_id, command_text)
            if exit_code is not None:
                return output, exit_code
            if output:
                if first_output_at is None:
                    first_output_at = now
                elif now - first_output_at >= OUTPUT_WITHOUT_EXIT_GRACE_SECONDS:
                    return output, exit_code
            build = self.get_build(project, build_id)
            build_result = str(build.get("result") or "").lower()
            if build.get("status") == "completed":
                if not output:
                    if first_completed_at is None:
                        first_completed_at = now
                    if now - first_completed_at < BUILD_COMPLETED_LOG_GRACE_SECONDS:
                        time.sleep(poll_interval)
                        continue
                if build_result == "succeeded":
                    return output, 0
                if build_result in {"failed", "canceled"}:
                    return output, 1
                return output, exit_code
            last_output = output
            last_exit_code = exit_code
            if now >= deadline:
                return last_output, last_exit_code
            time.sleep(poll_interval)

    def get_build(self, project: str, build_id: int) -> dict[str, Any]:
        return self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/build/builds/{build_id}",
            {"api-version": "7.1"},
        )

    def wait_for_build(
        self,
        project: str,
        build_id: int,
        timeout: float,
        poll_interval: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            build = self.get_build(project, build_id)
            if build.get("status") == "completed":
                return build
            if time.monotonic() >= deadline:
                raise AzureDevOpsError(f"Timed out waiting for build {build_id}.")
            time.sleep(poll_interval)

    def fetch_marked_output(
        self,
        project: str,
        build_id: int,
        run_id: str,
        command_text: str | None = None,
    ) -> tuple[str, int | None]:
        output, exit_code, _tunnel_urls = self.fetch_marked_output_and_tunnels(project, build_id, run_id, command_text)
        return output, exit_code

    def fetch_marked_output_and_tunnels(
        self,
        project: str,
        build_id: int,
        run_id: str,
        command_text: str | None = None,
    ) -> tuple[str, int | None, list[str]]:
        logs = self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/build/builds/{build_id}/logs",
            {"api-version": "7.1"},
        )
        log_ids = self._ordered_build_log_ids(project, build_id, run_id, logs.get("value", []))
        collected: list[str] = []
        seen: set[str] = set()
        exit_code: int | None = None
        tunnel_urls: list[str] = []
        seen_tunnel_urls: set[str] = set()
        for log_id in log_ids:
            try:
                text = self.rest_text_with_retry(
                    f"/{urllib.parse.quote(project)}/_apis/build/builds/{build_id}/logs/{log_id}",
                    {"api-version": "7.1"},
                    attempts=2,
                    delay=0.25,
                )
            except AzureDevOpsError:
                continue
            extracted, found_exit = extract_marked_output(text, run_id, command_text)
            if extracted and extracted not in seen:
                collected.append(extracted)
                seen.add(extracted)
                self._remember_marked_log(project, build_id, run_id, log_id)
            if found_exit is not None:
                exit_code = found_exit
                self._remember_marked_log(project, build_id, run_id, log_id)
            for url in extract_tunnel_urls(text):
                if url not in seen_tunnel_urls:
                    tunnel_urls.append(url)
                    seen_tunnel_urls.add(url)
            if exit_code is not None:
                break
        return "\n".join(part for part in collected if part).strip(), exit_code, tunnel_urls

    def _ordered_build_log_ids(self, project: str, build_id: int, run_id: str, logs: list[dict[str, Any]]) -> list[int]:
        ids = [int(log["id"]) for log in logs if log.get("id") is not None]
        cached = [log_id for log_id in self._marked_log_cache.get((project, build_id, run_id), []) if log_id in ids]
        recent_first = list(reversed(ids))
        return cached + [log_id for log_id in recent_first if log_id not in cached]

    def _remember_marked_log(self, project: str, build_id: int, run_id: str, log_id: int) -> None:
        key = (project, build_id, run_id)
        current = [item for item in self._marked_log_cache.get(key, []) if item != log_id]
        self._marked_log_cache[key] = [log_id] + current[:4]

    def fetch_tunnel_urls(self, project: str, build_id: int) -> list[str]:
        logs = self.rest_json(
            f"/{urllib.parse.quote(project)}/_apis/build/builds/{build_id}/logs",
            {"api-version": "7.1"},
        )
        urls: list[str] = []
        seen: set[str] = set()
        for log in logs.get("value", []):
            log_id = log.get("id")
            if log_id is None:
                continue
            try:
                text = self.rest_text_with_retry(
                    f"/{urllib.parse.quote(project)}/_apis/build/builds/{build_id}/logs/{log_id}",
                    {"api-version": "7.1"},
                )
            except AzureDevOpsError:
                continue
            for url in extract_tunnel_urls(text):
                if url not in seen:
                    urls.append(url)
                    seen.add(url)
        return urls

    def rest_json(
        self,
        path: str,
        query: dict[str, str],
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response_body = self.rest_request(path, query, method=method, body=body)
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise AzureDevOpsError(f"Azure DevOps REST returned non-JSON for {path}") from exc

    def rest_text(self, path: str, query: dict[str, str]) -> str:
        return self.rest_request(path, query, accept="text/plain")

    def rest_text_with_retry(
        self,
        path: str,
        query: dict[str, str],
        attempts: int = 5,
        delay: float = 2,
    ) -> str:
        last_error: AzureDevOpsError | None = None
        for attempt in range(attempts):
            try:
                return self.rest_text(path, query)
            except AzureDevOpsError as exc:
                last_error = exc
                if not is_retryable_rest_error(str(exc)) or attempt == attempts - 1:
                    raise
                time.sleep(delay * (attempt + 1))
        raise last_error or AzureDevOpsError(f"Failed to fetch {path}")

    def rest_request(
        self,
        path: str,
        query: dict[str, str],
        method: str = "GET",
        body: dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> str:
        qs = urllib.parse.urlencode(query)
        url = f"https://dev.azure.com/{self.org_name}{path}?{qs}"
        token = base64.b64encode(f":{self.pat}".encode()).decode()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Basic {token}",
                "Accept": accept,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=self.ssl_context) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            summary = summarize_error_body(detail)
            raise AzureDevOpsError(format_rest_error(exc.code, path, summary)) from exc
        except urllib.error.URLError as exc:
            raise AzureDevOpsError(f"Azure DevOps REST failed for {path}: {exc}") from exc

    def bind_agent_to_pipeline(
        self,
        agent: AgentRef,
        pipeline: PipelineRef,
    ) -> AgentRef:
        return replace(
            agent,
            project=pipeline.project,
            pipeline_id=pipeline.id,
            pipeline_name=pipeline.name,
        )


def normalize_org_url(value: str) -> str:
    if value.startswith("https://dev.azure.com/"):
        return value.rstrip("/")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        return f"https://dev.azure.com/{value}"
    raise AzureDevOpsError("Organization must be an org name or https://dev.azure.com/<org> URL.")


def org_name_from_url(value: str) -> str:
    parsed = urllib.parse.urlparse(normalize_org_url(value))
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise AzureDevOpsError("Organization URL must include the organization name.")
    return parts[0]


def normalize_capabilities(value: dict[str, Any]) -> dict[str, str]:
    """Normalize agent capability values to strings for quick local lookups."""

    capabilities: dict[str, str] = {}
    for key, raw in value.items():
        if raw is None:
            continue
        capabilities[str(key)] = str(raw)
    return capabilities


def redact_secret(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def is_retryable_rest_error(message: str) -> bool:
    match = re.search(r"REST (\d{3})", message)
    return bool(match and int(match.group(1)) in RETRYABLE_HTTP_STATUS)


def format_rest_error(status_code: int, path: str, summary: str) -> str:
    """Return operator-facing Azure DevOps REST failure guidance."""

    if status_code == 503:
        return (
            f"Azure DevOps REST 503 for {path}: Azure DevOps service is temporarily unavailable. "
            "Retry the command shortly."
        )
    if status_code == 429:
        return f"Azure DevOps REST 429 for {path}: request rate-limited by Azure DevOps. Wait briefly and retry."
    if status_code in {500, 502, 504}:
        return f"Azure DevOps REST {status_code} for {path}: transient Azure DevOps server error. Retry shortly."
    return f"Azure DevOps REST {status_code} for {path}: {summary}"


def summarize_error_body(body: str, limit: int = 500) -> str:
    body = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", body, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", " ", body)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "empty response"
    if len(cleaned) > limit:
        return cleaned[:limit] + "..."
    return cleaned


def extract_marked_output(text: str, run_id: str, command_text: str | None = None) -> tuple[str, int | None]:
    start = f"::EVILAZP_START::{run_id}"
    end_prefix = f"::EVILAZP_END::{run_id}::EXIT::"
    lines = text.splitlines()
    inside = False
    collect_trailing_error = False
    output: list[str] = []
    exit_code: int | None = None
    for line in lines:
        cleaned = strip_azure_log_prefix(line).strip()
        if cleaned == start:
            inside = True
            collect_trailing_error = False
            output = []
            continue
        if inside and cleaned.startswith(end_prefix):
            inside = False
            raw_exit = cleaned.removeprefix(end_prefix).strip()
            if raw_exit.lstrip("-").isdigit():
                exit_code = int(raw_exit)
                collect_trailing_error = exit_code != 0 and not output
            continue
        if inside:
            output.append(strip_azure_log_prefix(line).rstrip())
            continue
        if collect_trailing_error:
            if cleaned.startswith("##[section]") or cleaned.startswith("##[error]ProcessCompletedWithExitCode"):
                collect_trailing_error = False
                continue
            if cleaned:
                output.append(strip_azure_log_prefix(line).rstrip())
    return clean_command_echo(output, command_text), exit_code


def extract_tunnel_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        cleaned = strip_azure_log_prefix(line)
        if "Tunnel active" not in cleaned and "::EVILAZP_TUNNEL::" not in cleaned:
            continue
        for match in re.findall(r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*devtunnels\.ms[^\s\"'<>)]*", cleaned):
            url = match.rstrip(".,;")
            if url not in seen:
                urls.append(url)
                seen.add(url)
    return urls


def strip_azure_log_prefix(line: str) -> str:
    return re.sub(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z\s+", "", line)


def encode_command(command: str) -> str:
    return base64.b64encode(command.encode("utf-8")).decode("ascii")


def decode_command(command_b64: str) -> str:
    return base64.b64decode(command_b64.encode("ascii")).decode("utf-8", errors="replace")


def clean_command_echo(lines: list[str], command_text: str | None) -> str:
    cleaned = [line for line in lines]
    if command_text and cleaned and cleaned[0].strip().strip('"') == command_text.strip().strip('"'):
        cleaned = cleaned[1:]
    return "\n".join(cleaned).strip()
