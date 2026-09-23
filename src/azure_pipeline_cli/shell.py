"""Interactive shell for executing commands through Azure Pipelines."""

from __future__ import annotations

import cmd
import os
import re
import shlex
import sys
import threading
import time
import uuid
from datetime import datetime

from . import __version__
from .agent_builder import (
    AgentBuildConfig,
    AgentBuilderError,
    DEFAULT_SIGN_NAME,
    DEFAULT_SIGN_SUBJECT,
    DEFAULT_SIGN_URL,
    DEFAULT_TIMESTAMP_URL,
    DEFAULT_YAML_PATH,
    build_from_config,
    build_from_yaml,
    resolve_yaml_path,
    validate_runtime,
)
from .azure_devops import QUEUE_VARIABLES, AzureDevOpsClient, AzureDevOpsError, encode_command
from .azure_files import (
    AzureFilesError,
    build_agent_transfer_script,
    create_directory as azure_files_create_directory,
    create_share as azure_files_create_share,
    download as azure_files_download,
    format_agent_transfer_display_command,
    generate_share_sas_token,
    list_directory as azure_files_list_directory,
    list_shares as azure_files_list_shares,
    load_azure_files_config,
    parse_azure_files_command,
    parse_azure_files_management_command,
    remove_directory as azure_files_remove_directory,
    remove_share as azure_files_remove_share,
    save_azure_files_config_values,
    upload as azure_files_upload,
)
from .devtunnels import DevTunnelCredentials, DevTunnelError, DevTunnelManager, parse_devtunnels_command
from .models import AgentRef, PipelineRef
from .relay_bridge import HybridConnectionError, HybridConnectionManager, RelayBridgeError, RelayBridgeManager, parse_relay_bridge_command
from .resource_groups import ResourceGroupError, ResourceGroupManager, parse_resource_group_command
from .sessions import (
    SessionRecord,
    delete_session,
    find_session,
    load_sessions,
    mark_session_used,
    masked_secret,
    print_sessions_table,
    save_session,
)
from .storage_accounts import StorageAccountError, StorageAccountManager


AGENT_METADATA_REFRESH_TTL_SECONDS = 300


