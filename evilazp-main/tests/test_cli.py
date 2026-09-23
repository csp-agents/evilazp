from azure_pipeline_cli.cli import main


def test_init_is_non_mutating(capsys):
    assert (
        main(
            [
                "init",
                "--org",
                "mick3y",
                "--project",
                "p",
                "--repo",
                "r",
                "--pipeline",
                "pl",
                "--pool",
                "pool",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Dry run" in captured.out
    assert "push azure-pipelines.yml to Azure Repos branch main" in captured.out


def test_create_project_cli(monkeypatch, capsys):
    from azure_pipeline_cli import cli

    calls = []

    class FakeClient:
        def __init__(self, org, pat):
            calls.append(("client", org, pat))

        def ensure_project(self, project, visibility, process):
            calls.append(("ensure_project", project, visibility, process))

    monkeypatch.setattr(cli, "AzureDevOpsClient", FakeClient)

    assert main(["create-project", "--org", "mick3y", "--pat", "token", "--project", "proj"]) == 0

    captured = capsys.readouterr()
    assert "project ready: proj" in captured.out
    assert calls == [
        ("client", "mick3y", "token"),
        ("ensure_project", "proj", "private", "Basic"),
    ]


def test_create_pipeline_cli_requires_existing_project(monkeypatch, capsys):
    from azure_pipeline_cli import cli

    class FakeClient:
        def __init__(self, org, pat):
            pass

        def get_project(self, project):
            return None

    monkeypatch.setattr(cli, "AzureDevOpsClient", FakeClient)

    assert (
        main(
            [
                "create-pipeline",
                "--org",
                "mick3y",
                "--project",
                "missing",
                "--repo",
                "repo",
                "--pipeline",
                "pipe",
                "--pool",
                "pool",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert "Project not found: missing" in captured.err


def test_create_pipeline_cli(monkeypatch, capsys):
    from azure_pipeline_cli import cli
    from azure_pipeline_cli.models import PipelineRef

    calls = []

    class FakeClient:
        def __init__(self, org, pat):
            calls.append(("client", org, pat))

        def get_project(self, project):
            calls.append(("get_project", project))
            return {"name": project}

        def ensure_pool(self, pool):
            calls.append(("ensure_pool", pool))

        def ensure_repo(self, project, repo):
            calls.append(("ensure_repo", project, repo))
            return {"id": "repo-id"}

        def push_file(self, project, repo_id, branch, yml_path, content):
            calls.append(("push_file", project, repo_id, branch, yml_path, "pool:" in content))

        def ensure_pipeline(self, project, pipeline, repo, branch, yml_path, pool):
            calls.append(("ensure_pipeline", project, pipeline, repo, branch, yml_path, pool))
            return PipelineRef(7, pipeline, project, pool_name=pool)

    monkeypatch.setattr(cli, "AzureDevOpsClient", FakeClient)

    assert (
        main(
            [
                "create-pipeline",
                "--org",
                "mick3y",
                "--project",
                "proj",
                "--repo",
                "repo",
                "--pipeline",
                "pipe",
                "--pool",
                "pool",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "pipeline ready: proj/pipe (7)" in captured.out
    assert calls == [
        ("client", "mick3y", None),
        ("get_project", "proj"),
        ("ensure_pool", "pool"),
        ("ensure_repo", "proj", "repo"),
        ("push_file", "proj", "repo-id", "main", "azure-pipelines.yml", True),
        ("ensure_pipeline", "proj", "pipe", "repo", "main", "azure-pipelines.yml", "pool"),
    ]


def test_no_args_starts_shell_help(monkeypatch, capsys):
    from azure_pipeline_cli import cli

    class FakeShell:
        def __init__(self, *args, **kwargs):
            pass

        def cmdloop(self):
            print("shell-started")

    monkeypatch.setattr(cli, "PipelineShell", FakeShell)

    assert cli.main([]) == 0
    captured = capsys.readouterr()
    assert "shell-started" in captured.out


def test_startup_session_is_available_to_shell(monkeypatch):
    from azure_pipeline_cli import cli
    from azure_pipeline_cli.sessions import SessionRecord

    captured = {}

    class FakeClient:
        def __init__(self, org, pat):
            self.organization = org
            self.pat = pat

    class FakeShell:
        def __init__(self, *args, **kwargs):
            captured["initial_session"] = kwargs["initial_session"]
            captured["poll_interval"] = kwargs["poll_interval"]
            captured["agent_sync_interval"] = kwargs["agent_sync_interval"]

        def cmdloop(self):
            pass

    session = SessionRecord(
        "lab",
        "https://dev.azure.com/mick3y",
        "pat",
        sp_tenant_id="tenant",
        sp_client_id="client",
        sp_client_secret="secret",
    )
    monkeypatch.setattr(cli, "choose_startup_session", lambda: session)
    monkeypatch.setattr(cli, "AzureDevOpsClient", FakeClient)
    monkeypatch.setattr(cli, "PipelineShell", FakeShell)

    assert cli.main([]) == 0
    assert captured["initial_session"].sp_client_secret == "secret"
    assert captured["poll_interval"] == 1
    assert captured["agent_sync_interval"] == 1


def test_shell_polling_intervals_are_configurable(monkeypatch):
    from azure_pipeline_cli import cli

    captured = {}

    class FakeClient:
        def __init__(self, org, pat):
            pass

    class FakeShell:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def cmdloop(self):
            pass

    monkeypatch.setattr(cli, "AzureDevOpsClient", FakeClient)
    monkeypatch.setattr(cli, "PipelineShell", FakeShell)

    assert cli.main(["shell", "--org", "mick3y", "--poll-interval", "0.25", "--agent-sync-interval", "0.5"]) == 0

    assert captured["poll_interval"] == 0.25
    assert captured["agent_sync_interval"] == 0.5


def test_create_agent_cli_builds_from_connected_config(monkeypatch, capsys):
    from types import SimpleNamespace

    from azure_pipeline_cli import cli

    calls = []

    class FakeClient:
        organization = "https://dev.azure.com/org"
        pat = "pat"

        def __init__(self, org, pat=None):
            calls.append(("client", org, pat))

        def ensure_pool(self, pool):
            calls.append(("ensure_pool", pool))

    def fake_build_from_config(config, *, quiet=False):
        calls.append(("build", config.pool, config.agent, config.runtime, config.polling, quiet))
        return SimpleNamespace(binary="/tmp/Agent.Listener.exe")

    monkeypatch.setattr(cli, "AzureDevOpsClient", FakeClient)
    monkeypatch.setattr(cli, "build_from_config", fake_build_from_config)

    assert cli.main([
        "create-agent",
        "--org",
        "org",
        "--pool",
        "pool",
        "--agent",
        "agent",
        "--runtime",
        "win-x64",
        "--polling",
        "1",
    ]) == 0

    captured = capsys.readouterr()
    assert "agent binary ready: /tmp/Agent.Listener.exe" in captured.out
    assert calls == [
        ("client", "org", None),
        ("ensure_pool", "pool"),
        ("build", "pool", "agent", "win-x64", 1, True),
    ]


def test_create_agent_cli_passes_sign_options(monkeypatch, capsys):
    from types import SimpleNamespace

    from azure_pipeline_cli import cli

    calls = []

    class FakeClient:
        organization = "https://dev.azure.com/org"
        pat = "pat"

        def __init__(self, org, pat=None):
            pass

        def ensure_pool(self, pool):
            pass

    def fake_build_from_config(config, *, quiet=False):
        calls.append((config.sign, config.pfx, config.pfx_pass, config.timestamp, config.cert_subject, quiet))
        return SimpleNamespace(binary="/tmp/Agent.Listener.exe")

    monkeypatch.setattr(cli, "AzureDevOpsClient", FakeClient)
    monkeypatch.setattr(cli, "build_from_config", fake_build_from_config)

    assert cli.main([
        "create-agent",
        "--org",
        "org",
        "--pool",
        "pool",
        "--agent",
        "agent",
        "--runtime",
        "win-x64",
        "--sign",
        "--pfx",
        "/tmp/cert.pfx",
        "--pfx-pass",
        "secret",
        "--timestamp",
        "",
        "--cert-subject",
        "/CN=test",
    ]) == 0

    captured = capsys.readouterr()
    assert "agent binary ready" in captured.out
    assert calls == [(True, "/tmp/cert.pfx", "secret", "", "/CN=test", True)]


def test_shell_help_includes_clear(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_help("")
    captured = capsys.readouterr()
    assert "/clear" in captured.out


def test_shell_help_includes_agents_refresh(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_help("")
    captured = capsys.readouterr()
    assert "/agent" in captured.out
    assert "/agent-pool" in captured.out
    assert "/agents [--refresh|--all]" not in captured.out
    assert "/delete-agent <index|hostname>" not in captured.out


def test_agent_help_includes_subcommands(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_agent("--help")
    captured = capsys.readouterr()
    assert "/agent list [--refresh|--all]" in captured.out
    assert "/agent create --pool POOL --agent AGENT --runtime RUNTIME [--polling N] [--tunnel] [--relay] [--sign]" in captured.out
    assert "/agent create --yaml --runtime RUNTIME [--polling N] [--tunnel] [--relay] [--sign]" in captured.out
    assert "vendor/azure-pipelines-agent/.env" in captured.out
    assert "--relay-connection-string <value>" in captured.out
    assert "--sign" in captured.out
    assert "/agent remove <index|hostname>" in captured.out
    assert "/agent-pool" not in captured.out


def test_agent_pool_help_includes_subcommands(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_agent_pool("--help")
    captured = capsys.readouterr()
    assert "/agent-pool list" in captured.out
    assert "/agent-pool create --pool POOL" in captured.out
    assert "/agent-pool open-access --project PROJECT --pool POOL" in captured.out
    assert "/agent-pool remove --pool POOL" in captured.out


def test_shell_help_includes_sessions(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_help("")
    captured = capsys.readouterr()
    assert "/session" in captured.out
    assert "/sessions" not in captured.out
    assert "/session use <index|name>" not in captured.out


def test_session_help_includes_subcommands(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_session("--help")
    captured = capsys.readouterr()
    assert "/session list" in captured.out
    assert "/session use <index|name>" in captured.out
    assert "/session remove <index|name>" in captured.out


def test_shell_help_includes_project_pipeline_groups_only(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_help("")

    captured = capsys.readouterr()
    assert "/project" in captured.out
    assert "/pipeline" in captured.out
    assert "/project list" not in captured.out
    assert "/pipeline create" not in captured.out
    assert "/create-project" not in captured.out
    assert "/create-pipeline" not in captured.out
    assert "/yaml" not in captured.out


def test_bare_help_shows_local_help_without_selected_agent(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)

    shell.default("--help")

    captured = capsys.readouterr()
    assert "/agent" in captured.out
    assert "/help" in captured.out
    assert "No agent selected" not in captured.err


def test_project_and_pipeline_help_include_subcommands(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_project("--help")
    shell.do_pipeline("--help")

    captured = capsys.readouterr()
    assert "/project list" in captured.out
    assert "/project create" in captured.out
    assert "/project remove" in captured.out
    assert "/pipeline list" in captured.out
    assert "/pipeline create" in captured.out
    assert "/pipeline remove" in captured.out


def test_shell_help_includes_devtunnels(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_help("")
    captured = capsys.readouterr()
    assert "/devtunnels" in captured.out

    shell.do_devtunnels("--help")
    captured = capsys.readouterr()
    assert "/devtunnels start --tunnel-id <id> --port <port> [--local <port>]" in captured.out
    assert "/devtunnels --tunnel-id <id> --port <port> [--local <port>]" in captured.out
    assert "/devtunnels stop <id|localPort|all>" in captured.out


def test_shell_help_does_not_include_plain_remote_commands(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_help("")
    captured = capsys.readouterr()
    assert "print selected agent hostname" not in captured.out
    assert "whoami" not in captured.out


def test_banner_uses_evilazp_brand_and_current_version():
    from azure_pipeline_cli import __version__
    from azure_pipeline_cli.shell import format_banner

    banner = format_banner()

    assert "EvilAZP" in banner
    assert f"v{__version__}" in banner
    assert "____        __" not in banner


def test_prompt_colors_are_readline_safe(monkeypatch):
    from azure_pipeline_cli.shell import READLINE_IGNORE_END, READLINE_IGNORE_START, prompt_style_text

    class Tty:
        def isatty(self):
            return True

    class GnuReadline:
        __doc__ = "Importing this module enables command line editing using GNU readline."

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("EVILAZP_COLOR_PROMPT", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setitem(__import__("sys").modules, "readline", GnuReadline)
    text = prompt_style_text("evilazp", "cyan", stream=Tty())

    assert READLINE_IGNORE_START in text
    assert READLINE_IGNORE_END in text
    assert "evilazp" in text


def test_prompt_colors_disabled_for_libedit(monkeypatch):
    from azure_pipeline_cli.shell import READLINE_IGNORE_END, READLINE_IGNORE_START, prompt_style_text

    class Tty:
        def isatty(self):
            return True

    class LibeditReadline:
        __doc__ = "Importing this module enables command line editing using libedit readline."

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setitem(__import__("sys").modules, "readline", LibeditReadline)
    text = prompt_style_text("evilazp", "cyan", stream=Tty())

    assert text == "evilazp"
    assert READLINE_IGNORE_START not in text
    assert READLINE_IGNORE_END not in text


def test_prompt_colors_disabled_by_default_on_macos(monkeypatch):
    from azure_pipeline_cli import shell as shell_module

    class Tty:
        def isatty(self):
            return True

    class GnuReadline:
        __doc__ = "Importing this module enables command line editing using GNU readline."

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("EVILAZP_COLOR_PROMPT", raising=False)
    monkeypatch.setattr(shell_module.sys, "platform", "darwin")
    monkeypatch.setitem(__import__("sys").modules, "readline", GnuReadline)

    assert shell_module.prompt_style_text("evilazp", "cyan", stream=Tty()) == "evilazp"


def test_strip_prompt_prefix_from_recalled_command():
    from azure_pipeline_cli.shell import strip_prompt_prefix

    assert strip_prompt_prefix("evilazp(no-agent)> /agent list") == "/agent list"
    assert strip_prompt_prefix("evilazp(WINVM01)> whoami") == "whoami"
    assert strip_prompt_prefix("/agent list") == "/agent list"


def test_init_without_connection_stops_after_connect(monkeypatch, capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    monkeypatch.setattr(shell, "do_connect", lambda _arg: setattr(shell, "client", object()))

    shell.do_init("")

    captured = capsys.readouterr()
    assert "Run /init again" in captured.out


def test_connect_saves_session(monkeypatch, capsys):
    from azure_pipeline_cli.shell import PipelineShell

    saved = []

    class FakeClient:
        organization = "https://dev.azure.com/mick3y"
        org_name = "mick3y"

        def __init__(self, org, pat):
            self.organization = f"https://dev.azure.com/{org}"
            self.org_name = org
            self.pat = pat

        def list_projects(self):
            return ["p"]

    monkeypatch.setattr("azure_pipeline_cli.shell.AzureDevOpsClient", FakeClient)
    monkeypatch.setattr("azure_pipeline_cli.shell.save_session", lambda record: saved.append(record))

    shell = PipelineShell(None, project="proj", pipeline="pipe", pool="pool")
    shell.do_connect("--org mick3y --pat token --name lab")

    captured = capsys.readouterr()
    assert "saved session 'lab'" in captured.out
    assert saved[0].name == "lab"
    assert saved[0].organization == "https://dev.azure.com/mick3y"
    assert saved[0].pat == "token"
    assert saved[0].project == "proj"
    assert saved[0].pipeline == "pipe"
    assert saved[0].pool == "pool"


def test_connect_saves_session_with_sp_options(monkeypatch):
    from azure_pipeline_cli.shell import PipelineShell

    saved = []

    class FakeClient:
        def __init__(self, org, pat):
            self.organization = f"https://dev.azure.com/{org}"
            self.org_name = org
            self.pat = pat

        def list_projects(self):
            return ["p"]

    monkeypatch.setattr("azure_pipeline_cli.shell.AzureDevOpsClient", FakeClient)
    monkeypatch.setattr("azure_pipeline_cli.shell.save_session", lambda record: saved.append(record))

    shell = PipelineShell(None)
    shell.do_connect("--org mick3y --pat token --sp-tenant-id tenant --sp-client-id client --sp-client-secret secret")

    assert saved[0].sp_tenant_id == "tenant"
    assert saved[0].sp_client_id == "client"
    assert saved[0].sp_client_secret == "secret"


def test_session_command_connects_saved_session(monkeypatch, capsys):
    from azure_pipeline_cli.sessions import SessionRecord
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def __init__(self, org, pat):
            self.organization = org
            self.pat = pat

        def list_projects(self):
            return ["p1", "p2"]

    monkeypatch.setattr("azure_pipeline_cli.shell.AzureDevOpsClient", FakeClient)
    monkeypatch.setattr(
        "azure_pipeline_cli.shell.load_sessions",
        lambda: [SessionRecord("lab", "https://dev.azure.com/mick3y", "token", project="proj", pool="pool")],
    )
    monkeypatch.setattr("azure_pipeline_cli.shell.mark_session_used", lambda _name: None)

    shell = PipelineShell(None)
    shell.do_session("use 1")

    captured = capsys.readouterr()
    assert "via session 'lab'" in captured.out
    assert shell.client.organization == "https://dev.azure.com/mick3y"
    assert shell.project_filter == "proj"
    assert shell.pool_filter == "pool"


def test_session_list_shows_saved_sessions(monkeypatch, capsys):
    from azure_pipeline_cli.sessions import SessionRecord
    from azure_pipeline_cli.shell import PipelineShell

    monkeypatch.setattr(
        "azure_pipeline_cli.shell.load_sessions",
        lambda: [SessionRecord("lab", "https://dev.azure.com/mick3y", "token")],
    )

    shell = PipelineShell(None)
    shell.do_session("list")

    captured = capsys.readouterr()
    assert "lab" in captured.out
    assert "mick3y" in captured.out


def test_session_requires_use_subcommand(capsys):
    from azure_pipeline_cli.sessions import SessionRecord
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_session("1")

    captured = capsys.readouterr()
    assert "usage: /session <list|use|remove>" in captured.err


def test_devtunnels_requires_sp_credentials(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_devtunnels("--tunnel-id abc123 --port 22 --local 2222")

    captured = capsys.readouterr()
    assert "Current session has no DevTunnel SP credentials" in captured.err


def test_devtunnels_connect_list_stop(capsys):
    from azure_pipeline_cli.sessions import SessionRecord
    from azure_pipeline_cli.shell import PipelineShell

    class FakeConnection:
        tunnel_id = "abc123"
        remote_port = 22
        local_port = 2222
        status = "running"

    class FakeManager:
        def __init__(self):
            self.started = None
            self.shutdown_called = False

        def start(self, spec, credentials):
            self.started = (spec, credentials)
            return FakeConnection()

        def list(self):
            return [FakeConnection()]

        def stop(self, target):
            return [FakeConnection()] if target == "2222" else []

        def shutdown(self):
            self.shutdown_called = True

    manager = FakeManager()
    shell = PipelineShell(None, devtunnel_manager=manager)
    shell.current_session = SessionRecord(
        "lab",
        "org",
        "pat",
        sp_tenant_id="tenant",
        sp_client_id="client",
        sp_client_secret="secret",
    )

    shell.do_devtunnels("--tunnel-id abc123 --port 22 --local 2222")
    shell.do_devtunnels("list")
    shell.do_devtunnels("stop 2222")

    captured = capsys.readouterr()
    assert "connected 127.0.0.1:2222 -> abc123:22" in captured.out
    assert "local:127.0.0.1:2222" in captured.out
    assert "stopped 127.0.0.1:2222" in captured.out
    assert manager.started[1].client_secret == "secret"


def test_devtunnels_sp_saves_masked_secret(monkeypatch, capsys):
    from azure_pipeline_cli.sessions import SessionRecord
    from azure_pipeline_cli.shell import PipelineShell

    saved = []
    monkeypatch.setattr("azure_pipeline_cli.shell.save_session", lambda record: saved.append(record))
    shell = PipelineShell(None)
    shell.current_session = SessionRecord("lab", "org", "pat")

    shell.do_devtunnels("sp --tenant-id tenant --sp-client-id client --sp-secret secret")

    captured = capsys.readouterr()
    assert saved[0].sp_tenant_id == "tenant"
    assert saved[0].sp_client_id == "client"
    assert saved[0].sp_client_secret == "secret"
    assert "secret=secret" not in captured.out
    assert "se...et" in captured.out


def test_shell_help_includes_relay_bridge(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_help("")

    captured = capsys.readouterr()
    assert "/relay-bridge" in captured.out
    assert "/resource-group" in captured.out
    assert "/relay-bridge stop <id|localPort|relayName|all>" not in captured.out

    shell.do_relay_bridge("--help")
    captured = capsys.readouterr()
    assert "/relay-bridge stop <id|localPort|relayName|all>" in captured.out
    assert "/relay-bridge ns list" in captured.out
    assert "/relay-bridge ns keys" in captured.out
    assert "/relay-bridge hc list" in captured.out
    assert "-L <local-port>:<relay>" in captured.out
    assert "-T <relay>:<target-host>:<target-port>" in captured.out
    assert "-H <relay>:http/<host>:<port>" in captured.out
    assert "agent side: publish target SSH" in captured.out


def test_resource_group_help(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.do_resource_group("--help")

    captured = capsys.readouterr()
    assert "/resource-group list" in captured.out
    assert "/resource-group create -g <resource-group> -l <location>" in captured.out
    assert "/resource-group remove -g <resource-group> [--yes]" in captured.out


def test_resource_group_list_create_remove(capsys):
    from azure_pipeline_cli.resource_groups import ResourceGroup
    from azure_pipeline_cli.shell import PipelineShell

    class FakeResourceGroups:
        def __init__(self):
            self.calls = []

        def list(self):
            self.calls.append(("list",))
            return [ResourceGroup("rg-relay", "koreacentral", "Succeeded")]

        def create(self, name, location):
            self.calls.append(("create", name, location))
            return ResourceGroup(name, location, "Succeeded")

        def remove(self, name):
            self.calls.append(("remove", name))

    manager = FakeResourceGroups()
    shell = PipelineShell(None, resource_group_manager=manager)

    shell.do_resource_group("list")
    shell.do_resource_group("create -g rg-relay -l koreacentral")
    shell.do_resource_group("remove -g rg-relay --yes")

    captured = capsys.readouterr()
    assert "rg-relay" in captured.out
    assert "created rg-relay" in captured.out
    assert "removed rg-relay" in captured.out
    assert manager.calls == [
        ("list",),
        ("create", "rg-relay", "koreacentral"),
        ("remove", "rg-relay"),
    ]


def test_relay_bridge_connect_list_stop(capsys):
    from azure_pipeline_cli.relay_bridge import parse_relay_bridge_command
    from azure_pipeline_cli.shell import PipelineShell

    class FakeConnection:
        def __init__(self):
            self.id = 1
            self.forwards = parse_relay_bridge_command("start -x cs -L 2222:ssh-relay1234").spec.forwards
            self.status = "running"

    class FakeManager:
        def __init__(self):
            self.started = None
            self.shutdown_called = False

        def start(self, spec):
            self.started = spec
            return FakeConnection()

        def list(self):
            return [FakeConnection()]

        def stop(self, target):
            return [FakeConnection()] if target == "2222" else []

        def shutdown(self):
            self.shutdown_called = True

    manager = FakeManager()
    shell = PipelineShell(None, relay_bridge_manager=manager)

    shell.do_relay_bridge("start -x secret-connection -L 2222:ssh-relay1234")
    shell.do_relay_bridge("list")
    shell.do_relay_bridge("stop 2222")

    captured = capsys.readouterr()
    assert "started 1: -L ssh-relay1234 -> 2222" in captured.out
    assert "secret-connection" not in captured.out
    assert "ssh-relay1234" in captured.out
    assert "stopped 1" in captured.out
    assert manager.started.connection_string == "secret-connection"


def test_relay_bridge_hc_list_create_delete(capsys):
    from azure_pipeline_cli.relay_bridge import HybridConnection
    from azure_pipeline_cli.shell import PipelineShell

    class FakeHybridConnections:
        def __init__(self):
            self.calls = []

        def list(self, resource_group, namespace):
            self.calls.append(("list", resource_group, namespace))
            return [HybridConnection("ssh-relay", True)]

        def create(self, resource_group, namespace, name, requires_client_authorization=True):
            self.calls.append(("create", resource_group, namespace, name, requires_client_authorization))
            return HybridConnection(name, requires_client_authorization)

        def delete(self, resource_group, namespace, name):
            self.calls.append(("delete", resource_group, namespace, name))

    manager = FakeHybridConnections()
    shell = PipelineShell(None, hybrid_connection_manager=manager)

    shell.do_relay_bridge("hc list -g rg-relay -n relayns")
    shell.do_relay_bridge("hc create -g rg-relay -n relayns winvm03-ssh --no-auth")
    shell.do_relay_bridge("hc remove -g rg-relay -n relayns winvm03-ssh")

    captured = capsys.readouterr()
    assert "ssh-relay" in captured.out
    assert "created HC winvm03-ssh" in captured.out
    assert "removed HC winvm03-ssh" in captured.out
    assert manager.calls[1] == ("create", "rg-relay", "relayns", "winvm03-ssh", False)


def test_relay_bridge_namespace_list_create_delete(capsys):
    from azure_pipeline_cli.relay_bridge import RelayNamespace, RelayNamespaceKeys
    from azure_pipeline_cli.shell import PipelineShell

    class FakeHybridConnections:
        def __init__(self):
            self.calls = []

        def list_namespaces(self, resource_group):
            self.calls.append(("ns-list", resource_group))
            return [RelayNamespace("relayns", "koreacentral", "Succeeded", "https://relayns.servicebus.windows.net:443/")]

        def create_namespace(self, resource_group, name, location=None):
            self.calls.append(("ns-create", resource_group, name, location))
            return RelayNamespace(name, location or "", "Succeeded")

        def delete_namespace(self, resource_group, name):
            self.calls.append(("ns-delete", resource_group, name))

        def get_namespace_keys(self, resource_group, namespace, auth_rule="RootManageSharedAccessKey"):
            self.calls.append(("ns-keys", resource_group, namespace, auth_rule))
            return RelayNamespaceKeys("Endpoint=sb://relayns/;SharedAccessKey=secret", "", "", "", auth_rule)

    manager = FakeHybridConnections()
    shell = PipelineShell(None, hybrid_connection_manager=manager)

    shell.do_relay_bridge("ns list -g rg-relay")
    shell.do_relay_bridge("ns create -g rg-relay -n relayns -l koreacentral")
    shell.do_relay_bridge("ns keys -g rg-relay -n relayns --rule listen")
    shell.do_relay_bridge("ns remove -g rg-relay -n relayns")

    captured = capsys.readouterr()
    assert "relayns.servicebus.windows.net" in captured.out
    assert "created namespace relayns" in captured.out
    assert "Endpoint=sb://relayns/;SharedAccessKey=secret" in captured.out
    assert "removed namespace relayns" in captured.out
    assert manager.calls == [
        ("ns-list", "rg-relay"),
        ("ns-create", "rg-relay", "relayns", "koreacentral"),
        ("ns-keys", "rg-relay", "relayns", "listen"),
        ("ns-delete", "rg-relay", "relayns"),
    ]


def test_exit_shuts_down_devtunnels():
    from azure_pipeline_cli.shell import PipelineShell

    class FakeManager:
        def __init__(self):
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    manager = FakeManager()
    shell = PipelineShell(None, devtunnel_manager=manager)

    assert shell.do_exit("")
    assert manager.shutdown_called


def test_exit_shuts_down_relay_bridge():
    from azure_pipeline_cli.shell import PipelineShell

    class FakeManager:
        def shutdown(self):
            pass

    class FakeRelayBridgeManager:
        def __init__(self):
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    relay_bridge = FakeRelayBridgeManager()
    shell = PipelineShell(None, devtunnel_manager=FakeManager(), relay_bridge_manager=relay_bridge)

    assert shell.do_exit("")
    assert relay_bridge.shutdown_called


def test_only_slash_exit_exits(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)

    assert shell.do_quit("") is False
    assert shell.do_EOF("") is False

    captured = capsys.readouterr()
    assert "Use /exit to quit." in captured.err


def test_init_rejects_slash_project_name(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(object())

    shell.do_init("--project /init --repo repo --pipeline pipe --pool pool")

    captured = capsys.readouterr()
    assert "invalid project name" in captured.err


def test_shell_init_requires_one_line_options(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(object())

    shell.do_init("")

    captured = capsys.readouterr()
    assert "usage: /init --project PROJECT --repo REPO --pipeline PIPELINE --pool POOL" in captured.err


def test_shell_init_apply_false_is_dry_run_without_prompt(monkeypatch, capsys):
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def ensure_project(self, *args):
            calls.append(("ensure_project", args))

    def fail_confirm(*_args, **_kwargs):
        raise AssertionError("confirm should not be called")

    monkeypatch.setattr("azure_pipeline_cli.shell.confirm", fail_confirm)

    shell = PipelineShell(FakeClient())
    shell.do_init("--project proj --repo repo --pipeline pipe --pool pool --apply false")

    captured = capsys.readouterr()
    assert "dry run; no resources were changed" in captured.out
    assert calls == []


def test_shell_project_create(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def ensure_project(self, project, visibility, process):
            calls.append(("ensure_project", project, visibility, process))

    shell = PipelineShell(FakeClient())

    shell.do_project("create --project proj --visibility public --process Agile")

    captured = capsys.readouterr()
    assert "ready proj" in captured.out
    assert shell.project_filter == "proj"
    assert calls == [("ensure_project", "proj", "public", "Agile")]


def test_shell_project_create_requires_project_option(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def ensure_project(self, *_args):
            raise AssertionError("ensure_project should not be called")

    shell = PipelineShell(FakeClient())

    shell.do_project("create")

    captured = capsys.readouterr()
    assert "usage: /project create --project PROJECT" in captured.err


def test_shell_project_remove_with_yes(capsys):
    from azure_pipeline_cli.models import AgentRef, PipelineRef
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def delete_project(self, project):
            calls.append(("delete_project", project))

    shell = PipelineShell(FakeClient(), project="proj")
    shell.selected_agent = AgentRef(1, "WINVM01", 10, "pool", "online", True)
    shell.selected_pipeline = PipelineRef(7, "pipe", "proj")

    shell.do_project("remove proj --yes")

    captured = capsys.readouterr()
    assert "project proj" in captured.out
    assert shell.project_filter is None
    assert shell.selected_agent is None
    assert shell.selected_pipeline is None
    assert calls == [("delete_project", "proj")]


def test_shell_project_remove_not_found_shows_available_projects(capsys):
    from azure_pipeline_cli.azure_devops import AzureDevOpsError
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def delete_project(self, project):
            raise AzureDevOpsError(f"Project not found: {project}")

        def list_projects(self):
            return ["evilazp-project"]

    shell = PipelineShell(FakeClient())

    shell.do_project("remove evilazp-demo --yes")

    captured = capsys.readouterr()
    assert "Project not found: evilazp-demo" in captured.err
    assert "Available projects: evilazp-project" in captured.err


def test_shell_pipeline_create(capsys):
    from azure_pipeline_cli.models import PipelineRef
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def get_project(self, project):
            calls.append(("get_project", project))
            return {"name": project}

        def resolve_pool_name(self, pool):
            calls.append(("resolve_pool_name", pool))
            return "resolved-pool"

        def ensure_pool(self, pool):
            calls.append(("ensure_pool", pool))

        def ensure_repo(self, project, repo):
            calls.append(("ensure_repo", project, repo))
            return {"id": "repo-id"}

        def push_file(self, project, repo_id, branch, yml_path, content):
            calls.append(("push_file", project, repo_id, branch, yml_path, "EVILAZP_COMMAND_B64" in content))

        def ensure_pipeline(self, project, pipeline, repo, branch, yml_path, pool):
            calls.append(("ensure_pipeline", project, pipeline, repo, branch, yml_path, pool))
            return PipelineRef(9, pipeline, project, pool_name=pool)

        def clear_agent_cache(self):
            calls.append(("clear_agent_cache",))

    shell = PipelineShell(FakeClient())

    shell.do_pipeline(
        "create --project proj --repo repo --pipeline pipe --pool pool "
        "--branch main --yml-path azure-pipelines.yml"
    )

    captured = capsys.readouterr()
    assert "ready proj/pipe (9)" in captured.out
    assert shell.project_filter == "proj"
    assert shell.pipeline_filter == "9"
    assert shell.pool_filter == "resolved-pool"
    assert calls == [
        ("get_project", "proj"),
        ("resolve_pool_name", "pool"),
        ("ensure_pool", "resolved-pool"),
        ("ensure_repo", "proj", "repo"),
        ("push_file", "proj", "repo-id", "main", "azure-pipelines.yml", True),
        ("ensure_pipeline", "proj", "pipe", "repo", "main", "azure-pipelines.yml", "resolved-pool"),
        ("clear_agent_cache",),
    ]


def test_shell_pipeline_list_and_remove_with_yes(capsys):
    from azure_pipeline_cli.models import PipelineRef
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def list_pipelines(self, project):
            calls.append(("list_pipelines", project))
            return [PipelineRef(7, "pipe", project, pool_name="pool")]

        def delete_pipeline(self, project, pipeline_id):
            calls.append(("delete_pipeline", project, pipeline_id))

    shell = PipelineShell(FakeClient(), project="proj")

    shell.do_pipeline("list --project proj")
    shell.do_pipeline("remove pipe --yes")

    captured = capsys.readouterr()
    assert "pipe" in captured.out
    assert "pipeline proj/pipe (7)" in captured.out
    assert calls == [
        ("list_pipelines", "proj"),
        ("list_pipelines", "proj"),
        ("delete_pipeline", "proj", 7),
    ]


def test_shell_pipeline_list_project_not_found_shows_available_projects(capsys):
    from azure_pipeline_cli.azure_devops import AzureDevOpsError
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def list_pipelines(self, project):
            raise AzureDevOpsError(
                "Azure DevOps REST 400 for /evilazp-demo/_apis/build/definitions: "
                "TF200016: The following project does not exist: evilazp-demo."
            )

        def list_projects(self):
            return ["evilazp-project"]

    shell = PipelineShell(FakeClient())

    shell.do_pipeline("list --project evilazp-demo")

    captured = capsys.readouterr()
    assert "Project not found: evilazp-demo" in captured.err
    assert "Available projects: evilazp-project" in captured.err


def test_shell_pipeline_remove_project_not_found_shows_available_projects(capsys):
    from azure_pipeline_cli.azure_devops import AzureDevOpsError
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def list_pipelines(self, project):
            raise AzureDevOpsError("ProjectDoesNotExistWithNameException")

        def list_projects(self):
            return ["evilazp-project"]

    shell = PipelineShell(FakeClient())

    shell.do_pipeline("remove evilazp-pipeline --project evilazp-demo --yes")

    captured = capsys.readouterr()
    assert "Project not found: evilazp-demo" in captured.err
    assert "Available projects: evilazp-project" in captured.err


def test_shell_pipeline_list_requires_project_option(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def list_pipelines(self, *_args):
            raise AssertionError("list_pipelines should not be called")

    shell = PipelineShell(FakeClient(), project="proj")

    shell.do_pipeline("list")

    captured = capsys.readouterr()
    assert "usage: /pipeline list --project PROJECT" in captured.err


def test_shell_pipeline_create_requires_required_options(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def get_project(self, *_args):
            raise AssertionError("get_project should not be called")

    shell = PipelineShell(FakeClient())

    shell.do_pipeline("create --project proj --repo repo")

    captured = capsys.readouterr()
    assert "usage: /pipeline create --project PROJECT --repo REPO --pipeline PIPELINE --pool POOL" in captured.err


def test_unwrap_outer_quotes_for_interactive_run():
    from azure_pipeline_cli.shell import unwrap_outer_quotes

    assert unwrap_outer_quotes('"whoami /all"') == "whoami /all"
    assert unwrap_outer_quotes("'ipconfig /all'") == "ipconfig /all"
    assert unwrap_outer_quotes('echo "hello"') == 'echo "hello"'


def test_hostname_runs_remotely_instead_of_quick_local_lookup(capsys):
    from azure_pipeline_cli.models import AgentRef
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.selected_agent = AgentRef(1, "WINVM01", 2, "azure-pipeline", "online", True)

    shell.default("hostname")

    captured = capsys.readouterr()
    assert "Not connected" in captured.err


def test_whoami_prints_selected_pipeline_agent_info(capsys):
    from azure_pipeline_cli.models import AgentRef, PipelineRef
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)
    shell.selected_agent = AgentRef(
        1,
        "WINVM01",
        10,
        "azure-pipeline",
        "online",
        True,
        version="4.273.0",
        os_description="Microsoft Windows",
        capabilities={"USERNAME": "svc-evilazp", "USERDOMAIN": "LAB"},
    )
    shell.selected_pipeline = PipelineRef(7, "host-info", "project")

    shell.default("whoami")

    captured = capsys.readouterr()
    assert "WINVM01" in captured.out
    assert "Microsoft Windows" in captured.out
    assert "LAB\\svc-evilazp" in captured.out
    assert "host-info" in captured.out


def test_whoami_requires_selected_agent(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    shell = PipelineShell(None)

    shell.default("whoami")

    captured = capsys.readouterr()
    assert "No agent selected" in captured.err


def test_run_whoami_executes_remote_command(capsys):
    from azure_pipeline_cli.azure_devops import decode_command
    from azure_pipeline_cli.models import AgentRef, PipelineRef, RunResult
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def __init__(self):
            self.queued_command = None

        def ensure_queue_variables(self, project, pipeline_id, variables):
            pass

        def run_pipeline_command(self, pipeline, agent, command_b64, run_id, timeout, poll_interval):
            self.queued_command = decode_command(command_b64)
            return RunResult(123, "completed", "succeeded", "REMOTE-WHOAMI", 0)

    client = FakeClient()
    shell = PipelineShell(client)
    shell.selected_agent = AgentRef(1, "WINVM01", 10, "azure-pipeline", "online", True)
    shell.selected_pipeline = PipelineRef(7, "host-info", "project")

    shell.do_run("whoami")

    captured = capsys.readouterr()
    assert client.queued_command == "whoami"
    assert "REMOTE-WHOAMI" in captured.out
    assert "Field" not in captured.out
    assert "LAB\\" not in captured.out


def test_upload_target_agent_executes_remote_azure_files_script(monkeypatch, capsys):
    from azure_pipeline_cli.azure_devops import decode_command
    from azure_pipeline_cli.azure_files import AzureFilesConfig
    from azure_pipeline_cli.models import AgentRef, PipelineRef, RunResult
    from azure_pipeline_cli.shell import PipelineShell
    import azure_pipeline_cli.shell as shell_module

    class FakeClient:
        def __init__(self):
            self.queued_command = None

        def ensure_queue_variables(self, project, pipeline_id, variables):
            pass

        def run_pipeline_command(self, pipeline, agent, command_b64, run_id, timeout, poll_interval):
            self.queued_command = decode_command(command_b64)
            return RunResult(123, "completed", "succeeded", "[upload] /tmp/a.txt -> /share/a.txt", 0)

    monkeypatch.setattr(shell_module, "load_azure_files_config", lambda: AzureFilesConfig("acct", "?sig=secret"))
    client = FakeClient()
    shell = PipelineShell(client)
    shell.selected_agent = AgentRef(1, "WINVM01", 10, "azure-pipeline", "online", True)
    shell.selected_pipeline = PipelineRef(7, "host-info", "project")

    shell.do_upload("--target agent /tmp/a.txt /share/a.txt")

    captured = capsys.readouterr()
    assert "run" in captured.out
    assert "[upload] /tmp/a.txt -> /share/a.txt" in captured.out
    assert "Send-AzFileOne" in client.queued_command
    assert "$LocalPath = '/tmp/a.txt'" in client.queued_command
    assert "$RemoteRoot = 'a.txt'" in client.queued_command


def test_azure_files_shell_share_and_list_commands(monkeypatch, capsys):
    from azure_pipeline_cli.azure_files import AzureFilesListItem
    from azure_pipeline_cli.shell import PipelineShell
    import azure_pipeline_cli.shell as shell_module

    calls = []
    monkeypatch.setattr(shell_module, "azure_files_list_shares", lambda: ["data"])

    def fake_create_share(share):
        calls.append(("create_share", share))
        return share

    def fake_list_directory(share, path):
        calls.append(("list", share, path))
        return [AzureFilesListItem("directory", "uploads", "-"), AzureFilesListItem("file", "a.txt", "3")]

    monkeypatch.setattr(shell_module, "azure_files_create_share", fake_create_share)
    monkeypatch.setattr(shell_module, "azure_files_list_directory", fake_list_directory)

    shell = PipelineShell(None)

    shell.do_azure_files("share list")
    shell.do_azure_files("share create data")
    shell.do_azure_files("list --share /data/uploads")

    captured = capsys.readouterr()
    assert "data" in captured.out
    assert "share created: data" in captured.out
    assert "uploads" in captured.out
    assert "a.txt" in captured.out
    assert calls == [("create_share", "data"), ("list", "data", "uploads")]


def test_azure_files_shell_directory_remove_and_sas(monkeypatch, capsys):
    from azure_pipeline_cli.azure_files import AzureFilesRemotePath
    from azure_pipeline_cli.shell import PipelineShell
    import azure_pipeline_cli.shell as shell_module

    calls = []

    def fake_remove_directory(share, path):
        calls.append(("remove_directory", share, path))
        return AzureFilesRemotePath(share, path)

    def fake_sas(share, permissions, hours):
        calls.append(("sas", share, permissions, hours))
        return "sv=2024&sig=token"

    def fake_save(values):
        calls.append(("save", values))

    monkeypatch.setattr(shell_module, "azure_files_remove_directory", fake_remove_directory)
    monkeypatch.setattr(shell_module, "generate_share_sas_token", fake_sas)
    monkeypatch.setattr(shell_module, "save_azure_files_config_values", fake_save)

    shell = PipelineShell(None)

    shell.do_azure_files("directory remove --share data uploads/a")
    shell.do_azure_files("sas")

    captured = capsys.readouterr()
    assert "directory removed: /data/uploads/a" in captured.out
    assert "SAS saved" in captured.out
    assert "sv=2024&sig=token" not in captured.out
    assert calls == [
        ("remove_directory", "data", "uploads/a"),
        ("sas", "", "rwld", 24),
        ("save", {"azure_files_sas": "sv=2024&sig=token"}),
    ]


def test_azure_files_shell_storage_account_commands(monkeypatch, capsys):
    from azure_pipeline_cli.shell import PipelineShell
    from azure_pipeline_cli.storage_accounts import StorageAccount
    import azure_pipeline_cli.shell as shell_module

    calls = []

    class FakeStorageAccounts:
        def list(self):
            calls.append(("list",))
            return [StorageAccount("st", "rg", "koreacentral", "Standard_LRS", "StorageV2")]

        def create(self, name, resource_group, location, sku, kind):
            calls.append(("create", name, resource_group, location, sku, kind))
            return StorageAccount(name, resource_group, location, sku, kind)

        def remove(self, name, resource_group):
            calls.append(("remove", name, resource_group))

        def key(self, name, resource_group=""):
            calls.append(("key", name, resource_group))
            return "secret-key"

    def fake_save(values):
        calls.append(("save", values))

    monkeypatch.setattr(shell_module, "save_azure_files_config_values", fake_save)
    shell = PipelineShell(None, storage_account_manager=FakeStorageAccounts())

    shell.do_azure_files("storage-account list")
    shell.do_azure_files("storage-account create st -g rg -l koreacentral")
    shell.do_azure_files("storage-account remove st -g rg")
    shell.do_azure_files("storage-account key --account st --resource-group rg")

    captured = capsys.readouterr()
    assert "Standard_LRS" in captured.out
    assert "storage account created: st" in captured.out
    assert "storage account removed: st" in captured.out
    assert "storage account key saved" in captured.out
    assert "secret-key" not in captured.out
    assert calls == [
        ("list",),
        ("create", "st", "rg", "koreacentral", "Standard_LRS", "StorageV2"),
        ("remove", "st", "rg"),
        ("key", "st", "rg"),
        ("save", {"azure_files_account": "st", "azure_files_account_key": "secret-key"}),
    ]


def test_use_prepares_runner_variables_and_run_reuses_them(capsys):
    from azure_pipeline_cli.models import AgentRef, PipelineRef, RunResult
    from azure_pipeline_cli.shell import PipelineShell

    calls = []
    agent = AgentRef(1, "WINVM01", 10, "pool", "online", True)
    pipeline = PipelineRef(7, "pipe", "proj")

    class FakeClient:
        def find_pipeline_candidates(self, selected_agent, project_filter=None, pipeline_filter=None):
            calls.append(("find_pipeline_candidates", selected_agent.name, project_filter, pipeline_filter))
            return [pipeline]

        def bind_agent_to_pipeline(self, selected_agent, selected_pipeline):
            calls.append(("bind_agent_to_pipeline", selected_agent.name, selected_pipeline.id))
            return selected_agent

        def ensure_queue_variables(self, project, pipeline_id, variables):
            calls.append(("ensure_queue_variables", project, pipeline_id, variables["targetPool"], variables["targetAgent"]))

        def run_pipeline_command(self, pipeline, agent, command_b64, run_id, timeout, poll_interval):
            calls.append(("run_pipeline_command", pipeline.id, agent.name))
            return RunResult(123, "completed", "succeeded", "ok", 0)

    shell = PipelineShell(FakeClient())
    shell.agents = [agent]

    shell.do_use("1")
    shell.do_run("whoami")

    captured = capsys.readouterr()
    assert "selected" in captured.out
    assert "ok" in captured.out
    assert calls == [
        ("find_pipeline_candidates", "WINVM01", None, None),
        ("bind_agent_to_pipeline", "WINVM01", 7),
        ("ensure_queue_variables", "proj", 7, "pool", "WINVM01"),
        ("run_pipeline_command", 7, "WINVM01"),
    ]


def test_agent_sync_refreshes_cache_and_agents_uses_cached_data(capsys):
    from azure_pipeline_cli.models import AgentRef
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def __init__(self):
            self.clear_calls = 0
            self.discover_calls = 0

        def clear_agent_cache(self):
            self.clear_calls += 1

        def discover_agents(self, pool_filter):
            self.discover_calls += 1
            assert pool_filter is None
            return [
                AgentRef(1, "WINVM01", 10, "azure-pipeline", "offline", True),
                AgentRef(2, "WINVM02", 11, "winvm02", "online", True),
            ]

    client = FakeClient()
    shell = PipelineShell(client)

    shell._sync_agents_once()
    shell.do_agent("list")

    captured = capsys.readouterr()
    assert "WINVM02" in captured.out
    assert "WINVM01" not in captured.out
    assert client.clear_calls == 1
    assert client.discover_calls == 1


def test_agent_sync_clears_selected_offline_agent():
    from azure_pipeline_cli.models import AgentRef, PipelineRef
    from azure_pipeline_cli.shell import PipelineShell

    class FakeClient:
        def clear_agent_cache(self):
            pass

        def discover_agents(self, pool_filter):
            return [AgentRef(1, "WINVM01", 10, "azure-pipeline", "offline", True)]

    shell = PipelineShell(FakeClient())
    shell.selected_agent = AgentRef(1, "WINVM01", 10, "azure-pipeline", "online", True)
    shell.selected_pipeline = PipelineRef(1, "pipe", "proj")
    shell.prompt = "WINVM01> "

    shell._sync_agents_once()

    assert shell.selected_agent is None
    assert shell.selected_pipeline is None
    assert shell.prompt == "evilazp(no-agent)> "


def test_delete_agent_by_visible_index_with_yes(capsys):
    from azure_pipeline_cli.models import AgentRef, PipelineRef
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def clear_agent_cache(self):
            calls.append(("clear_agent_cache",))

        def delete_agent(self, pool_id, agent_id):
            calls.append(("delete_agent", pool_id, agent_id))

    shell = PipelineShell(FakeClient())
    shell.agents = [AgentRef(7, "WINVM01", 10, "winvm02", "online", True)]
    shell._agent_cache = list(shell.agents)
    shell.selected_agent = shell.agents[0]
    shell.selected_pipeline = PipelineRef(1, "pipe", "proj")

    shell.do_agent("remove 1 --yes")

    captured = capsys.readouterr()
    assert "agent WINVM01 (7) from pool winvm02" in captured.out
    assert shell.selected_agent is None
    assert calls == [
        ("delete_agent", 10, 7),
        ("clear_agent_cache",),
    ]


def test_delete_agent_by_agent_id_and_pool_with_yes(capsys):
    from azure_pipeline_cli.models import AgentRef
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def get_pool(self, pool):
            calls.append(("get_pool", pool))
            return {"id": 10, "name": "winvm02"}

        def discover_agents(self, pool_filter):
            calls.append(("discover_agents", pool_filter))
            return [AgentRef(7, "WINVM01", 10, "winvm02", "offline", True)]

        def delete_agent(self, pool_id, agent_id):
            calls.append(("delete_agent", pool_id, agent_id))

        def clear_agent_cache(self):
            calls.append(("clear_agent_cache",))

    shell = PipelineShell(FakeClient())

    shell.do_delete_agent("--agent-id 7 --pool winvm02 --yes")

    captured = capsys.readouterr()
    assert "agent WINVM01 (7) from pool winvm02" in captured.out
    assert calls == [
        ("get_pool", "winvm02"),
        ("discover_agents", "winvm02"),
        ("delete_agent", 10, 7),
        ("clear_agent_cache",),
    ]


def test_agent_create_pool(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def ensure_pool(self, pool):
            calls.append(("ensure_pool", pool))
            return {"id": 10, "name": pool}

        def clear_agent_cache(self):
            calls.append(("clear_agent_cache",))

    shell = PipelineShell(FakeClient())

    shell.do_agent_pool("create --pool pool")

    captured = capsys.readouterr()
    assert "ready pool" in captured.out
    assert shell.pool_filter == "pool"
    assert calls == [
        ("ensure_pool", "pool"),
        ("clear_agent_cache",),
    ]


def test_agent_create_builds_from_current_session(monkeypatch, capsys):
    from types import SimpleNamespace

    import azure_pipeline_cli.shell as shell_module
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        organization = "https://dev.azure.com/org"
        pat = "pat"

        def ensure_pool(self, pool):
            calls.append(("ensure_pool", pool))
            return {"id": 10, "name": pool}

        def clear_agent_cache(self):
            calls.append(("clear_agent_cache",))

    def fake_build_from_config(config, *, quiet=False):
        calls.append(("build", config.pool, config.agent, config.runtime, config.polling, quiet))
        return SimpleNamespace(binary="/tmp/Agent.Listener.exe")

    monkeypatch.setattr(shell_module, "build_from_config", fake_build_from_config)

    shell = PipelineShell(FakeClient())
    shell.do_agent("create --pool pool --agent agent --runtime win-x64 --polling 1")

    captured = capsys.readouterr()
    assert "binary ready /tmp/Agent.Listener.exe" in captured.out
    assert shell.pool_filter == "pool"
    assert calls == [
        ("ensure_pool", "pool"),
        ("build", "pool", "agent", "win-x64", 1, True),
        ("clear_agent_cache",),
    ]


def test_agent_create_passes_sign_options(monkeypatch, capsys):
    from types import SimpleNamespace

    import azure_pipeline_cli.shell as shell_module
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        organization = "https://dev.azure.com/org"
        pat = "pat"

        def ensure_pool(self, pool):
            calls.append(("ensure_pool", pool))
            return {"id": 10, "name": pool}

        def clear_agent_cache(self):
            calls.append(("clear_agent_cache",))

    def fake_build_from_config(config, *, quiet=False):
        calls.append(("build", config.sign, config.pfx, config.pfx_pass, config.timestamp, config.cert_subject, quiet))
        return SimpleNamespace(binary="/tmp/Agent.Listener.exe")

    monkeypatch.setattr(shell_module, "build_from_config", fake_build_from_config)

    shell = PipelineShell(FakeClient())
    shell.do_agent(
        "create --pool pool --agent agent --runtime win-x64 --sign "
        "--pfx /tmp/cert.pfx --pfx-pass secret --timestamp '' --cert-subject /CN=test"
    )

    captured = capsys.readouterr()
    assert "binary ready /tmp/Agent.Listener.exe" in captured.out
    assert calls == [
        ("ensure_pool", "pool"),
        ("build", True, "/tmp/cert.pfx", "secret", "", "/CN=test", True),
        ("clear_agent_cache",),
    ]


def test_agent_create_builds_from_yaml(monkeypatch, capsys):
    from types import SimpleNamespace

    import azure_pipeline_cli.agent_builder as builder
    import azure_pipeline_cli.shell as shell_module
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    def fake_build_from_yaml(
        path,
        *,
        runtime,
        tunnel,
        relay,
        polling,
        sign=False,
        pfx="",
        pfx_pass="",
        pfx_pass_env="EVILAZP_PFX_PASS",
        timestamp="",
        sign_name="",
        sign_url="",
        cert_subject="",
        quiet=False,
    ):
        calls.append((path, runtime, tunnel, relay, polling, sign, pfx, pfx_pass, quiet))
        return SimpleNamespace(binary="/tmp/Agent.Listener")

    monkeypatch.setattr(shell_module, "build_from_yaml", fake_build_from_yaml)

    shell = PipelineShell(None)
    shell.do_agent("create --yaml .env --runtime linux-x64 --tunnel --relay --polling 5")

    captured = capsys.readouterr()
    assert "binary ready /tmp/Agent.Listener" in captured.out
    assert calls == [(".env", "linux-x64", True, True, 5, False, "", "", True)]

    calls.clear()
    shell.do_agent("create --yaml --runtime linux-x64")
    assert calls == [(str(builder.DEFAULT_YAML_PATH), "linux-x64", False, False, 0, False, "", "", True)]


def test_agent_pool_list(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def list_pools(self):
            calls.append(("list_pools",))
            return [{"id": 10, "name": "pool"}]

    shell = PipelineShell(FakeClient())

    shell.do_agent_pool("list")

    captured = capsys.readouterr()
    assert "pool" in captured.out
    assert calls == [("list_pools",)]


def test_agent_pool_open_access(capsys):
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def open_queue_access(self, project, pool):
            calls.append(("open_queue_access", project, pool))

    shell = PipelineShell(FakeClient())

    shell.do_agent_pool("open-access --project proj --pool pool")

    captured = capsys.readouterr()
    assert "proj/pool" in captured.out
    assert calls == [("open_queue_access", "proj", "pool")]


def test_agent_remove_pool_with_yes(capsys):
    from azure_pipeline_cli.models import AgentRef, PipelineRef
    from azure_pipeline_cli.shell import PipelineShell

    calls = []

    class FakeClient:
        def delete_pool(self, pool):
            calls.append(("delete_pool", pool))

        def clear_agent_cache(self):
            calls.append(("clear_agent_cache",))

    shell = PipelineShell(FakeClient(), pool="pool")
    shell.selected_agent = AgentRef(7, "WINVM01", 10, "pool", "online", True)
    shell.selected_pipeline = PipelineRef(1, "pipe", "proj")

    shell.do_agent_pool("remove --pool pool --yes")

    captured = capsys.readouterr()
    assert "agent pool pool" in captured.out
    assert shell.pool_filter is None
    assert shell.selected_agent is None
    assert shell.selected_pipeline is None
    assert calls == [
        ("delete_pool", "pool"),
        ("clear_agent_cache",),
    ]


def test_normalize_remote_command_unwraps_outer_quotes():
    from azure_pipeline_cli.cli import normalize_remote_command

    assert normalize_remote_command(["--", '"whoami /all"']) == "whoami /all"


def test_print_yaml(capsys):
    assert main(["print-yaml"]) == 0
    captured = capsys.readouterr()
    assert "pool:" in captured.out
    assert "$(targetAgent)" in captured.out
    assert "::EVILAZP_START::" in captured.out
