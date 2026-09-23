import json

from azure_pipeline_cli.azure_devops import (
    AzureDevOpsClient,
    AzureDevOpsError,
    AgentRef,
    clean_command_echo,
    decode_command,
    encode_command,
    extract_marked_output,
    extract_tunnel_urls,
    format_rest_error,
    is_retryable_rest_error,
    normalize_org_url,
    org_name_from_url,
    redact_secret,
    summarize_error_body,
)
from azure_pipeline_cli.pipeline_yaml import render_pipeline_yaml


def test_normalize_org_url_accepts_org_name():
    assert normalize_org_url("mick3y") == "https://dev.azure.com/mick3y"


def test_org_name_from_url():
    assert org_name_from_url("https://dev.azure.com/mick3y") == "mick3y"


def test_redact_secret():
    assert redact_secret("token abc failed", "abc") == "token *** failed"


def test_client_requires_pat(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    try:
        AzureDevOpsClient("mick3y")
    except AzureDevOpsError as exc:
        assert "PAT is required" in str(exc)
    else:
        raise AssertionError("expected AzureDevOpsError")


def test_client_accepts_explicit_pat():
    client = AzureDevOpsClient("mick3y", pat="token")
    assert client.org_name == "mick3y"


def test_push_file_uses_initial_branch_object_id(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []

    def fake_rest_json(path, query, method="GET", body=None):
        calls.append((path, query, method, body))
        if path.endswith("/refs"):
            return {"value": []}
        return {"ok": True}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)

    client.push_file("proj", "repo-id", "main", "azure-pipelines.yml", render_pipeline_yaml())

    push_body = calls[-1][3]
    assert push_body["refUpdates"][0]["oldObjectId"] == "0" * 40
    assert push_body["commits"][0]["changes"][0]["changeType"] == "add"


def test_push_file_edits_existing_file(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []

    def fake_rest_json(path, query, method="GET", body=None):
        calls.append((path, query, method, body))
        if path.endswith("/refs"):
            return {"value": [{"objectId": "a" * 40}]}
        return {"ok": True}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)
    monkeypatch.setattr(client, "file_exists", lambda *args: True)

    client.push_file("proj", "repo-id", "main", "azure-pipelines.yml", render_pipeline_yaml())

    push_body = calls[-1][3]
    assert push_body["refUpdates"][0]["oldObjectId"] == "a" * 40
    assert push_body["commits"][0]["changes"][0]["changeType"] == "edit"


def test_get_pool_is_case_insensitive(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    monkeypatch.setattr(client, "list_pools", lambda: [{"id": 10, "name": "WINVM01"}])

    assert client.get_pool("winvm01") == {"id": 10, "name": "WINVM01"}


def test_delete_project_uses_project_id_and_waits(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []

    monkeypatch.setattr(client, "get_project", lambda name: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "wait_for_operation", lambda operation_id, timeout, poll_interval: calls.append(("wait", operation_id, timeout, poll_interval)))

    def fake_rest_json(path, query, method="GET", body=None):
        calls.append((path, query, method, body))
        return {"id": "operation-id"}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)

    client.delete_project("proj")

    assert calls == [
        ("/_apis/projects/project-id", {"api-version": "7.1"}, "DELETE", None),
        ("wait", "operation-id", 300, 5),
    ]


def test_discover_agents_uses_single_filtered_pool(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []

    monkeypatch.setattr(client, "list_pools", lambda: [{"id": 1, "name": "Default"}, {"id": 2, "name": "Other"}])

    def fake_list_agents(pool_id):
        calls.append(pool_id)
        return [{"id": 7, "name": "WINVM01", "status": "online", "enabled": True}]

    monkeypatch.setattr(client, "list_agents", fake_list_agents)

    agents = client.discover_agents("default")

    assert calls == [1]
    assert agents[0].name == "WINVM01"


def test_list_agents_requests_capabilities(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []

    def fake_rest_json(path, query, method="GET", body=None):
        calls.append((path, query))
        return {"value": []}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)

    assert client.list_agents(10) == []
    assert calls == [
        (
            "/_apis/distributedtask/pools/10/agents",
            {"api-version": "7.1", "includeCapabilities": "true"},
        )
    ]


def test_delete_agent_uses_distributed_task_delete(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []
    client._agent_cache[10] = [{"id": 7}]

    def fake_rest_request(path, query, method="GET", body=None, accept="application/json"):
        calls.append((path, query, method, body, accept))
        return ""

    monkeypatch.setattr(client, "rest_request", fake_rest_request)

    client.delete_agent(10, 7)

    assert calls == [
        (
            "/_apis/distributedtask/pools/10/agents/7",
            {"api-version": "7.1"},
            "DELETE",
            None,
            "application/json",
        )
    ]
    assert 10 not in client._agent_cache


def test_delete_pool_uses_distributed_task_pool_delete(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []
    client._pool_cache = [{"id": 10, "name": "pool"}]
    client._agent_cache[10] = [{"id": 7}]

    def fake_rest_request(path, query, method="GET", body=None, accept="application/json"):
        calls.append((path, query, method, body, accept))
        return ""

    monkeypatch.setattr(client, "rest_request", fake_rest_request)

    client.delete_pool("pool")

    assert calls == [
        (
            "/_apis/distributedtask/pools/10",
            {"api-version": "7.1"},
            "DELETE",
            None,
            "application/json",
        )
    ]
    assert client._pool_cache is None
    assert 10 not in client._agent_cache


def test_ensure_pool_refreshes_stale_pool_cache(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    client._pool_cache = [{"id": 14, "name": "evilazp-pool"}]
    calls = []

    def fake_rest_json(path, query, method="GET", body=None):
        calls.append((path, method, body))
        if method == "GET":
            return {"value": []}
        return {"id": 17, "name": body["name"]}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)

    pool = client.ensure_pool("evilazp-pool")

    assert pool == {"id": 17, "name": "evilazp-pool"}
    assert calls == [
        ("/_apis/distributedtask/pools", "GET", None),
        ("/_apis/distributedtask/pools", "POST", {"name": "evilazp-pool", "poolType": "automation"}),
    ]


def test_delete_pipeline_uses_build_definition_delete(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []

    def fake_rest_request(path, query, method="GET", body=None, accept="application/json"):
        calls.append((path, query, method, body, accept))
        return ""

    monkeypatch.setattr(client, "rest_request", fake_rest_request)

    client.delete_pipeline("proj", 7)

    assert calls == [
        (
            "/proj/_apis/build/definitions/7",
            {"api-version": "7.1"},
            "DELETE",
            None,
            "application/json",
        )
    ]


def test_list_queues_uses_distributed_task_endpoint(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []

    def fake_rest_json(path, query, method="GET", body=None):
        calls.append((path, query, method, body))
        return {"value": [{"id": 3, "pool": {"id": 10, "name": "pool"}}]}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)

    assert client.list_queues("proj")[0]["id"] == 3
    assert calls == [
        (
            "/proj/_apis/distributedtask/queues",
            {"api-version": "7.1"},
            "GET",
            None,
        )
    ]


def test_ensure_queue_for_pool_creates_project_queue(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []

    def fake_rest_json(path, query, method="GET", body=None):
        calls.append((path, query, method, body))
        if method == "GET":
            return {"value": []}
        return {"id": 3, "name": "pool", "pool": {"id": 10, "name": "pool"}}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)
    monkeypatch.setattr(client, "get_pool", lambda _pool: {"id": 10, "name": "pool"})

    queue = client.ensure_queue_for_pool("proj", "pool")

    assert queue["id"] == 3
    assert calls == [
        (
            "/proj/_apis/distributedtask/queues",
            {"api-version": "7.1"},
            "GET",
            None,
        ),
        (
            "/proj/_apis/distributedtask/queues",
            {"api-version": "7.1", "authorizePipelines": "true"},
            "POST",
            {"name": "pool", "pool": {"id": 10}},
        ),
    ]


def test_authorize_pipeline_queue_uses_pipeline_permissions_api(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []

    def fake_rest_json(path, query, method="GET", body=None):
        calls.append((path, query, method, body))
        return {}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)

    client.authorize_pipeline_queue("proj", 48, 1)

    assert calls == [
        (
            "/proj/_apis/pipelines/pipelinePermissions/queue/48",
            {"api-version": "7.1-preview.1"},
            "PATCH",
            {"pipelines": [{"id": 1, "authorized": True}]},
        )
    ]


def test_open_queue_access_authorizes_all_pipelines(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = []
    monkeypatch.setattr(client, "ensure_queue_for_pool", lambda project, pool: {"id": 71, "name": pool})

    def fake_rest_json(path, query, method="GET", body=None):
        calls.append((path, query, method, body))
        return {"allPipelines": {"authorized": True}}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)

    assert client.open_queue_access("proj", "pool") == {"allPipelines": {"authorized": True}}
    assert calls == [
        (
            "/proj/_apis/pipelines/pipelinePermissions/queue/71",
            {"api-version": "7.1-preview.1"},
            "PATCH",
            {
                "resource": {"type": "queue", "id": "71"},
                "allPipelines": {"authorized": True},
                "pipelines": [],
            },
        )
    ]


def test_clear_agent_cache_forces_agent_refetch(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = {"count": 0}

    def fake_rest_json(path, query, method="GET", body=None):
        calls["count"] += 1
        return {"value": []}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)

    client.list_agents(10)
    client.list_agents(10)
    client.clear_agent_cache()
    client.list_agents(10)

    assert calls["count"] == 2


def test_discover_agents_keeps_registration_metadata(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    monkeypatch.setattr(client, "list_pools", lambda: [{"id": 1, "name": "Default"}])
    monkeypatch.setattr(
        client,
        "list_agents",
        lambda _pool_id: [
            {
                "id": 7,
                "name": "WINVM01",
                "status": "online",
                "enabled": True,
                "version": "4.273.0",
                "osDescription": "Microsoft Windows",
                "systemCapabilities": {"USERNAME": "svc-evilazp"},
            }
        ],
    )

    agent = client.discover_agents("default")[0]

    assert agent.version == "4.273.0"
    assert agent.os_description == "Microsoft Windows"
    assert agent.capabilities["USERNAME"] == "svc-evilazp"


def test_resolve_pool_name_accepts_agent_name(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    monkeypatch.setattr(client, "get_pool", lambda _name: None)
    monkeypatch.setattr(
        client,
        "discover_agents",
        lambda _pool: [
            type("Agent", (), {"name": "WINVM01", "pool_name": "Default"})(),
        ],
    )

    assert client.resolve_pool_name("winvm01") == "Default"


def test_find_pipeline_candidates_prefers_exact_pool_matches(monkeypatch):
    from azure_pipeline_cli.models import PipelineRef

    client = AzureDevOpsClient("mick3y", pat="token")
    exact = PipelineRef(4, "evilazp-pipeline", "evilazp-demo", pool_id=22, pool_name="evilazp-pool2")
    fallback = PipelineRef(1, "azure-pipeline", "azure-pipeline", pool_id=10, pool_name="old-pool")
    monkeypatch.setattr(client, "list_projects", lambda: ["azure-pipeline", "evilazp-demo"])
    monkeypatch.setattr(
        client,
        "list_pipelines",
        lambda project: [fallback] if project == "azure-pipeline" else [exact],
    )
    monkeypatch.setattr(client, "pipeline_has_queue_variables", lambda _project, _pipeline_id: True)

    candidates = client.find_pipeline_candidates(
        AgentRef(7, "evilazp-demo-agent", 22, "evilazp-pool2", "online", True)
    )

    assert candidates == [exact]


def test_find_pipeline_candidates_falls_back_to_evilazp_variables(monkeypatch):
    from azure_pipeline_cli.models import PipelineRef

    client = AzureDevOpsClient("mick3y", pat="token")
    fallback = PipelineRef(1, "azure-pipeline", "azure-pipeline", pool_id=10, pool_name="old-pool")
    monkeypatch.setattr(client, "list_projects", lambda: ["azure-pipeline"])
    monkeypatch.setattr(client, "list_pipelines", lambda _project: [fallback])
    monkeypatch.setattr(client, "pipeline_has_queue_variables", lambda _project, _pipeline_id: True)

    candidates = client.find_pipeline_candidates(AgentRef(7, "WINVM01", 22, "new-pool", "online", True))

    assert candidates == [fallback]


def test_ensure_runner_variables_sets_real_pool_and_agent(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    updates = []

    def fake_rest_json(path, query, method="GET", body=None):
        if method == "GET":
            return {
                "variables": {
                    "targetPool": {"value": "placeholder"},
                    "targetAgent": {"value": "placeholder"},
                    "commandB64": {"value": "placeholder"},
                    "runId": {"value": "placeholder"},
                }
            }
        updates.append(body)
        return {"ok": True}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)

    client.ensure_runner_variables("proj", 1, "azure-pipeline", "WINVM01")

    variables = updates[0]["variables"]
    assert variables["targetPool"]["value"] == "azure-pipeline"
    assert variables["targetAgent"]["value"] == "WINVM01"
    assert variables["commandB64"]["value"] != "placeholder"


def test_wait_for_marked_output_retries_until_exit_code(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = {"count": 0}

    def fake_fetch(project, build_id, run_id, command_text=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return "", None
        return "done", 0

    monkeypatch.setattr(client, "fetch_marked_output", fake_fetch)
    monkeypatch.setattr(client, "get_build", lambda *args: {"status": "inProgress", "result": None})
    monkeypatch.setattr("azure_pipeline_cli.azure_devops.time.sleep", lambda _seconds: None)

    assert client.wait_for_marked_output("proj", 1, "run", timeout=5, poll_interval=1) == ("done", 0)
    assert calls["count"] == 2


def test_wait_for_marked_output_keeps_polling_when_output_arrives_before_exit(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = {"count": 0}

    def fake_fetch(project, build_id, run_id, command_text=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return "user output", None
        return "user output", 0

    monkeypatch.setattr(client, "fetch_marked_output", fake_fetch)
    monkeypatch.setattr(client, "get_build", lambda *args: {"status": "inProgress", "result": None})
    monkeypatch.setattr("azure_pipeline_cli.azure_devops.time.sleep", lambda _seconds: None)

    assert client.wait_for_marked_output("proj", 1, "run", timeout=5, poll_interval=1) == ("user output", 0)
    assert calls["count"] == 2


def test_wait_for_marked_output_returns_output_after_short_exit_marker_grace(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = {"count": 0}
    monotonic_values = iter([0, 0, 0.25, 0.5])

    def fake_fetch(project, build_id, run_id, command_text=None):
        calls["count"] += 1
        return "user output", None

    monkeypatch.setattr(client, "fetch_marked_output", fake_fetch)
    monkeypatch.setattr(client, "get_build", lambda *args: {"status": "inProgress", "result": None})
    monkeypatch.setattr("azure_pipeline_cli.azure_devops.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("azure_pipeline_cli.azure_devops.time.sleep", lambda _seconds: None)

    assert client.wait_for_marked_output("proj", 1, "run", timeout=30, poll_interval=1) == ("user output", None)
    assert calls["count"] == 3


def test_wait_for_marked_output_rechecks_logs_after_build_completes_without_marker(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = {"count": 0}
    monotonic_values = iter([0, 0, 1, 1, 2.5, 2.5])

    def fake_fetch(project, build_id, run_id, command_text=None):
        calls["count"] += 1
        if calls["count"] == 2:
            return "late output", 0
        return "", None

    monkeypatch.setattr(client, "fetch_marked_output", fake_fetch)
    monkeypatch.setattr(client, "get_build", lambda *args: {"status": "completed", "result": "succeeded"})
    monkeypatch.setattr("azure_pipeline_cli.azure_devops.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("azure_pipeline_cli.azure_devops.time.sleep", lambda _seconds: None)

    assert client.wait_for_marked_output("proj", 1, "run", timeout=5, poll_interval=1) == ("late output", 0)
    assert calls["count"] == 2


def test_extract_marked_output_keeps_delayed_error_after_failed_end():
    text = "\n".join(
        [
            "2026-06-22T11:25:58.6370392Z ::EVILAZP_START::run",
            "2026-06-22T11:25:59.9517165Z ::EVILAZP_END::run::EXIT::1",
            "2026-06-22T11:26:00.7723991Z C:\\_work\\_temp\\evilazp-run.ps1 : Cannot find path 'C:/Users/mick3y/Desktop/test.txt' because it does not exist.",
            "2026-06-22T11:26:01.2357988Z At C:\\_work\\_temp\\evilazp-run.ps1:178 char:5",
            "2026-06-22T11:26:01.2527419Z ##[error]ProcessCompletedWithExitCode0",
            "2026-06-22T11:26:01.2536494Z ##[section]StepFinishing",
        ]
    )

    output, exit_code = extract_marked_output(text, "run")

    assert exit_code == 1
    assert "Cannot find path" in output
    assert "ProcessCompletedWithExitCode0" not in output


def test_run_pipeline_command_queues_parameters_string(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    queued_bodies = []

    def fake_rest_json(path, query, method="GET", body=None):
        if method == "POST":
            queued_bodies.append(body)
            return {"id": 123}
        return {}

    monkeypatch.setattr(client, "rest_json", fake_rest_json)
    monkeypatch.setattr(client, "wait_pipeline_command", lambda **kwargs: None)

    client.run_pipeline_command(
        pipeline=type("Pipeline", (), {"project": "proj", "id": 1})(),
        agent=AgentRef(1, "WINVM02", 10, "winvm02", "online", True),
        command_b64="aXBjb25maWc=",
        run_id="run123",
        timeout=10,
        poll_interval=1,
    )

    params = json.loads(queued_bodies[0]["parameters"])
    assert params == {
        "targetPool": "winvm02",
        "targetAgent": "WINVM02",
        "commandB64": "aXBjb25maWc=",
        "runId": "run123",
    }
    assert queued_bodies[0]["variables"] == {
        "targetPool": {"value": "winvm02"},
        "targetAgent": {"value": "WINVM02"},
        "commandB64": {"value": "aXBjb25maWc="},
        "runId": {"value": "run123"},
    }


def test_retryable_rest_error_detection():
    assert is_retryable_rest_error("Azure DevOps REST 503 for /logs/2: unavailable")
    assert not is_retryable_rest_error("Azure DevOps REST 404 for /logs/2: missing")


def test_summarize_error_body_strips_html():
    assert summarize_error_body("<html><style>body{color:red}</style><title>Azure DevOps Services Unavailable</title></html>") == (
        "Azure DevOps Services Unavailable"
    )


def test_format_rest_error_explains_azure_devops_503():
    message = format_rest_error(503, "/azure-pipeline/_apis/build/builds/82", "body { margin: 0; }")

    assert "temporarily unavailable" in message
    assert "Retry the command shortly" in message
    assert "polling the build" not in message
    assert "body { margin" not in message


def test_rest_text_with_retry(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    calls = {"count": 0}

    def fake_rest_text(path, query):
        calls["count"] += 1
        if calls["count"] == 1:
            raise AzureDevOpsError("Azure DevOps REST 503 for /logs/2: unavailable")
        return "ok"

    monkeypatch.setattr(client, "rest_text", fake_rest_text)
    monkeypatch.setattr("azure_pipeline_cli.azure_devops.time.sleep", lambda _seconds: None)

    assert client.rest_text_with_retry("/logs/2", {}) == "ok"
    assert calls["count"] == 2


def test_encode_command_utf8_base64():
    assert encode_command("whoami") == "d2hvYW1p"


def test_decode_command_utf8_base64():
    assert decode_command("d2hvYW1p") == "whoami"


def test_extract_marked_output_with_azure_timestamp_prefixes():
    text = "\n".join(
        [
            "2026-06-01T10:00:00.0000000Z preamble",
            "2026-06-01T10:00:01.0000000Z ::EVILAZP_START::run123",
            "2026-06-01T10:00:02.0000000Z user output",
            "plain output",
            "2026-06-01T10:00:03.0000000Z ::EVILAZP_END::run123::EXIT::7",
            "2026-06-01T10:00:04.0000000Z trailing",
        ]
    )

    output, exit_code = extract_marked_output(text, "run123")

    assert output == "user output\nplain output"
    assert exit_code == 7


def test_extract_marked_output_removes_command_echo():
    text = "\n".join(
        [
            "::EVILAZP_START::run123",
            "ipconfig /all",
            "Windows IP Configuration",
            "::EVILAZP_END::run123::EXIT::0",
        ]
    )

    output, exit_code = extract_marked_output(text, "run123", "ipconfig /all")

    assert output == "Windows IP Configuration"
    assert exit_code == 0


def test_fetch_marked_output_reads_recent_logs_first_and_caches_marker(monkeypatch):
    client = AzureDevOpsClient("mick3y", pat="token")
    fetched = []

    def fake_rest_json(path, query, method="GET", body=None):
        return {"value": [{"id": 1}, {"id": 2}, {"id": 3}]}

    def fake_rest_text(path, query, attempts=5, delay=2):
        log_id = int(path.rsplit("/", 1)[1])
        fetched.append((log_id, attempts, delay))
        if log_id == 3:
            return "\n".join(
                [
                    "::EVILAZP_START::run123",
                    "done",
                    "::EVILAZP_END::run123::EXIT::0",
                ]
            )
        return "old log"

    monkeypatch.setattr(client, "rest_json", fake_rest_json)
    monkeypatch.setattr(client, "rest_text_with_retry", fake_rest_text)

    assert client.fetch_marked_output("proj", 7, "run123") == ("done", 0)
    assert fetched == [(3, 2, 0.25)]

    fetched.clear()
    assert client.fetch_marked_output("proj", 7, "run123") == ("done", 0)
    assert fetched == [(3, 2, 0.25)]


def test_extract_tunnel_urls_from_marker_and_diag_line():
    text = "\n".join(
        [
            "2026-06-01T10:00:00.0000000Z ::EVILAZP_TUNNEL::https://abc-usew.devtunnels.ms",
            "2026-06-01T10:00:01.0000000Z [TunnelPlugin] Tunnel active. Forwarding ports: 8080",
            "2026-06-01T10:00:02.0000000Z Tunnel active: https://abc-8080.usew.devtunnels.ms.",
            "2026-06-01T10:00:03.0000000Z Tunnel active: https://abc-8080.usew.devtunnels.ms.",
        ]
    )

    assert extract_tunnel_urls(text) == [
        "https://abc-usew.devtunnels.ms",
        "https://abc-8080.usew.devtunnels.ms",
    ]