class PipelineShell(cmd.Cmd):
    prompt = "evilazp(no-agent)> "

    def __init__(
        self,
        client: AzureDevOpsClient | None,
        project: str | None = None,
        pipeline: str | None = None,
        pool: str | None = None,
        timeout: float = 900,
        poll_interval: float = 0.5,
        agent_sync_interval: float = 1,
        devtunnel_manager: DevTunnelManager | None = None,
        relay_bridge_manager: RelayBridgeManager | None = None,
        hybrid_connection_manager: HybridConnectionManager | None = None,
        resource_group_manager: ResourceGroupManager | None = None,
        storage_account_manager: StorageAccountManager | None = None,
        initial_session: SessionRecord | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.project_filter = project
        self.pipeline_filter = pipeline
        self.pool_filter = pool
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.agent_sync_interval = agent_sync_interval
        self.agents: list[AgentRef] = []
        self._agent_cache: list[AgentRef] = []
        self._known_online_agent_keys: set[tuple[int, int, str]] = set()
        self.agent_cache_key: str | None = None
        self.agent_sync_error: str | None = None
        self.agent_sync_started = False
        self._run_threads: list[threading.Thread] = []
        self._agent_metadata_refresh_cache: dict[tuple[int, str], float] = {}
        self._agent_lock = threading.RLock()
        self._agent_sync_stop = threading.Event()
        self._agent_sync_thread: threading.Thread | None = None
        self.selected_agent: AgentRef | None = None
        self.selected_pipeline: PipelineRef | None = None
        self.runner_variables_key: tuple[str, int, int, str] | None = None
        self.current_session: SessionRecord | None = initial_session
        self.devtunnels = devtunnel_manager or DevTunnelManager()
        self.relay_bridge = relay_bridge_manager or RelayBridgeManager()
        self.hybrid_connections = hybrid_connection_manager or HybridConnectionManager()
        self.resource_groups = resource_group_manager or ResourceGroupManager()
        self.storage_accounts = storage_account_manager or StorageAccountManager()
        self.intro = format_banner()
        self.prompt = format_prompt(None)
        self._preloop_initialized = False
        configure_readline()

    def preloop(self) -> None:
        if not self._preloop_initialized:
            clear_screen_on_start()
            self._start_agent_sync()
            self._preloop_initialized = True

    def postloop(self) -> None:
        self._stop_agent_sync()
        self.devtunnels.shutdown()
        self.relay_bridge.shutdown()

    def cmdloop(self, intro: str | None = None) -> bool | None:
        next_intro = intro
        while True:
            try:
                return super().cmdloop(next_intro)
            except KeyboardInterrupt:
                print()
                print(warn_text("Use /exit to quit."))
                next_intro = ""

    def onecmd(self, line: str) -> bool | None:
        line = strip_prompt_prefix(line)
        try:
            if line.startswith("/"):
                parts = line[1:].split(maxsplit=1)
                command = parts[0].replace("-", "_")
                rest = parts[1] if len(parts) > 1 else ""
                if not command or not hasattr(self, f"do_{command}"):
                    print(error_text(f"unknown slash command: /{parts[0] if parts else ''}"), file=sys.stderr)
                    return None
                return super().onecmd(f"{command} {rest}".strip())
            return self.default(line)
        except KeyboardInterrupt:
            print()
            print(warn_text("Use /exit to quit."))
            return None

    def precmd(self, line: str) -> str:
        return strip_prompt_prefix(line)

    def default(self, line: str) -> bool | None:
        line = unwrap_outer_quotes(line)
        if not line.strip():
            return None
        first = line.split(maxsplit=1)[0]
        if first in {
            "--help",
            "-h",
            "agent",
            "agent-pool",
            "agent_pool",
            "agents",
            "use",
            "init",
            "connect",
            "project",
            "pools",
            "pipeline",
            "session",
            "delete-agent",
            "delete_agent",
            "devtunnels",
            "relay_bridge",
            "relay-bridge",
            "help",
            "run",
            "upload",
            "download",
            "azure-files",
            "azure_files",
            "clear",
            "exit",
            "quit",
        }:
            suggestion = "/help" if first in {"--help", "-h", "help"} else f"/{line}"
            print(error_text(f"Unknown remote command '{first}'. Did you mean {suggestion}?"), file=sys.stderr)
            return None
        return self._execute_remote_command(line)

    def _execute_remote_command(self, line: str, *, background: bool = False, display_command: str | None = None) -> bool | None:
        """Queue a pipeline run that executes a real command on the selected agent."""

        if not self.client:
            print(error_text("Not connected. Run /connect first."), file=sys.stderr)
            return None
        if not self.selected_agent or not self.selected_pipeline:
            print(error_text("No agent selected. Run /agent list then /use <index|hostname>."), file=sys.stderr)
            return None
        run_id = uuid.uuid4().hex
        agent_name = self.selected_agent.name
        try:
            self._ensure_selected_runner_variables()
            command_b64 = encode_command(line)
            if background:
                build_id = self.client.queue_pipeline_command(
                    pipeline=self.selected_pipeline,
                    agent=self.selected_agent,
                    command_b64=command_b64,
                    run_id=run_id,
                )
                print(status_text("run", f"{run_id} queued on {agent_name} (build {build_id})"))
                thread = threading.Thread(
                    target=self._wait_for_remote_command_result,
                    args=(self.selected_pipeline, build_id, run_id, line, agent_name, display_command or line),
                    name=f"evilazp-run-{build_id}",
                    daemon=True,
                )
                self._run_threads.append(thread)
                thread.start()
                return None
            result = self.client.run_pipeline_command(
                pipeline=self.selected_pipeline,
                agent=self.selected_agent,
                command_b64=command_b64,
                run_id=run_id,
                timeout=self.timeout,
                poll_interval=self.poll_interval,
            )
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return None
        self._print_remote_command_result(result, agent_name=agent_name, command_text=display_command or line)
        return None

    def _wait_for_remote_command_result(
        self,
        pipeline: PipelineRef,
        build_id: int,
        run_id: str,
        command_text: str,
        agent_name: str,
        display_command: str,
    ) -> None:
        try:
            result = self.client.wait_pipeline_command(
                pipeline=pipeline,
                build_id=build_id,
                run_id=run_id,
                command_text=command_text,
                timeout=self.timeout,
                poll_interval=self.poll_interval,
            )
        except AzureDevOpsError as exc:
            print()
            print(error_text(f"build {build_id}: {exc}"), file=sys.stderr)
            self._redisplay_prompt()
            return
        except Exception as exc:  # pragma: no cover - defensive guard for background worker failures.
            print()
            print(error_text(f"build {build_id}: {exc}"), file=sys.stderr)
            self._redisplay_prompt()
            return
        print()
        self._print_remote_command_result(result, agent_name=agent_name, command_text=display_command)
        self._redisplay_prompt()

    def _print_remote_command_result(self, result, *, agent_name: str | None = None, command_text: str | None = None) -> None:
        if agent_name or command_text:
            result_value = result.result or ("command-completed" if result.exit_code is not None else result.status) or "unknown"
            exit_value = result.exit_code if result.exit_code is not None else "?"
            target = agent_name or "unknown-agent"
            print(status_text("result", f"{target} build {result.build_id} result={result_value} exit={exit_value}", "run"))
            if command_text:
                print(muted_text(f"$ {command_text}"))
        for tunnel_url in result.tunnel_urls:
            print(status_text("tunnel", tunnel_url))
        if result.output:
            print(result.output)
        elif result.result != "succeeded":
            print(error_text(f"build {result.build_id} result={result.result}; no marked command output found"), file=sys.stderr)
        else:
            print(status_text("build", f"{result.build_id} completed with no command output"))
        if result.exit_code not in (None, 0):
            print(status_text("exit", f"{result.exit_code} build {result.build_id} result={result.result}", "warn"))

    def _redisplay_prompt(self) -> None:
        sys.stdout.write(self.prompt)
        sys.stdout.flush()

    def do_help(self, arg: str) -> None:
        """Show slash command help."""

        commands = {
            "/help": "show this command list",
            "/connect [--org ORG]": "connect with PAT from env or prompt",
            "/project": "manage Azure DevOps projects; run /project --help",
            "/pools": "list agent pools",
            "/pipeline": "manage Azure Pipelines definitions; run /pipeline --help",
            "/session": "manage saved connection sessions; run /session --help",
            "/devtunnels": "manage DevTunnel local forwards; run /devtunnels --help",
            "/relay-bridge": "manage Azure Relay Bridge, namespaces, and HCs; run /relay-bridge --help",
            "/resource-group": "manage Azure resource groups; run /resource-group --help",
            "/agent": "manage Azure Pipelines agent registrations; run /agent --help",
            "/agent-pool": "manage Azure Pipelines agent pools; run /agent-pool --help",
            "/use <index|hostname>": "select an agent and resolve its pipeline",
            "/run <command>": "execute one PowerShell command on the selected agent",
            "/upload [--target local|agent] <local> /<share>/<path>": "upload a file or directory to Azure Files",
            "/download [--target local|agent] /<share>/<path> <local>": "download a file or directory from Azure Files",
            "/azure-files": "manage Azure Files shares, directories, listings, and SAS tokens",
            "/init": "interactive setup wizard for project/pool/repo/pipeline",
            "/clear": "clear the terminal",
            "/exit": "exit the shell",
        }
        if arg:
            key = f"/{arg.strip().lstrip('/')}"
            for command, description in commands.items():
                if command.split()[0] == key:
                    print(f"{command:<24} {description}")
                    return
            print(error_text(f"unknown help topic: {arg}"), file=sys.stderr)
            return
        print(section_title("Commands"))
        print_table(("Command", "Description"), list(commands.items()))

    def do_connect(self, arg: str) -> None:
        """Connect to Azure DevOps using a PAT."""

        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        org = options.get("org") or input_default("Organization", self.client.organization if self.client else "")
        pat = options.get("pat") or os.environ.get("AZURE_DEVOPS_EXT_PAT")
        if not pat:
            try:
                pat = input("PAT: ").strip()
            except EOFError:
                print(
                    error_text("PAT is required. Set AZURE_DEVOPS_EXT_PAT or run /connect in an interactive terminal."),
                    file=sys.stderr,
                )
                return
        if not pat:
            print(error_text("PAT is required."), file=sys.stderr)
            return
        try:
            self.client = AzureDevOpsClient(org, pat)
            projects = self.client.list_projects()
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        session_record = SessionRecord(
            name=options.get("name") or self.client.org_name,
            organization=self.client.organization,
            pat=pat,
            project=self.project_filter,
            pipeline=self.pipeline_filter,
            pool=self.pool_filter,
            sp_tenant_id=options.get("sp-tenant-id"),
            sp_client_id=options.get("sp-client-id"),
            sp_client_secret=options.get("sp-client-secret"),
        )
        self.current_session = session_record
        if options.get("save", "true").lower() not in {"false", "0", "no"}:
            save_session(session_record)
            print(status_text("saved", f"saved session '{session_record.name}'"))
        self._reset_agent_cache()
        self._start_agent_sync()
        print(status_text("connected", f"{self.client.organization} ({len(projects)} project(s))"))

    def do_session(self, arg: str) -> None:
        """List, use, or remove saved sessions."""

        tokens = shlex.split(arg)
        if not tokens or tokens[0] in {"--help", "-h", "help"}:
            print_session_help()
            return
        sessions = load_sessions()
        if tokens[0] == "list":
            if len(tokens) != 1:
                print(error_text("usage: /session list"), file=sys.stderr)
                return
            print_sessions_table(sessions)
            return
        if tokens[0] == "use":
            if len(tokens) != 2:
                print(error_text("usage: /session use <index|name>"), file=sys.stderr)
                return
            self._use_session(tokens[1], sessions)
            return
        if tokens[0] == "remove":
            if len(tokens) != 2:
                print(error_text("usage: /session remove <index|name>"), file=sys.stderr)
                return
            session = find_session(tokens[1], sessions)
            if not session:
                print(error_text(f"session not found: {tokens[1]}"), file=sys.stderr)
                return
            if delete_session(session.name):
                print(status_text("removed", f"session '{session.name}'"))
            return
        print(error_text("usage: /session <list|use|remove>. Run /session --help."), file=sys.stderr)

    def _use_session(self, target: str, sessions: list[SessionRecord]) -> None:
        session = find_session(target, sessions)
        if not session:
            print(error_text(f"session not found: {target}"), file=sys.stderr)
            return
        try:
            self.client = AzureDevOpsClient(session.organization, session.pat)
            projects = self.client.list_projects()
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        mark_session_used(session.name)
        self.current_session = session
        self.project_filter = session.project
        self.pipeline_filter = session.pipeline
        self.pool_filter = session.pool
        self._reset_agent_cache()
        self._start_agent_sync()
        self.selected_agent = None
        self.selected_pipeline = None
        self.runner_variables_key = None
        self.prompt = format_prompt(None)
        print(status_text("connected", f"{self.client.organization} ({len(projects)} project(s)) via session '{session.name}'"))

    def do_devtunnels(self, arg: str) -> None:
        """Manage local forwards to agent-hosted DevTunnels."""

        if arg.strip() in {"--help", "-h", "help"}:
            print_devtunnels_help()
            return
        try:
            command = parse_devtunnels_command(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return

        if command.action == "list":
            rows = [
                (
                    item.tunnel_id,
                    f"remote:{item.remote_port}",
                    f"local:127.0.0.1:{item.local_port}",
                    item.status,
                )
                for item in self.devtunnels.list()
            ]
            if rows:
                print_table(("Tunnel", "Remote", "Local", "Status"), rows)
            else:
                print("No active DevTunnel connections.")
            return

        if command.action == "stop":
            stopped = self.devtunnels.stop(command.target or "")
            if not stopped:
                print(error_text(f"devtunnel connection not found: {command.target}"), file=sys.stderr)
                return
            for item in stopped:
                print(status_text("devtunnels", f"stopped 127.0.0.1:{item.local_port}"))
            return

        if command.action == "sp":
            self._save_devtunnel_sp(command.sp_tenant_id or "", command.sp_client_id or "", command.sp_client_secret or "")
            return

        credentials = self._devtunnel_credentials()
        if not credentials:
            print(
                error_text(
                    "Current session has no DevTunnel SP credentials. Reconnect with /connect "
                    "--sp-tenant-id <id> --sp-client-id <id> --sp-client-secret <secret>, "
                    "or update this session with /devtunnels sp --tenant-id <id> --sp-client-id <id> --sp-secret <secret>."
                ),
                file=sys.stderr,
            )
            return
        assert command.spec is not None
        try:
            connection = self.devtunnels.start(command.spec, credentials)
        except DevTunnelError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        print(status_text("devtunnels", f"connected 127.0.0.1:{connection.local_port} -> {connection.tunnel_id}:{connection.remote_port}"))

    def do_relay_bridge(self, arg: str) -> None:
        """Manage Azure Relay Bridge SDK helper processes."""

        if arg.strip() in {"--help", "-h", "help"}:
            print_relay_bridge_help()
            return
        try:
            command = parse_relay_bridge_command(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return

        if command.action == "list":
            rows = []
            for connection in self.relay_bridge.list():
                for forward in connection.forwards:
                    rows.append(
                        (
                            str(connection.id),
                            _relay_bridge_mode_label(forward.mode),
                            forward.relay_name,
                            forward.endpoint,
                            connection.status,
                        )
                    )
            if rows:
                print_table(("ID", "Mode", "Relay", "Local/Target", "Status"), rows)
            else:
                print("No active Relay Bridge connections.")
            return

        if command.action == "stop":
            stopped = self.relay_bridge.stop(command.target or "")
            if not stopped:
                print(error_text(f"relay-bridge connection not found: {command.target}"), file=sys.stderr)
                return
            for item in stopped:
                print(status_text("relay-bridge", f"stopped {item.id}"))
            return

        if command.action == "ns-list":
            try:
                items = self.hybrid_connections.list_namespaces(command.resource_group or "")
            except HybridConnectionError as exc:
                print(error_text(str(exc)), file=sys.stderr)
                return
            if not items:
                print("No Relay namespaces found.")
                return
            print_table(
                ("Name", "Location", "State", "Endpoint"),
                [(item.name, item.location or "-", item.provisioning_state or "-", item.service_bus_endpoint or "-") for item in items],
            )
            return

        if command.action == "ns-create":
            try:
                item = self.hybrid_connections.create_namespace(command.resource_group or "", command.namespace or "", command.location)
            except HybridConnectionError as exc:
                print(error_text(str(exc)), file=sys.stderr)
                return
            print(status_text("relay-bridge", f"created namespace {item.name}"))
            return

        if command.action == "ns-delete":
            try:
                self.hybrid_connections.delete_namespace(command.resource_group or "", command.namespace or "")
            except HybridConnectionError as exc:
                print(error_text(str(exc)), file=sys.stderr)
                return
            print(status_text("relay-bridge", f"removed namespace {command.namespace}"))
            return

        if command.action == "ns-keys":
            try:
                keys = self.hybrid_connections.get_namespace_keys(
                    command.resource_group or "",
                    command.namespace or "",
                    command.auth_rule or "RootManageSharedAccessKey",
                )
            except HybridConnectionError as exc:
                print(error_text(str(exc)), file=sys.stderr)
                return
            print(keys.primary_connection_string)
            return

        if command.action == "hc-list":
            try:
                items = self.hybrid_connections.list(command.resource_group or "", command.namespace or "")
            except HybridConnectionError as exc:
                print(error_text(str(exc)), file=sys.stderr)
                return
            if not items:
                print("No Hybrid Connections found.")
                return
            print_table(
                ("Name", "Client Auth", "Metadata"),
                [(item.name, str(item.requires_client_authorization).lower(), item.user_metadata or "-") for item in items],
            )
            return

        if command.action == "hc-create":
            try:
                item = self.hybrid_connections.create(
                    command.resource_group or "",
                    command.namespace or "",
                    command.hc_name or "",
                    command.requires_client_authorization,
                )
            except HybridConnectionError as exc:
                print(error_text(str(exc)), file=sys.stderr)
                return
            print(status_text("relay-bridge", f"created HC {item.name}"))
            return

        if command.action == "hc-delete":
            try:
                self.hybrid_connections.delete(command.resource_group or "", command.namespace or "", command.hc_name or "")
            except HybridConnectionError as exc:
                print(error_text(str(exc)), file=sys.stderr)
                return
            print(status_text("relay-bridge", f"removed HC {command.hc_name}"))
            return

        assert command.spec is not None
        try:
            connection = self.relay_bridge.start(command.spec)
        except RelayBridgeError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        forwards = ", ".join(f"{_relay_bridge_mode_label(item.mode)} {item.relay_name} -> {item.endpoint}" for item in connection.forwards)
        print(status_text("relay-bridge", f"started {connection.id}: {forwards}"))

    def do_resource_group(self, arg: str) -> None:
        """Manage Azure resource groups."""

        if arg.strip() in {"--help", "-h", "help"}:
            print_resource_group_help()
            return
        try:
            command = parse_resource_group_command(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return

        if command.action == "list":
            try:
                items = self.resource_groups.list()
            except ResourceGroupError as exc:
                print(error_text(str(exc)), file=sys.stderr)
                return
            if not items:
                print("No resource groups found.")
                return
            print_table(
                ("Name", "Location", "State"),
                [(item.name, item.location or "-", item.provisioning_state or "-") for item in items],
            )
            return

        if command.action == "create":
            try:
                item = self.resource_groups.create(command.name or "", command.location or "")
            except ResourceGroupError as exc:
                print(error_text(str(exc)), file=sys.stderr)
                return
            print(status_text("resource-group", f"created {item.name}"))
            return

        if not command.yes:
            answer = input(f"Remove resource group '{command.name}' and all contained Azure resources? Type 'yes' to continue: ")
            if answer.strip().lower() != "yes":
                print(warn_text("cancelled"))
                return
        try:
            self.resource_groups.remove(command.name or "")
        except ResourceGroupError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        print(status_text("resource-group", f"removed {command.name}"))

    def do_project(self, arg: str) -> None:
        """Manage Azure DevOps projects."""

        tokens = shlex.split(arg)
        action = tokens[0].lower() if tokens else ""
        if action in {"--help", "-h", "help"}:
            print_project_help()
            return
        if not self._require_client():
            return
        if not tokens:
            print(error_text("usage: /project <list|create|remove>. Run /project --help."), file=sys.stderr)
            return
        rest = shlex.join(tokens[1:])
        if action == "list":
            self._project_list()
            return
        if action == "create":
            self._project_create(rest)
            return
        if action == "remove":
            self._project_remove(rest)
            return
        print(error_text(f"unknown /project action: {tokens[0]}"), file=sys.stderr)

    def _project_list(self) -> None:
        try:
            for project in self.client.list_projects():
                print(project)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)

    def _format_project_not_found(self, project: str, exc: Exception | None = None) -> str:
        """Return a project-not-found message with live project names when possible."""

        message = f"Project not found: {project}"
        try:
            projects = self.client.list_projects()
        except AzureDevOpsError:
            projects = []
        if projects:
            message += f". Available projects: {', '.join(projects)}"
        elif exc:
            message += f". {exc}"
        return message

    def _project_create(self, arg: str) -> None:
        try:
            target, options = parse_target_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if target:
            print(error_text("usage: /project create --project PROJECT [--visibility private|public] [--process Basic]"), file=sys.stderr)
            return
        project = options.get("project")
        if not project:
            print(error_text("usage: /project create --project PROJECT [--visibility private|public] [--process Basic]"), file=sys.stderr)
            return
        visibility = options.get("visibility") or "private"
        process = options.get("process") or "Basic"
        if project.startswith("/") or not project.strip():
            print(error_text(f"invalid project name: {project}"), file=sys.stderr)
            return
        try:
            self.client.ensure_project(project, visibility, process)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        self.project_filter = project
        print(status_text("project", f"ready {project}"))

    def _project_remove(self, arg: str) -> None:
        try:
            target, options = parse_target_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        project = options.get("project") or target
        if not project:
            print(error_text("usage: /project remove <project|--project PROJECT> [--yes]"), file=sys.stderr)
            return
        if "yes" not in options and not confirm(f"Delete project {project}?", default=False):
            print(warn_text("remove cancelled"))
            return
        try:
            self.client.delete_project(project)
        except AzureDevOpsError as exc:
            if is_project_not_found_error(exc, project):
                print(error_text(self._format_project_not_found(project, exc)), file=sys.stderr)
            else:
                print(error_text(str(exc)), file=sys.stderr)
            return
        if self.project_filter == project:
            self.project_filter = None
        if self.selected_pipeline and self.selected_pipeline.project == project:
            self.selected_pipeline = None
            self.selected_agent = None
            self.runner_variables_key = None
            self.prompt = format_prompt(None)
        print(status_text("removed", f"project {project}"))

    def do_pools(self, _arg: str) -> None:
        """List agent pools."""

        if not self._require_client():
            return
        try:
            rows = [(str(pool.get("id")), str(pool.get("name"))) for pool in self.client.list_pools()]
            print_table(("ID", "Pool"), rows)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)

    def do_pipeline(self, arg: str) -> None:
        """Manage Azure Pipelines definitions."""

        tokens = shlex.split(arg)
        action = tokens[0].lower() if tokens else ""
        if action in {"--help", "-h", "help"}:
            print_pipeline_help()
            return
        if not self._require_client():
            return
        if not tokens:
            print(error_text("usage: /pipeline <list|create|remove>. Run /pipeline --help."), file=sys.stderr)
            return
        rest = shlex.join(tokens[1:])
        if action == "list":
            self._pipeline_list(rest)
            return
        if action == "create":
            self._pipeline_create(rest)
            return
        if action == "remove":
            self._pipeline_remove(rest)
            return
        print(error_text(f"unknown /pipeline action: {tokens[0]}"), file=sys.stderr)

    def _pipeline_list(self, arg: str) -> None:
        try:
            target, options = parse_target_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if target:
            print(error_text("usage: /pipeline list --project PROJECT"), file=sys.stderr)
            return
        project = options.get("project")
        if not project:
            print(error_text("usage: /pipeline list --project PROJECT"), file=sys.stderr)
            return
        try:
            rows = [(str(pipeline.id), pipeline.name, pipeline.pool_name or "-") for pipeline in self.client.list_pipelines(project)]
            print_table(("ID", "Pipeline", "Pool"), rows)
        except AzureDevOpsError as exc:
            if is_project_not_found_error(exc, project):
                print(error_text(self._format_project_not_found(project, exc)), file=sys.stderr)
            else:
                print(error_text(str(exc)), file=sys.stderr)

    def _pipeline_create(self, arg: str) -> None:
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if missing_options(options, "project", "repo", "pipeline", "pool"):
            print(error_text(f"usage: /pipeline create --project PROJECT --repo REPO --pipeline PIPELINE --pool POOL [--branch main] [--yml-path azure-pipelines.yml]"), file=sys.stderr)
            return
        project = options["project"]
        repo = options["repo"]
        pipeline = options["pipeline"]
        pool = options["pool"]
        branch = options.get("branch") or "main"
        yml_path = options.get("yml-path") or "azure-pipelines.yml"
        if project.startswith("/") or not project.strip():
            print(error_text(f"invalid project name: {project}"), file=sys.stderr)
            return
        try:
            if not self.client.get_project(project):
                raise AzureDevOpsError(f"Project not found: {project}")
            if hasattr(self.client, "clear_pool_cache"):
                self.client.clear_pool_cache()
            resolved_pool = self.client.resolve_pool_name(pool)
            if resolved_pool != pool:
                print(status_text("resolved", f"'{pool}' to agent pool '{resolved_pool}'"))
                pool = resolved_pool
            self.client.ensure_pool(pool)
            repo_data = self.client.ensure_repo(project, repo)
            from .pipeline_yaml import render_pipeline_yaml

            self.client.push_file(project, repo_data["id"], branch, yml_path, render_pipeline_yaml())
            print(status_text("pushed", f"{yml_path} to {repo}@{branch}"))
            created_pipeline = self.client.ensure_pipeline(project, pipeline, repo, branch, yml_path, pool)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        self.project_filter = project
        self.pipeline_filter = str(created_pipeline.id)
        self.pool_filter = pool
        self._reset_agent_cache()
        print(status_text("pipeline", f"ready {created_pipeline.project}/{created_pipeline.name} ({created_pipeline.id})"))

    def _pipeline_remove(self, arg: str) -> None:
        try:
            target, options = parse_target_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        project = options.get("project") or self.project_filter
        pipeline_target = options.get("pipeline") or target
        if not project or not pipeline_target:
            print(error_text("usage: /pipeline remove <pipeline|--pipeline PIPELINE> --project PROJECT [--yes]"), file=sys.stderr)
            return
        try:
            pipeline = self._resolve_pipeline_target(project, pipeline_target)
        except AzureDevOpsError as exc:
            if is_project_not_found_error(exc, project):
                print(error_text(self._format_project_not_found(project, exc)), file=sys.stderr)
            else:
                print(error_text(str(exc)), file=sys.stderr)
            return
        if not pipeline:
            return
        if "yes" not in options and not confirm(f"Delete pipeline {project}/{pipeline.name} ({pipeline.id})?", default=False):
            print(warn_text("remove cancelled"))
            return
        try:
            self.client.delete_pipeline(project, pipeline.id)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if self.selected_pipeline and self.selected_pipeline.project == project and self.selected_pipeline.id == pipeline.id:
            self.selected_pipeline = None
            self.selected_agent = None
            self.runner_variables_key = None
            self.prompt = format_prompt(None)
        if self.pipeline_filter in {pipeline.name, str(pipeline.id)}:
            self.pipeline_filter = None
        print(status_text("removed", f"pipeline {project}/{pipeline.name} ({pipeline.id})"))

    def do_agent(self, arg: str) -> None:
        """Manage Azure Pipelines agent registrations."""

        tokens = shlex.split(arg)
        action = tokens[0].lower() if tokens else ""
        if action in {"--help", "-h", "help"}:
            print_agent_help()
            return
        if not tokens:
            print(error_text("usage: /agent <list|create|remove>. Run /agent --help."), file=sys.stderr)
            return
        rest = shlex.join(tokens[1:])
        if action == "list":
            self._agent_list(rest)
            return
        if action == "create":
            self._agent_create(rest)
            return
        if action == "remove":
            self._agent_remove(rest)
            return
        print(error_text(f"unknown /agent action: {tokens[0]}"), file=sys.stderr)

    def do_agent_pool(self, arg: str) -> None:
        """Manage Azure Pipelines agent pools."""

        tokens = shlex.split(arg)
        action = tokens[0].lower() if tokens else ""
        if action in {"--help", "-h", "help"}:
            print_agent_pool_help()
            return
        if not tokens:
            print(error_text("usage: /agent-pool <list|create|open-access|remove>. Run /agent-pool --help."), file=sys.stderr)
            return
        rest = shlex.join(tokens[1:])
        if action == "list":
            self._agent_pool_list(rest)
            return
        if action == "create":
            self._agent_pool_create(rest)
            return
        if action == "open-access":
            self._agent_pool_open_access(rest)
            return
        if action == "remove":
            self._agent_pool_remove(rest)
            return
        print(error_text(f"unknown /agent-pool action: {tokens[0]}"), file=sys.stderr)

    def do_agents(self, _arg: str) -> None:
        """Deprecated compatibility shim for /agent list."""

        print(error_text("usage: /agent list [--refresh|--all]"), file=sys.stderr)

    def _agent_list(self, arg: str) -> None:
        """List online agents by hostname."""

        if not self._require_client():
            return
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        refresh = "refresh" in options
        pool_filter = None if "all" in options else self.pool_filter
        cache_key = pool_filter or "*"
        try:
            if refresh:
                self._sync_agents_once()
            with self._agent_lock:
                cached_agents = list(self._agent_cache)
                self.agent_cache_key = cache_key
                self.agents = filter_online_agents(cached_agents, pool_filter)
                sync_error = self.agent_sync_error
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if sync_error and not self.agents:
            print(error_text(f"agent sync error: {sync_error}"), file=sys.stderr)
        if not self.agents:
            suffix = f" in pool '{pool_filter}'" if pool_filter else ""
            print(warn_text(f"No online agents found{suffix}."))
            return
        rows = [
            (str(index), agent.name, agent.pool_name, agent.status)
            for index, agent in enumerate(self.agents, start=1)
        ]
        print_table(("#", "Agent", "Pool", "Status"), rows)

    def _agent_create(self, arg: str) -> None:
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        runtime = options.get("runtime")
        if not runtime:
            print_agent_create_usage(file=sys.stderr)
            return
        try:
            validate_runtime(runtime)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        polling_raw = options.get("polling", "0")
        try:
            polling = int(polling_raw)
        except ValueError:
            print(error_text(f"invalid polling seconds: {polling_raw}"), file=sys.stderr)
            return
        try:
            if options.get("yaml"):
                result = build_from_yaml(
                    resolve_yaml_path(options["yaml"]),
                    runtime=runtime,
                    tunnel="tunnel" in options,
                    relay="relay" in options,
                    polling=polling,
                    sign="sign" in options,
                    pfx=options.get("pfx") or "",
                    pfx_pass=options.get("pfx-pass") or "",
                    pfx_pass_env=options.get("pfx-pass-env") or "EVILAZP_PFX_PASS",
                    timestamp=options.get("timestamp", DEFAULT_TIMESTAMP_URL),
                    sign_name=options.get("sign-name", DEFAULT_SIGN_NAME),
                    sign_url=options.get("sign-url", DEFAULT_SIGN_URL),
                    cert_subject=options.get("cert-subject", DEFAULT_SIGN_SUBJECT),
                    quiet=True,
                )
            else:
                if not self._require_client():
                    return
                pool = options.get("pool")
                agent = options.get("agent")
                if not pool or not agent:
                    print_agent_create_usage(file=sys.stderr)
                    return
                self.client.ensure_pool(pool)
                result = build_from_config(
                    AgentBuildConfig(
                        url=self.client.organization,
                        pat=self.client.pat or "",
                        pool=pool,
                        agent=agent,
                        runtime=runtime,
                        tunnel="tunnel" in options,
                        relay="relay" in options,
                        polling=polling,
                        sp_tenant=options.get("sp-tenant") or "",
                        sp_client=options.get("sp-client") or "",
                        sp_secret=options.get("sp-secret") or "",
                        tunnel_id=options.get("tunnel-id") or "",
                        tunnel_ports=options.get("tunnel-ports") or "",
                        relay_connection_string=options.get("relay-connection-string") or "",
                        relay_local_forward=tuple(split_option_list(options.get("relay-local-forward"))),
                        relay_remote_forward=tuple(split_option_list(options.get("relay-remote-forward"))),
                        relay_remote_http_forward=tuple(split_option_list(options.get("relay-remote-http-forward"))),
                        sign="sign" in options,
                        pfx=options.get("pfx") or "",
                        pfx_pass=options.get("pfx-pass") or "",
                        pfx_pass_env=options.get("pfx-pass-env") or "EVILAZP_PFX_PASS",
                        timestamp=options.get("timestamp", DEFAULT_TIMESTAMP_URL),
                        sign_name=options.get("sign-name", DEFAULT_SIGN_NAME),
                        sign_url=options.get("sign-url", DEFAULT_SIGN_URL),
                        cert_subject=options.get("cert-subject", DEFAULT_SIGN_SUBJECT),
                    ),
                    quiet=True,
                )
                self.pool_filter = pool
                self._reset_agent_cache()
        except (AgentBuilderError, AzureDevOpsError, ValueError) as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        print(status_text("agent", f"binary ready {result.binary}"))

    def _agent_pool_list(self, arg: str) -> None:
        if not self._require_client():
            return
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if options:
            print(error_text("usage: /agent-pool list"), file=sys.stderr)
            return
        try:
            rows = [(str(pool.get("id")), str(pool.get("name"))) for pool in self.client.list_pools()]
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        print_table(("ID", "Pool"), rows)

    def _agent_pool_create(self, arg: str) -> None:
        if not self._require_client():
            return
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        pool = options.get("pool")
        if not pool:
            print(error_text("usage: /agent-pool create --pool POOL"), file=sys.stderr)
            return
        try:
            created = self.client.ensure_pool(pool)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        self.pool_filter = str(created.get("name") or pool)
        self._reset_agent_cache()
        print(status_text("pool", f"ready {self.pool_filter}"))

    def _agent_pool_open_access(self, arg: str) -> None:
        if not self._require_client():
            return
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        project = options.get("project")
        pool = options.get("pool")
        if not project or not pool:
            print(error_text("usage: /agent-pool open-access --project PROJECT --pool POOL"), file=sys.stderr)
            return
        try:
            self.client.open_queue_access(project, pool)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        print(status_text("open-access", f"{project}/{pool}"))

    def _agent_remove(self, arg: str) -> None:
        if not self._require_client():
            return
        try:
            target, options = parse_target_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if not target and "agent-id" not in options:
            print(error_text("usage: /agent remove <index|hostname> [--pool POOL] [--yes]"), file=sys.stderr)
            print(error_text("usage: /agent remove --agent-id ID --pool POOL [--yes]"), file=sys.stderr)
            return
        try:
            agent = self._resolve_delete_agent_target(target, options)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if not agent:
            return
        if "yes" not in options and not confirm(
            f"Delete agent {agent.name} ({agent.id}) from pool {agent.pool_name} ({agent.pool_id})?",
            default=False,
        ):
            print(warn_text("remove cancelled"))
            return
        try:
            self.client.delete_agent(agent.pool_id, agent.id)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if self.selected_agent and self.selected_agent.id == agent.id and self.selected_agent.pool_id == agent.pool_id:
            self.selected_agent = None
            self.selected_pipeline = None
            self.runner_variables_key = None
            self.prompt = format_prompt(None)
        self._reset_agent_cache()
        print(status_text("removed", f"agent {agent.name} ({agent.id}) from pool {agent.pool_name}"))

    def _agent_pool_remove(self, arg: str) -> None:
        if not self._require_client():
            return
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        pool = options.get("pool") or ""
        if not pool:
            print(error_text("usage: /agent-pool remove --pool POOL [--yes]"), file=sys.stderr)
            return
        if "yes" not in options and not confirm(f"Delete agent pool {pool}?", default=False):
            print(warn_text("remove cancelled"))
            return
        try:
            self.client.delete_pool(pool)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if self.pool_filter == pool:
            self.pool_filter = None
        if self.selected_agent and self.selected_agent.pool_name == pool:
            self.selected_agent = None
            self.selected_pipeline = None
            self.runner_variables_key = None
            self.prompt = format_prompt(None)
        self._reset_agent_cache()
        print(status_text("removed", f"agent pool {pool}"))

    def do_use(self, arg: str) -> None:
        """Select an agent by /agent list index or hostname."""

        if not self._require_client():
            return
        if not self.agents:
            self._agent_list("")
        if not self.agents:
            return
        target = arg.strip()
        if not target:
            print(error_text("usage: /use <index|hostname>"), file=sys.stderr)
            return
        agent = self._find_agent(target)
        if not agent:
            print(error_text(f"agent not found: {target}"), file=sys.stderr)
            return
        try:
            candidates = self.client.find_pipeline_candidates(
                agent,
                project_filter=self.project_filter,
                pipeline_filter=self.pipeline_filter,
            )
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if not candidates:
            print(error_text(f"No pipeline found for agent pool {agent.pool_name}."), file=sys.stderr)
            return
        pipeline = candidates[0] if len(candidates) == 1 else self._choose_pipeline(candidates)
        if not pipeline:
            return
        bound_agent = self.client.bind_agent_to_pipeline(agent, pipeline)
        previous_key = self.runner_variables_key
        self.selected_agent = bound_agent
        self.selected_pipeline = pipeline
        self.runner_variables_key = None
        try:
            self._ensure_selected_runner_variables()
        except AzureDevOpsError as exc:
            self.selected_agent = None
            self.selected_pipeline = None
            self.runner_variables_key = previous_key
            self.prompt = format_prompt(None)
            print(error_text(str(exc)), file=sys.stderr)
            return
        self.prompt = format_prompt(agent.name)
        print(status_text("selected", f"{agent.name} via {pipeline.project}/{pipeline.name}"))

    def do_delete_agent(self, arg: str) -> None:
        """Deprecated compatibility shim for /agent remove."""

        self._agent_remove(arg)

    def do_run(self, arg: str) -> bool | None:
        """Run a command on the selected agent."""

        if not arg.strip():
            print(error_text("usage: /run <command>"), file=sys.stderr)
            return None
        return self._execute_remote_command(unwrap_outer_quotes(arg), background=True)

    def do_upload(self, arg: str) -> None:
        """Upload a local file or directory to Azure Files."""

        try:
            command = parse_azure_files_command("upload", arg)
            if command.target == "agent":
                return self._execute_agent_azure_files_command(command)
            result = azure_files_upload(command.source, command.destination, overwrite=command.overwrite)
        except AzureFilesError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        print(status_text("upload", f"{result.source} -> {result.destination}"))

    def do_download(self, arg: str) -> None:
        """Download a file or directory from Azure Files."""

        try:
            command = parse_azure_files_command("download", arg)
            if command.target == "agent":
                return self._execute_agent_azure_files_command(command)
            result = azure_files_download(command.source, command.destination, overwrite=command.overwrite)
        except AzureFilesError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        print(status_text("download", f"{result.source} -> {result.destination}"))

    def _execute_agent_azure_files_command(self, command) -> bool | None:
        try:
            config = load_azure_files_config()
            self._refresh_selected_agent_metadata()
            shell = agent_transfer_shell(self.selected_agent)
            script = build_agent_transfer_script(command, config, shell=shell)
            display_command = format_agent_transfer_display_command(command)
        except AzureFilesError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return None
        return self._execute_remote_command(script, display_command=display_command)

    def do_azure_files(self, arg: str) -> None:
        """Manage Azure Files shares, directories, listings, and SAS tokens."""

        if arg.strip() in {"--help", "-h", "help"}:
            print_azure_files_help()
            return
        try:
            command = parse_azure_files_management_command(arg)
            if command.action == "share-list":
                shares = azure_files_list_shares()
                if shares:
                    print_table(("Share",), [(item,) for item in shares])
                else:
                    print("No Azure Files shares found.")
                return
            if command.action == "share-create":
                share = azure_files_create_share(command.share)
                print(status_text("azure-files", f"share created: {share}"))
                return
            if command.action == "share-remove":
                share = azure_files_remove_share(command.share)
                print(status_text("azure-files", f"share removed: {share}"))
                return
            if command.action == "directory-create":
                remote = azure_files_create_directory(command.share, command.path)
                print(status_text("azure-files", f"directory created: {remote.display}"))
                return
            if command.action == "directory-remove":
                remote = azure_files_remove_directory(command.share, command.path)
                print(status_text("azure-files", f"directory removed: {remote.display}"))
                return
            if command.action == "list":
                items = azure_files_list_directory(command.share, command.path)
                if items:
                    print_table(("Type", "Name", "Size"), [(item.type, item.name, item.size) for item in items])
                else:
                    target = f"/{command.share}/{command.path}".rstrip("/")
                    print(f"No Azure Files entries found at {target}.")
                return
            if command.action == "sas":
                token = generate_share_sas_token(command.share, permissions=command.permissions, hours=command.hours)
                save_azure_files_config_values({"azure_files_sas": token})
                print(status_text("azure-files", f"SAS saved to {DEFAULT_YAML_PATH}"))
                return
            if command.action == "storage-account-list":
                accounts = self.storage_accounts.list()
                if accounts:
                    print_table(
                        ("Name", "Resource Group", "Location", "SKU", "Kind"),
                        [(item.name, item.resource_group, item.location, item.sku, item.kind) for item in accounts],
                    )
                else:
                    print("No Azure Storage accounts found.")
                return
            if command.action == "storage-account-create":
                account = self.storage_accounts.create(command.share, command.resource_group, command.location, command.sku, command.kind)
                print(status_text("azure-files", f"storage account created: {account.name}"))
                return
            if command.action == "storage-account-remove":
                self.storage_accounts.remove(command.share, command.resource_group)
                print(status_text("azure-files", f"storage account removed: {command.share}"))
                return
            if command.action == "storage-account-key":
                key = self.storage_accounts.key(command.share, command.resource_group)
                save_azure_files_config_values({"azure_files_account": command.share, "azure_files_account_key": key})
                print(status_text("azure-files", f"storage account key saved to {DEFAULT_YAML_PATH}"))
                return
        except AzureFilesError as exc:
            print(error_text(str(exc)), file=sys.stderr)
        except StorageAccountError as exc:
            print(error_text(str(exc)), file=sys.stderr)

    def do_create_project(self, arg: str) -> None:
        """Create or reuse an Azure DevOps project."""

        if not self._require_client():
            return
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        project = options.get("project")
        if not project:
            print(error_text("usage: /project create --project PROJECT [--visibility private|public] [--process Basic]"), file=sys.stderr)
            return
        visibility = options.get("visibility") or "private"
        process = options.get("process") or "Basic"
        if project.startswith("/") or not project.strip():
            print(error_text(f"invalid project name: {project}"), file=sys.stderr)
            return
        try:
            self.client.ensure_project(project, visibility, process)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        self.project_filter = project
        print(status_text("project", f"ready {project}"))

    def do_create_pipeline(self, arg: str) -> None:
        """Create or reuse a YAML pipeline in an existing project."""

        if not self._require_client():
            return
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if missing_options(options, "project", "repo", "pipeline", "pool"):
            print(error_text("usage: /pipeline create --project PROJECT --repo REPO --pipeline PIPELINE --pool POOL [--branch main] [--yml-path azure-pipelines.yml]"), file=sys.stderr)
            return
        project = options["project"]
        repo = options["repo"]
        pipeline = options["pipeline"]
        pool = options["pool"]
        branch = options.get("branch") or "main"
        yml_path = options.get("yml-path") or "azure-pipelines.yml"
        if project.startswith("/") or not project.strip():
            print(error_text(f"invalid project name: {project}"), file=sys.stderr)
            return
        try:
            if not self.client.get_project(project):
                raise AzureDevOpsError(f"Project not found: {project}")
            if hasattr(self.client, "clear_pool_cache"):
                self.client.clear_pool_cache()
            resolved_pool = self.client.resolve_pool_name(pool)
            if resolved_pool != pool:
                print(status_text("resolved", f"'{pool}' to agent pool '{resolved_pool}'"))
                pool = resolved_pool
            self.client.ensure_pool(pool)
            repo_data = self.client.ensure_repo(project, repo)
            from .pipeline_yaml import render_pipeline_yaml

            self.client.push_file(project, repo_data["id"], branch, yml_path, render_pipeline_yaml())
            print(status_text("pushed", f"{yml_path} to {repo}@{branch}"))
            created_pipeline = self.client.ensure_pipeline(project, pipeline, repo, branch, yml_path, pool)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        self.project_filter = project
        self.pipeline_filter = str(created_pipeline.id)
        self.pool_filter = pool
        self._reset_agent_cache()
        print(status_text("pipeline", f"ready {created_pipeline.project}/{created_pipeline.name} ({created_pipeline.id})"))

    def do_init(self, arg: str) -> None:
        """Run interactive setup."""

        if not self.client:
            print(error_text("Not connected. Run /connect first."), file=sys.stderr)
            self.do_connect("")
            if self.client:
                print(status_text("connected", "Run /init again to start setup."))
            return
        try:
            options = parse_options(arg)
        except ValueError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        if missing_options(options, "project", "repo", "pipeline", "pool"):
            print(error_text("usage: /init --project PROJECT --repo REPO --pipeline PIPELINE --pool POOL [--branch main] [--yml-path azure-pipelines.yml] [--visibility private|public] [--process Basic] [--apply false]"), file=sys.stderr)
            return
        project = options["project"]
        repo = options["repo"]
        pipeline = options["pipeline"]
        pool = options["pool"]
        branch = options.get("branch") or "main"
        yml_path = options.get("yml-path") or "azure-pipelines.yml"
        visibility = options.get("visibility") or "private"
        process = options.get("process") or "Basic"
        if project.startswith("/") or not project.strip():
            print(error_text(f"invalid project name: {project}"), file=sys.stderr)
            return
        apply = options.get("apply", "true").lower() not in {"false", "0", "no"}
        print(section_title("Plan"))
        print_table(
            ("Resource", "Value"),
            [("project", project), ("pool", pool), ("repo", repo), ("pipeline", pipeline), ("yaml", yml_path)],
        )
        if not apply:
            print(warn_text("dry run; no resources were changed"))
            return
        try:
            if hasattr(self.client, "clear_pool_cache"):
                self.client.clear_pool_cache()
            resolved_pool = self.client.resolve_pool_name(pool)
            if resolved_pool != pool:
                print(status_text("resolved", f"'{pool}' to agent pool '{resolved_pool}'"))
                pool = resolved_pool
            self.client.ensure_project(project, visibility, process)
            self.client.ensure_pool(pool)
            repo_data = self.client.ensure_repo(project, repo)
            from .pipeline_yaml import render_pipeline_yaml

            self.client.push_file(project, repo_data["id"], branch, yml_path, render_pipeline_yaml())
            print(status_text("pushed", f"{yml_path} to {repo}@{branch}"))
            created_pipeline = self.client.ensure_pipeline(project, pipeline, repo, branch, yml_path, pool)
        except AzureDevOpsError as exc:
            print(error_text(str(exc)), file=sys.stderr)
            return
        self.project_filter = project
        self.pipeline_filter = str(created_pipeline.id)
        self.pool_filter = pool
        self._reset_agent_cache()
        print(status_text("init", "complete"))
        print(muted_text("Register/start an agent in the pool if none is online, then run /agent list."))

    def do_clear(self, _arg: str) -> None:
        """Clear the terminal."""

        os.system("cls" if os.name == "nt" else "clear")

    def do_exit(self, _arg: str) -> bool:
        """Exit the shell."""

        self.devtunnels.shutdown()
        self.relay_bridge.shutdown()
        return True

    def do_quit(self, arg: str) -> bool:
        """Do not exit; /exit is the only shell exit command."""

        print(error_text("Use /exit to quit."), file=sys.stderr)
        return False

    def do_EOF(self, _arg: str) -> bool:
        print()
        print(error_text("Use /exit to quit."), file=sys.stderr)
        return False

    def emptyline(self) -> None:
        return None

    def _find_agent(self, target: str) -> AgentRef | None:
        if target.isdigit():
            index = int(target)
            if 1 <= index <= len(self.agents):
                return self.agents[index - 1]
            return None
        matches = [agent for agent in self.agents if agent.name == target]
        return matches[0] if len(matches) == 1 else None

    def _resolve_pipeline_target(self, project: str, target: str) -> PipelineRef | None:
        matches = [
            pipeline
            for pipeline in self.client.list_pipelines(project)
            if pipeline.name == target or str(pipeline.id) == target
        ]
        if not matches:
            print(error_text(f"pipeline not found: {project}/{target}"), file=sys.stderr)
            return None
        if len(matches) > 1:
            names = ", ".join(f"{pipeline.name}({pipeline.id})" for pipeline in matches)
            print(error_text(f"multiple pipelines match {target}; use the numeric id. matches: {names}"), file=sys.stderr)
            return None
        return matches[0]

    def _resolve_delete_agent_target(self, target: str | None, options: dict[str, str]) -> AgentRef | None:
        pool = options.get("pool")
        if "agent-id" in options:
            if not pool:
                print(error_text("usage: /agent remove --agent-id ID --pool POOL [--yes]"), file=sys.stderr)
                return None
            try:
                agent_id = int(options["agent-id"])
            except ValueError:
                print(error_text(f"invalid agent id: {options['agent-id']}"), file=sys.stderr)
                return None
            pool_data = self.client.get_pool(pool)
            if not pool_data:
                print(error_text(f"pool not found: {pool}"), file=sys.stderr)
                return None
            pool_id = int(pool_data["id"])
            pool_name = str(pool_data.get("name") or pool)
            for agent in self.client.discover_agents(pool_name):
                if agent.id == agent_id:
                    return agent
            return AgentRef(agent_id, str(agent_id), pool_id, pool_name, "unknown", False)
        if not target:
            return None
        if target.isdigit():
            index = int(target)
            if 1 <= index <= len(self.agents):
                return self.agents[index - 1]
        with self._agent_lock:
            cached_agents = list(self._agent_cache)
        if not cached_agents:
            self._sync_agents_once()
            with self._agent_lock:
                cached_agents = list(self._agent_cache)
        pool_lower = pool.lower() if pool else None
        matches = [
            agent
            for agent in cached_agents
            if agent.name.lower() == target.lower()
            and (not pool_lower or pool_lower in {agent.pool_name.lower(), str(agent.pool_id)})
        ]
        if not matches:
            print(error_text(f"agent not found: {target}"), file=sys.stderr)
            return None
        if len(matches) > 1:
            pools = ", ".join(f"{agent.pool_name}({agent.pool_id})" for agent in matches)
            print(error_text(f"multiple agents named {target}; use --pool. matches: {pools}"), file=sys.stderr)
            return None
        return matches[0]

    def _choose_pipeline(self, candidates: list[PipelineRef]) -> PipelineRef | None:
        print("Multiple pipelines match this agent pool:")
        for index, pipeline in enumerate(candidates, start=1):
            print(f"{index}. {pipeline.project}/{pipeline.name} ({pipeline.id})")
        raw = input("pipeline> ").strip()
        if not raw.isdigit():
            print("selection cancelled", file=sys.stderr)
            return None
        index = int(raw)
        if 1 <= index <= len(candidates):
            return candidates[index - 1]
        print("selection out of range", file=sys.stderr)
        return None

    def _require_client(self) -> bool:
        if self.client:
            return True
        print("Not connected. Run /connect first.", file=sys.stderr)
        return False

    def _start_agent_sync(self) -> None:
        if not self.client or self._agent_sync_thread and self._agent_sync_thread.is_alive():
            return
        self._agent_sync_stop.clear()
        self._agent_sync_thread = threading.Thread(target=self._agent_sync_loop, name="evilazp-agent-sync", daemon=True)
        self._agent_sync_thread.start()

    def _stop_agent_sync(self) -> None:
        self._agent_sync_stop.set()
        if self._agent_sync_thread and self._agent_sync_thread.is_alive():
            self._agent_sync_thread.join(timeout=1)

    def _agent_sync_loop(self) -> None:
        while not self._agent_sync_stop.is_set():
            try:
                self._sync_agents_once()
            except AzureDevOpsError as exc:
                with self._agent_lock:
                    self.agent_sync_error = str(exc)
            except Exception as exc:  # pragma: no cover - keeps background sync from killing the shell.
                with self._agent_lock:
                    self.agent_sync_error = str(exc)
            if self._agent_sync_stop.wait(self.agent_sync_interval):
                break

    def _sync_agents_once(self) -> None:
        if not self.client:
            return
        self.client.clear_agent_cache()
        agents = self.client.discover_agents(None)
        new_agents: list[AgentRef] = []
        with self._agent_lock:
            had_previous_sync = self.agent_sync_started
            self._agent_cache = agents
            self.agent_sync_error = None
            self.agent_sync_started = True
            self.agents = filter_online_agents(agents, self.pool_filter)
            online_agents = filter_online_agents(agents, None)
            current_online_keys = {agent_identity_key(agent) for agent in online_agents}
            if had_previous_sync:
                new_agents = [
                    agent
                    for agent in online_agents
                    if agent_identity_key(agent) not in self._known_online_agent_keys
                ]
            self._known_online_agent_keys = current_online_keys
            if self.selected_agent:
                fresh_agent = next(
                    (
                        agent
                        for agent in online_agents
                        if agent.name == self.selected_agent.name and agent.pool_id == self.selected_agent.pool_id
                    ),
                    None,
                )
                if fresh_agent and self.selected_pipeline:
                    self.selected_agent = self.client.bind_agent_to_pipeline(fresh_agent, self.selected_pipeline)
                elif fresh_agent:
                    self.selected_agent = fresh_agent
                else:
                    self.selected_agent = None
                    self.selected_pipeline = None
                    self.runner_variables_key = None
                    self.prompt = format_prompt(None)
        if new_agents:
            self._notify_new_agents(new_agents)

    def _notify_new_agents(self, agents: list[AgentRef]) -> None:
        observed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print()
        for agent in agents:
            host = agent_host_hint(agent)
            print(status_text("agent+", f"new agent registered: {agent.name} connected at {observed_at} from {host}"))
        self._redisplay_prompt()

    def _reset_agent_cache(self) -> None:
        with self._agent_lock:
            self.agents = []
            self._agent_cache = []
            self._known_online_agent_keys = set()
            self._agent_metadata_refresh_cache = {}
            self.agent_cache_key = None
            self.agent_sync_error = None
            self.agent_sync_started = False
            self.runner_variables_key = None
        if self.client and hasattr(self.client, "clear_agent_cache"):
            self.client.clear_agent_cache()

    def _ensure_selected_runner_variables(self) -> None:
        if not self.client or not self.selected_agent or not self.selected_pipeline:
            return
        key = (
            self.selected_pipeline.project,
            self.selected_pipeline.id,
            self.selected_agent.pool_id,
            self.selected_agent.name,
        )
        if self.runner_variables_key == key:
            return
        self.client.ensure_queue_variables(
            self.selected_pipeline.project,
            self.selected_pipeline.id,
            {
                "targetPool": self.selected_agent.pool_name,
                "targetAgent": self.selected_agent.name,
                "commandB64": "V3JpdGUtSG9zdCAnYXpwLXNoZWxsIHJlYWR5Jw==",
                "runId": "default",
            },
        )
        self.runner_variables_key = key

    def _refresh_selected_agent_metadata(self) -> None:
        if not self.client or not self.selected_agent:
            return
        cache_key = (self.selected_agent.pool_id, self.selected_agent.name)
        now = time.monotonic()
        last_refresh = self._agent_metadata_refresh_cache.get(cache_key)
        if last_refresh is not None and now - last_refresh < AGENT_METADATA_REFRESH_TTL_SECONDS:
            return
        with self._agent_lock:
            cached_agents = list(self._agent_cache)
        fresh_agent = next(
            (
                agent
                for agent in cached_agents
                if agent.name == self.selected_agent.name and agent.pool_id == self.selected_agent.pool_id
            ),
            None,
        )
        if fresh_agent is None:
            if not hasattr(self.client, "discover_agents"):
                return
            try:
                fresh_agent = next(
                    (
                        agent
                        for agent in self.client.discover_agents(self.selected_agent.pool_name)
                        if agent.name == self.selected_agent.name and agent.pool_id == self.selected_agent.pool_id
                    ),
                    None,
                )
            except AzureDevOpsError:
                return
        if not fresh_agent:
            return
        if self.selected_pipeline:
            self.selected_agent = self.client.bind_agent_to_pipeline(fresh_agent, self.selected_pipeline)
        else:
            self.selected_agent = fresh_agent
        self._agent_metadata_refresh_cache[cache_key] = now

    def _print_quick_agent_info(self, line: str) -> bool:
        if line.strip().lower() != "whoami":
            return False
        if not self.selected_agent:
            print(error_text("No agent selected. Run /agent list then /use <index|hostname>."), file=sys.stderr)
            return True
        agent = self.selected_agent
        rows = [
            ("agent", agent.name),
            ("agent_id", agent.id),
            ("pool", agent.pool_name),
            ("pool_id", agent.pool_id),
            ("status", agent.status),
            ("enabled", str(agent.enabled).lower()),
            ("os", agent.os_description or "-"),
            ("version", agent.version or "-"),
            ("account", registered_account_hint(agent) or "-"),
        ]
        if self.selected_pipeline:
            rows.extend(
                [
                    ("project", self.selected_pipeline.project),
                    ("pipeline", self.selected_pipeline.name),
                    ("pipeline_id", self.selected_pipeline.id),
                ]
            )
        print_table(("Field", "Value"), rows)
        return True

    def _devtunnel_credentials(self) -> DevTunnelCredentials | None:
        session = self.current_session
        if not session or not session.sp_tenant_id or not session.sp_client_id or not session.sp_client_secret:
            return None
        return DevTunnelCredentials(session.sp_tenant_id, session.sp_client_id, session.sp_client_secret)

    def _save_devtunnel_sp(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        if not self.current_session:
            if not self.client:
                print(error_text("Connect or load a saved session before saving DevTunnel SP credentials."), file=sys.stderr)
                return
            self.current_session = SessionRecord(
                name=self.client.org_name,
                organization=self.client.organization,
                pat=getattr(self.client, "pat", ""),
                project=self.project_filter,
                pipeline=self.pipeline_filter,
                pool=self.pool_filter,
            )
        self.current_session = SessionRecord(
            name=self.current_session.name,
            organization=self.current_session.organization,
            pat=self.current_session.pat,
            project=self.current_session.project,
            pipeline=self.current_session.pipeline,
            pool=self.current_session.pool,
            sp_tenant_id=tenant_id,
            sp_client_id=client_id,
            sp_client_secret=client_secret,
            created_at=self.current_session.created_at,
            last_used_at=self.current_session.last_used_at,
        )
        save_session(self.current_session)
        print(status_text("devtunnels", f"saved SP tenant={tenant_id} client={client_id} secret={masked_secret(client_secret)}"))


def parse_options(arg: str) -> dict[str, str]:
    tokens = shlex.split(arg)
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            raise ValueError(f"unexpected argument: {token}")
        key = token[2:]
        if index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
            options[key] = tokens[index + 1]
            index += 2
        else:
            options[key] = "true"
            index += 1
    return options


def parse_target_options(arg: str) -> tuple[str | None, dict[str, str]]:
    tokens = shlex.split(arg)
    target: str | None = None
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            key = token[2:]
            if index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                options[key] = tokens[index + 1]
                index += 2
            else:
                options[key] = "true"
                index += 1
            continue
        if target is not None:
            raise ValueError(f"unexpected argument: {token}")
        target = token
        index += 1
    return target, options


def missing_options(options: dict[str, str], *names: str) -> list[str]:
    return [name for name in names if not options.get(name)]


def is_project_not_found_error(exc: Exception, project: str) -> bool:
    message = str(exc)
    return (
        f"Project not found: {project}" in message
        or "ProjectDoesNotExistWithNameException" in message
        or "TF200016" in message
    )


def split_option_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def input_default(label: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def confirm(label: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{label} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def unwrap_outer_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return value


def strip_prompt_prefix(value: str) -> str:
    return re.sub(r"^\s*evilazp\([^)]*\)>\s*", "", value, count=1)


def registered_account_hint(agent: AgentRef) -> str | None:
    """Return the agent registration-time account hint from system capabilities."""

    capabilities = {key.lower(): value for key, value in agent.capabilities.items()}
    domain = first_capability(capabilities, "userdomain", "userdnsdomain")
    username = first_capability(capabilities, "username", "user", "logname")
    if domain and username:
        return f"{domain}\\{username}"
    if username:
        return username
    return None


def agent_identity_key(agent: AgentRef) -> tuple[int, int, str]:
    return (agent.pool_id, agent.id, agent.name.lower())


def agent_host_hint(agent: AgentRef) -> str:
    capabilities = {key.lower(): value for key, value in agent.capabilities.items()}
    host = first_capability(
        capabilities,
        "agent.machinename",
        "machinename",
        "computername",
        "hostname",
        "agent.hostname",
    )
    os_text = agent.os_description or first_capability(
        capabilities,
        "agent.osdescription",
        "osdescription",
        "agent.os",
    )
    if host and os_text:
        return f"{host} ({os_text})"
    if host:
        return host
    if os_text:
        return os_text
    return "unknown host"


def first_capability(capabilities: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = capabilities.get(name.lower())
        if value:
            return value
    return None


def agent_transfer_shell(agent: AgentRef | None) -> str:
    if not agent:
        return "powershell"
    values = [agent.name, agent.pool_name, agent.os_description or ""]
    capabilities = {key.lower(): value for key, value in agent.capabilities.items()}
    values.extend(str(value) for value in capabilities.values())
    text = " ".join(values).lower()
    if any(marker in text for marker in ("windows", "windows_nt", "win32", "microsoft windows")):
        return "powershell"
    if any(marker in text for marker in ("linux", "darwin", "mac os", "macos", "ubuntu", "debian", "centos", "rhel")):
        return "bash"
    return "powershell"


def filter_online_agents(agents: list[AgentRef], pool_filter: str | None) -> list[AgentRef]:
    pool_filter_lower = pool_filter.lower() if pool_filter else None
    filtered: list[AgentRef] = []
    for agent in agents:
        if not agent.online:
            continue
        if pool_filter_lower and pool_filter_lower not in {agent.pool_name.lower(), str(agent.pool_id)}:
            continue
        filtered.append(agent)
    return sorted(filtered, key=lambda item: (item.name.lower(), item.pool_name.lower()))


def print_project_help() -> None:
    print(section_title("/project"))
    print_table(
        ("Command", "Description"),
        {
            "/project list": "list Azure DevOps projects",
            "/project create --project PROJECT [--visibility private|public] [--process Basic]": "create or reuse a project",
            "/project remove <name> [--yes]": "remove an Azure DevOps project",
            "/project remove --project <name> [--yes]": "remove an Azure DevOps project",
            "/project --help": "show this help",
        }.items(),
    )


def print_pipeline_help() -> None:
    print(section_title("/pipeline"))
    print_table(
        ("Command", "Description"),
        {
            "/pipeline list --project PROJECT": "list pipelines in a project",
            "/pipeline create --project PROJECT --repo REPO --pipeline PIPELINE --pool POOL [--branch main] [--yml-path azure-pipelines.yml]": "create or reuse a YAML pipeline",
            "/pipeline remove <pipeline> --project PROJECT [--yes]": "remove a pipeline definition",
            "/pipeline remove --pipeline PIPELINE --project PROJECT [--yes]": "remove a pipeline definition",
            "/pipeline --help": "show this help",
        }.items(),
    )


def print_session_help() -> None:
    print(section_title("/session"))
    print_table(
        ("Command", "Description"),
        {
            "/session list": "list saved connection sessions",
            "/session use <index|name>": "connect with a saved session",
            "/session remove <index|name>": "remove a saved session",
            "/session --help": "show this help",
        }.items(),
    )


def print_agent_help() -> None:
    print(section_title("/agent"))
    print_table(
        ("Command", "Description"),
        {
            "/agent list [--refresh|--all]": "list online agents by hostname and pool",
            "/agent create --pool POOL --agent AGENT --runtime RUNTIME [--polling N] [--tunnel] [--relay] [--sign]": "bake and build an agent using the current session",
            "/agent create --yaml --runtime RUNTIME [--polling N] [--tunnel] [--relay] [--sign]": "bake and build an agent from YAML",
            "/agent remove <index|hostname> [--pool POOL] [--yes]": "remove an agent registration",
            "/agent remove --agent-id ID --pool POOL [--yes]": "remove an agent registration by id and pool",
            "/agent --help": "show this help",
        }.items(),
    )
    print()
    print(section_title("create options"))
    print_table(
        ("Option", "Meaning"),
        {
            "--yaml": f"load baked agent values from {DEFAULT_YAML_PATH}; use --yaml PATH to override",
            "--runtime <runtime>": "target runtime: linux-x64, linux-arm64, win-x64, osx-x64",
            "--polling <seconds>": "fixed no-job polling delay; omit for random 5-15s",
            "--tunnel": "include the Dev Tunnels plugin; YAML must include SP and tunnel values",
            "--relay": "include the Azure Relay Bridge plugin; YAML must include Relay values",
            "--tunnel-id <id>": "direct mode: bake a DevTunnel id",
            "--tunnel-ports <ports>": "direct mode: bake comma-separated DevTunnel ports",
            "--relay-connection-string <value>": "direct mode: bake Azure Relay connection string",
            "--relay-local-forward <expr>": "direct mode: bake comma-separated local Relay forwards",
            "--relay-remote-forward <expr>": "direct mode: bake comma-separated remote TCP Relay forwards",
            "--relay-remote-http-forward <expr>": "direct mode: bake comma-separated remote HTTP Relay forwards",
            "--sign": "Authenticode-sign Windows output; creates a temporary self-signed cert unless --pfx is supplied",
            "--pfx <path>": "use an existing PFX/PKCS#12 certificate instead of a temporary self-signed cert",
            "--pfx-pass <password>": "PFX password; alternatively set $EVILAZP_PFX_PASS",
        }.items(),
    )


def print_agent_create_usage(file=sys.stderr) -> None:
    print(error_text("usage: /agent create --pool POOL --agent AGENT --runtime RUNTIME [--polling N] [--tunnel] [--relay] [--sign]"), file=file)
    print(error_text("usage: /agent create --yaml --runtime RUNTIME [--polling N] [--tunnel] [--relay] [--sign]"), file=file)


def print_agent_pool_help() -> None:
    print(section_title("/agent-pool"))
    print_table(
        ("Command", "Description"),
        {
            "/agent-pool list": "list organization agent pools",
            "/agent-pool create --pool POOL": "create or reuse an organization agent pool",
            "/agent-pool open-access --project PROJECT --pool POOL": "allow all pipelines in a project to use a pool",
            "/agent-pool remove --pool POOL [--yes]": "remove an organization agent pool",
            "/agent-pool --help": "show this help",
        }.items(),
    )


def _relay_bridge_mode_label(mode: str) -> str:
    return {"local": "-L", "remote": "-T", "remote-http": "-H"}.get(mode, mode)


def print_devtunnels_help() -> None:
    print(section_title("/devtunnels"))
    print_table(
        ("Command", "Description"),
        {
            "/devtunnels start --tunnel-id <id> --port <port> [--local <port>]": "connect local TCP forwarding using the current session's SP credentials",
            "/devtunnels --tunnel-id <id> --port <port> [--local <port>]": "same as /devtunnels start, kept for compatibility",
            "/devtunnels list": "list active DevTunnel forwards in this shell",
            "/devtunnels stop <id|localPort|all>": "stop active DevTunnel forwards",
            "/devtunnels sp --tenant-id <id> --sp-client-id <id> --sp-secret <secret>": "optional: update SP credentials on the current session",
            "/devtunnels --help": "show this help",
        }.items(),
    )


def print_azure_files_help() -> None:
    print(section_title("/azure-files"))
    print_table(
        ("Command", "Description"),
        {
            "/azure-files storage-account list": "list Azure Storage accounts",
            "/azure-files storage-account create <name> -g <rg> -l <location> [--sku Standard_LRS]": "create a StorageV2 account",
            "/azure-files storage-account remove <name> -g <rg>": "remove a storage account immediately",
            "/azure-files storage-account key --account <name> [--resource-group <rg>]": "save the first account key to .env",
            "/azure-files share list": "list Azure Files shares",
            "/azure-files share create <share>": "create a share",
            "/azure-files share remove <share>": "remove a share immediately",
            "/azure-files directory create --share <share> <directory>": "create a directory path",
            "/azure-files directory remove --share <share> <directory>": "remove a directory immediately",
            "/azure-files list --share /<share>[/directory]": "list one directory level",
            "/azure-files sas [--permissions rwld] [--hours 24]": "save an account SAS from azure_files_account_key to .env; defaults: permissions=rwld, hours=24",
            "/azure-files --help": "show this help",
        }.items(),
    )


def print_relay_bridge_help() -> None:
    print(section_title("/relay-bridge"))
    print_table(
        ("Command", "Description"),
        {
            '/relay-bridge start -x "<connection-string>" [-L expr] [-T expr] [-H expr]': "start Azure Relay Bridge forwards",
            "/relay-bridge list": "list active Relay Bridge helpers in this shell",
            "/relay-bridge stop <id|localPort|relayName|all>": "stop active Relay Bridge helpers",
            "/relay-bridge ns list -g <rg>": "list Azure Relay namespaces",
            "/relay-bridge ns create -g <rg> -n <namespace> [-l <location>]": "create an Azure Relay namespace",
            "/relay-bridge ns remove -g <rg> -n <namespace>": "remove an Azure Relay namespace",
            "/relay-bridge ns keys -g <rg> -n <namespace> [--rule <rule>]": "print namespace primary connection string for -x",
            "/relay-bridge hc list -g <rg> -n <namespace>": "list Azure Relay Hybrid Connections",
            "/relay-bridge hc create -g <rg> -n <namespace> <hc>": "create an Azure Relay Hybrid Connection",
            "/relay-bridge hc remove -g <rg> -n <namespace> <hc>": "remove an Azure Relay Hybrid Connection",
            "/relay-bridge --help": "show this help",
        }.items(),
    )
    print()
    print(section_title("start options"))
    print_table(
        ("Option", "Meaning"),
        {
            "-x <connection-string>": "Azure Relay namespace or Hybrid Connection SAS connection string",
            "-L <local-port>:<relay>": "local listener; connect 127.0.0.1:<local-port> on this machine to the relay",
            "-T <relay>:<target-host>:<target-port>": "remote TCP target; expose a target reachable from this machine through the relay",
            "-H <relay>:http/<host>:<port>": "remote HTTP target; expose an HTTP service reachable from this machine through the relay",
        }.items(),
    )
    print()
    print(section_title("examples"))
    print_table(
        ("Goal", "Command"),
        {
            "client side: open local SSH port": '/relay-bridge start -x "<connection-string>" -L 2222:ssh',
            "agent side: publish target SSH": '/relay-bridge start -x "<connection-string>" -T ssh:127.0.0.1:22',
            "agent side: publish target web": '/relay-bridge start -x "<connection-string>" -H web:http/127.0.0.1:8080',
        }.items(),
    )


def print_resource_group_help() -> None:
    print(section_title("/resource-group"))
    print_table(
        ("Command", "Description"),
        {
            "/resource-group list": "list Azure resource groups",
            "/resource-group create -g <resource-group> -l <location>": "create an Azure resource group",
            "/resource-group remove -g <resource-group> [--yes]": "remove an Azure resource group and its contained resources",
            "/resource-group --help": "show this help",
        }.items(),
    )
    print()
    print(section_title("examples"))
    print_table(
        ("Goal", "Command"),
        {
            "create Relay resource group": "/resource-group create -g rg-relay-bridge -l koreacentral",
            "remove without prompt": "/resource-group remove -g rg-relay-bridge --yes",
        }.items(),
    )


ANSI_STYLES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "blue": "\033[34m",
}

READLINE_IGNORE_START = "\001"
READLINE_IGNORE_END = "\002"


def color_enabled(stream=sys.stdout) -> bool:
    return bool(interactive_terminal(stream) and not os.environ.get("NO_COLOR"))


def interactive_terminal(stream=sys.stdout) -> bool:
    return bool(stream.isatty() and os.environ.get("TERM") != "dumb")


def style_text(text: str, *styles: str, stream=sys.stdout) -> str:
    if not color_enabled(stream):
        return text
    prefix = "".join(ANSI_STYLES[item] for item in styles if item in ANSI_STYLES)
    return f"{prefix}{text}{ANSI_STYLES['reset']}" if prefix else text


def prompt_style_text(text: str, *styles: str, stream=sys.stdout) -> str:
    if not prompt_color_enabled(stream):
        return text
    prefix = "".join(ANSI_STYLES[item] for item in styles if item in ANSI_STYLES)
    if not prefix:
        return text
    return (
        f"{READLINE_IGNORE_START}{prefix}{READLINE_IGNORE_END}"
        f"{text}"
        f"{READLINE_IGNORE_START}{ANSI_STYLES['reset']}{READLINE_IGNORE_END}"
    )


def prompt_color_enabled(stream=sys.stdout) -> bool:
    """Return true only when readline can safely redisplay colored prompts."""

    if not color_enabled(stream):
        return False
    if sys.platform == "darwin" and os.environ.get("EVILAZP_COLOR_PROMPT") != "1":
        return False
    try:
        import readline
    except ImportError:
        return False
    # Python on macOS commonly links readline to libedit. libedit does not
    # reliably honor GNU readline's \001/\002 non-printing markers, which makes
    # up-arrow history redraw include stale prompt text in the editable line.
    return "libedit" not in (readline.__doc__ or "").lower()


def configure_readline() -> None:
    try:
        import readline
    except ImportError:
        return

    try:
        readline.set_history_length(1000)
        readline.parse_and_bind("set horizontal-scroll-mode off")
    except Exception:
        return


def format_banner() -> str:
    lines = [
        "  ______      _ _   ___ __________",
        " |  ____|    (_) | / _ \\___  / __ \\",
        " | |____   ___| |/ /_\\ \\ / /| |_/ /",
        " |  __\\ \\ / / | ||  _  |/ / |  __/",
        " | |___\\ V /| | || | | / /__| |",
        " |______\\_/ |_|_\\_| |_/_____|_|",
    ]
    header = style_text("EvilAZP", "bold", "cyan")
    version = style_text(f"v{__version__}", "dim")
    hint = style_text("type /help for commands | /agent list is backed by a 1s live cache", "dim")
    return "\n".join([style_text(line, "cyan") for line in lines] + [f"  {header} {version}", f"  {hint}", ""])


def format_prompt(agent_name: str | None) -> str:
    label = agent_name or "no-agent"
    if agent_name:
        return f"{prompt_style_text('evilazp', 'cyan')}({prompt_style_text(label, 'green')})> "
    return f"{prompt_style_text('evilazp', 'cyan')}({prompt_style_text(label, 'yellow')})> "


def section_title(value: str) -> str:
    return style_text(f"== {value} ==", "bold", "cyan")


def status_text(label: str, message: str, level: str = "ok") -> str:
    color = {"ok": "green", "warn": "yellow", "error": "red", "run": "cyan"}.get(level, "cyan")
    return f"{style_text('[' + label + ']', color, 'bold')} {message}"


def error_text(message: str) -> str:
    return status_text("error", message, "error")


def warn_text(message: str) -> str:
    return status_text("warn", message, "warn")


def muted_text(message: str) -> str:
    return style_text(message, "dim")


def print_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    normalized = [tuple(str(cell) for cell in row) for row in rows]
    widths = [len(header) for header in headers]
    for row in normalized:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    header_line = "  ".join(header.ljust(width) for header, width in zip(headers, widths))
    divider = "  ".join("-" * width for width in widths)
    print(style_text(header_line, "bold", "cyan"))
    print(style_text(divider, "dim"))
    for row in normalized:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))


def clear_screen_on_start() -> None:
    if interactive_terminal(sys.stdout):
        os.system("cls" if os.name == "nt" else "clear")
