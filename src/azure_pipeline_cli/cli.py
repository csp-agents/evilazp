"""Command-line entry point for evilazp."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .agent_builder import (
    AgentBuildConfig,
    AgentBuilderError,
    DEFAULT_SIGN_NAME,
    DEFAULT_SIGN_SUBJECT,
    DEFAULT_SIGN_URL,
    DEFAULT_TIMESTAMP_URL,
    DEFAULT_YAML_PATH,
    SUPPORTED_RUNTIMES,
    build_from_config,
    build_from_yaml,
)
from .azure_devops import QUEUE_VARIABLES, AzureDevOpsClient, AzureDevOpsError, encode_command
from .pipeline_yaml import render_pipeline_yaml
from .shell import PipelineShell, unwrap_outer_quotes
from .sessions import SessionRecord, find_session, load_sessions, mark_session_used, print_sessions_table


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evilazp")
    parser.add_argument("--version", action="version", version=f"evilazp {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    shell_parser = subparsers.add_parser("shell", help="start interactive Azure Pipeline shell")
    shell_parser.add_argument("--org", help="Azure DevOps org name or URL")
    shell_parser.add_argument("--pat", help="Azure DevOps PAT; defaults to AZURE_DEVOPS_EXT_PAT")
    shell_parser.add_argument("--project", help="limit pipeline discovery to one project")
    shell_parser.add_argument("--pipeline", help="limit discovery to one pipeline name or id")
    shell_parser.add_argument("--pool", help="limit agent discovery to one pool name or id")
    shell_parser.add_argument("--timeout", type=float, default=900, help="build completion timeout in seconds")
    shell_parser.add_argument("--poll-interval", type=float, default=0.5, help="build poll interval in seconds")
    shell_parser.add_argument("--agent-sync-interval", type=float, default=1, help="agent list refresh interval in seconds")

    run_parser = subparsers.add_parser("run", help="run one command through an Azure Pipeline agent")
    add_common_azure_args(run_parser)
    run_parser.add_argument("--agent", required=True, help="agent hostname or /agent list index")
    run_parser.add_argument("remote_command", nargs=argparse.REMAINDER, help="command to execute after --")
    run_parser.set_defaults(func=run_once)

    yaml_parser = subparsers.add_parser("print-yaml", help="print the reusable azure-pipelines.yml template")
    yaml_parser.set_defaults(func=print_yaml)

    create_agent_parser = subparsers.add_parser("create-agent", help="bake and build the bundled Azure Pipelines agent")
    add_agent_builder_args(create_agent_parser, require_org=False)
    create_agent_parser.set_defaults(func=run_create_agent)

    create_project_parser = subparsers.add_parser("create-project", help="create or reuse an Azure DevOps project")
    create_project_parser.add_argument("--org", required=True, help="Azure DevOps org name or URL")
    create_project_parser.add_argument("--pat", help="Azure DevOps PAT; defaults to AZURE_DEVOPS_EXT_PAT")
    create_project_parser.add_argument("--project", required=True, help="project name to create/use")
    create_project_parser.add_argument("--visibility", default="private", choices=["private", "public"])
    create_project_parser.add_argument("--process", default="Basic")
    create_project_parser.set_defaults(func=run_create_project)

    create_pipeline_parser = subparsers.add_parser("create-pipeline", help="create or reuse a YAML pipeline")
    create_pipeline_parser.add_argument("--org", required=True, help="Azure DevOps org name or URL")
    create_pipeline_parser.add_argument("--pat", help="Azure DevOps PAT; defaults to AZURE_DEVOPS_EXT_PAT")
    create_pipeline_parser.add_argument("--project", required=True, help="existing project name")
    create_pipeline_parser.add_argument("--repo", required=True, help="repository name to create/use")
    create_pipeline_parser.add_argument("--pipeline", required=True, help="pipeline name to create/use")
    create_pipeline_parser.add_argument("--pool", required=True, help="agent pool name to create/use")
    create_pipeline_parser.add_argument("--branch", default="main")
    create_pipeline_parser.add_argument("--yml-path", default="azure-pipelines.yml")
    create_pipeline_parser.set_defaults(func=run_create_pipeline)

    init_parser = subparsers.add_parser("init", help="bootstrap Azure DevOps resources")
    init_parser.add_argument("--org", required=True, help="Azure DevOps org name or URL")
    init_parser.add_argument("--pat", help="Azure DevOps PAT; defaults to AZURE_DEVOPS_EXT_PAT")
    init_parser.add_argument("--project", required=True, help="project name to create/use")
    init_parser.add_argument("--repo", required=True, help="repository name to create/use")
    init_parser.add_argument("--pipeline", required=True, help="pipeline name to create/use")
    init_parser.add_argument("--pool", required=True, help="agent pool name to create/use")
    init_parser.add_argument("--visibility", default="private", choices=["private", "public"])
    init_parser.add_argument("--process", default="Basic")
    init_parser.add_argument("--branch", default="main")
    init_parser.add_argument("--yml-path", default="azure-pipelines.yml")
    init_parser.add_argument("--apply", action="store_true", help="create resources and commit/push YAML")
    init_parser.set_defaults(func=run_init)
    return parser


def add_common_azure_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--org", required=True, help="Azure DevOps org name or URL")
    parser.add_argument("--pat", help="Azure DevOps PAT; defaults to AZURE_DEVOPS_EXT_PAT")
    parser.add_argument("--project", help="limit pipeline discovery to one project")
    parser.add_argument("--pipeline", help="limit discovery to one pipeline name or id")
    parser.add_argument("--pool", help="limit agent discovery to one pool name or id")
    parser.add_argument("--timeout", type=float, default=900, help="build completion timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="build poll interval in seconds")


def add_agent_builder_args(parser: argparse.ArgumentParser, *, require_org: bool) -> None:
    parser.add_argument(
        "--yaml",
        dest="yaml_path",
        nargs="?",
        const=str(DEFAULT_YAML_PATH),
        help=f"YAML config file for agent bake/build; omit value to use {DEFAULT_YAML_PATH}",
    )
    parser.add_argument("--org", required=require_org, help="Azure DevOps org name or URL")
    parser.add_argument("--pat", help="Azure DevOps PAT; defaults to AZURE_DEVOPS_EXT_PAT")
    parser.add_argument("--pool", help="agent pool name to bake into the agent")
    parser.add_argument("--agent", help="agent name to bake into the agent")
    parser.add_argument(
        "--runtime",
        required=True,
        choices=SUPPORTED_RUNTIMES,
        help="target runtime",
    )
    parser.add_argument("--polling", type=int, default=0, help="fixed no-job polling delay in seconds; omit for random 5-15s")
    parser.add_argument("--tunnel", action="store_true", help="include Dev Tunnels plugin")
    parser.add_argument("--relay", action="store_true", help="include Azure Relay Bridge plugin")
    parser.add_argument("--sp-tenant", dest="sp_tenant", default="", help="service principal tenant id")
    parser.add_argument("--sp-client", dest="sp_client", default="", help="service principal client id")
    parser.add_argument("--sp-secret", dest="sp_secret", default="", help="service principal client secret")
    parser.add_argument("--tunnel-id", default="", help="DevTunnel id to bake")
    parser.add_argument("--tunnel-ports", default="", help="comma-separated DevTunnel ports to bake")
    parser.add_argument("--relay-connection-string", default="", help="Azure Relay connection string")
    parser.add_argument("--relay-local-forward", action="append", default=[], help="Relay local forward expression")
    parser.add_argument("--relay-remote-forward", action="append", default=[], help="Relay remote TCP forward expression")
    parser.add_argument("--relay-remote-http-forward", action="append", default=[], help="Relay remote HTTP forward expression")
    parser.add_argument("--sign", action="store_true", help="Authenticode-sign Windows output; auto-generates a temporary self-signed cert by default")
    parser.add_argument("--pfx", default="", help="optional PFX/PKCS#12 signing certificate")
    parser.add_argument("--pfx-pass", dest="pfx_pass", default="", help="optional PFX password")
    parser.add_argument("--pfx-pass-env", default="EVILAZP_PFX_PASS", help="environment variable containing optional PFX password")
    parser.add_argument("--timestamp", default=DEFAULT_TIMESTAMP_URL, help="RFC3161 timestamp URL; use empty string to disable")
    parser.add_argument("--sign-name", default=DEFAULT_SIGN_NAME, help="osslsigncode description")
    parser.add_argument("--sign-url", default=DEFAULT_SIGN_URL, help="osslsigncode URL")
    parser.add_argument("--cert-subject", default=DEFAULT_SIGN_SUBJECT, help="self-signed certificate subject used when --pfx is omitted")


def print_yaml(_args: argparse.Namespace) -> int:
    print(render_pipeline_yaml(), end="")
    return 0


def run_create_project(args: argparse.Namespace) -> int:
    try:
        client = AzureDevOpsClient(args.org, args.pat)
        client.ensure_project(args.project, args.visibility, args.process)
    except AzureDevOpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"project ready: {args.project}")
    return 0


def run_create_pipeline(args: argparse.Namespace) -> int:
    try:
        client = AzureDevOpsClient(args.org, args.pat)
        if not client.get_project(args.project):
            raise AzureDevOpsError(f"Project not found: {args.project}")
        client.ensure_pool(args.pool)
        repo = client.ensure_repo(args.project, args.repo)
        client.push_file(args.project, repo["id"], args.branch, args.yml_path, render_pipeline_yaml())
        pipeline = client.ensure_pipeline(args.project, args.pipeline, args.repo, args.branch, args.yml_path, args.pool)
    except AzureDevOpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"pipeline ready: {pipeline.project}/{pipeline.name} ({pipeline.id})")
    return 0


def run_create_agent(args: argparse.Namespace) -> int:
    try:
        if args.yaml_path:
            result = build_from_yaml(
                args.yaml_path,
                runtime=args.runtime,
                tunnel=args.tunnel,
                relay=args.relay,
                polling=args.polling,
                sign=args.sign,
                pfx=args.pfx,
                pfx_pass=args.pfx_pass,
                pfx_pass_env=args.pfx_pass_env,
                timestamp=args.timestamp,
                sign_name=args.sign_name,
                sign_url=args.sign_url,
                cert_subject=args.cert_subject,
                quiet=True,
            )
        else:
            if not args.org or not args.pool or not args.agent:
                print("error: usage: evilazp create-agent --org ORG --pool POOL --agent AGENT --runtime RUNTIME [--polling N] [--tunnel] [--relay] [--sign]", file=sys.stderr)
                print("error: or:    evilazp create-agent --yaml --runtime RUNTIME [--polling N] [--tunnel] [--relay] [--sign]", file=sys.stderr)
                return 2
            client = AzureDevOpsClient(args.org, args.pat)
            client.ensure_pool(args.pool)
            result = build_from_config(
                AgentBuildConfig(
                    url=client.organization,
                    pat=client.pat or "",
                    pool=args.pool,
                    agent=args.agent,
                    runtime=args.runtime,
                    tunnel=args.tunnel,
                    relay=args.relay,
                    polling=args.polling,
                    sp_tenant=args.sp_tenant,
                    sp_client=args.sp_client,
                    sp_secret=args.sp_secret,
                    tunnel_id=args.tunnel_id,
                    tunnel_ports=args.tunnel_ports,
                    relay_connection_string=args.relay_connection_string,
                    relay_local_forward=tuple(args.relay_local_forward),
                    relay_remote_forward=tuple(args.relay_remote_forward),
                    relay_remote_http_forward=tuple(args.relay_remote_http_forward),
                    sign=args.sign,
                    pfx=args.pfx,
                    pfx_pass=args.pfx_pass,
                    pfx_pass_env=args.pfx_pass_env,
                    timestamp=args.timestamp,
                    sign_name=args.sign_name,
                    sign_url=args.sign_url,
                    cert_subject=args.cert_subject,
                ),
                quiet=True,
            )
    except (AgentBuilderError, AzureDevOpsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"agent binary ready: {result.binary}")
    return 0


def run_init(args: argparse.Namespace) -> int:
    steps = [
        f"create/use project: {args.project}",
        f"create/use agent pool: {args.pool}",
        f"create/use repository: {args.repo}",
        f"push {args.yml_path} to Azure Repos branch {args.branch}",
        f"create/use pipeline: {args.pipeline}",
    ]
    if not args.apply:
        print("Dry run. Add --apply to execute:")
        for step in steps:
            print(f"- {step}")
        return 0
    try:
        client = AzureDevOpsClient(args.org, args.pat)
        client.ensure_project(args.project, args.visibility, args.process)
        client.ensure_pool(args.pool)
        repo = client.ensure_repo(args.project, args.repo)
        client.push_file(args.project, repo["id"], args.branch, args.yml_path, render_pipeline_yaml())
        client.ensure_pipeline(args.project, args.pipeline, args.repo, args.branch, args.yml_path, args.pool)
    except AzureDevOpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("init complete")
    return 0


def run_shell(args: argparse.Namespace) -> int:
    try:
        session = None if args.org else choose_startup_session()
        if session:
            args.org = session.organization
            args.pat = session.pat
            args.project = args.project or session.project
            args.pipeline = args.pipeline or session.pipeline
            args.pool = args.pool or session.pool
        client = AzureDevOpsClient(args.org, args.pat) if args.org else None
        PipelineShell(
            client,
            project=args.project,
            pipeline=args.pipeline,
            pool=args.pool,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            agent_sync_interval=args.agent_sync_interval,
            initial_session=session,
        ).cmdloop()
    except AzureDevOpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def choose_startup_session() -> SessionRecord | None:
    if not sys.stdin.isatty():
        return None
    sessions = load_sessions()
    if not sessions:
        return None
    print("Saved sessions:")
    print_sessions_table(sessions)
    raw = input("session [number/name, Enter=new]: ").strip()
    if not raw:
        return None
    session = find_session(raw, sessions)
    if not session:
        print(f"session not found: {raw}", file=sys.stderr)
        return None
    mark_session_used(session.name)
    return session


def run_once(args: argparse.Namespace) -> int:
    command = normalize_remote_command(args.remote_command)
    if not command:
        print("error: provide a command after --", file=sys.stderr)
        return 2
    try:
        client = AzureDevOpsClient(args.org, args.pat)
        agents = [agent for agent in client.discover_agents(args.pool) if agent.online]
        agent = find_agent(args.agent, agents)
        if not agent:
            print(f"error: online agent not found: {args.agent}", file=sys.stderr)
            return 1
        candidates = client.find_pipeline_candidates(
            agent,
            project_filter=args.project,
            pipeline_filter=args.pipeline,
        )
        if not candidates:
            print(f"error: no pipeline found for agent pool {agent.pool_name}", file=sys.stderr)
            return 1
        if len(candidates) > 1:
            names = ", ".join(f"{item.project}/{item.name}" for item in candidates)
            print(f"error: multiple pipelines match; use --project/--pipeline. candidates: {names}", file=sys.stderr)
            return 1
        pipeline = candidates[0]
        client.ensure_runner_variables(pipeline.project, pipeline.id, agent.pool_name, agent.name)
        result = client.run_pipeline_command(
            pipeline=pipeline,
            agent=agent,
            command_b64=encode_command(command),
            run_id="cli" + __import__("uuid").uuid4().hex,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )
    except AzureDevOpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for tunnel_url in result.tunnel_urls:
        print(f"[tunnel] {tunnel_url}")
    if result.output:
        print(result.output)
    elif result.result != "succeeded":
        print(f"[build {result.build_id}] result={result.result}; no marked command output found", file=sys.stderr)
    if result.exit_code not in (None, 0):
        return result.exit_code
    return 0


def normalize_remote_command(parts: list[str]) -> str:
    if parts and parts[0] == "--":
        parts = parts[1:]
    return unwrap_outer_quotes(" ".join(parts).strip())


def find_agent(target: str, agents: list) -> object | None:
    if target.isdigit():
        index = int(target)
        if 1 <= index <= len(agents):
            return agents[index - 1]
        return None
    matches = [agent for agent in agents if agent.name == target]
    return matches[0] if len(matches) == 1 else None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["shell"])
    if args.command == "shell":
        return run_shell(args)
    func = getattr(args, "func")
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
