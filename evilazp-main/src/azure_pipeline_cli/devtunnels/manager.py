"""Process lifecycle management for DevTunnel local forwards.

The Python shell keeps command UX and registry state here. The actual DevTunnels
protocol connection is delegated to the repo-local SDK helper process.
"""

from __future__ import annotations

import os
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .commands import DevTunnelSpec


@dataclass(frozen=True)
class DevTunnelCredentials:
    """Service-principal credentials used by the helper process."""

    tenant_id: str
    client_id: str
    client_secret: str


@dataclass
class DevTunnelConnection:
    """A running helper process associated with a local forward."""

    tunnel_id: str
    remote_port: int
    local_port: int
    process: subprocess.Popen[str]
    status: str = "running"


class DevTunnelError(RuntimeError):
    """A user-facing DevTunnel command failure."""


class DevTunnelManager:
    """Start, list, and stop DevTunnel helper processes."""

    def __init__(self, helper_command: list[str] | None = None, ready_timeout: float = 75) -> None:
        self.helper_command = helper_command or default_helper_command()
        self.ready_timeout = ready_timeout
        self._connections: dict[int, DevTunnelConnection] = {}

    def start(self, spec: DevTunnelSpec, credentials: DevTunnelCredentials) -> DevTunnelConnection:
        self._reap_finished()
        if spec.local_port in self._connections:
            raise DevTunnelError(f"local port already in use by this shell: {spec.local_port}")

        # The helper owns all SDK networking. Python only supplies validated
        # parameters and waits for READY so the shell remains responsive.
        command = [
            *self.helper_command,
            "--tunnel-id",
            spec.tunnel_id,
            "--remote-port",
            str(spec.remote_port),
            "--local-port",
            str(spec.local_port),
            "--connect-timeout",
            str(max(5, int(self.ready_timeout) - 5)),
        ]
        env = {
            **os.environ,
            "EVILAZP_DEVTUNNEL_TENANT_ID": credentials.tenant_id,
            "EVILAZP_DEVTUNNEL_CLIENT_ID": credentials.client_id,
            "EVILAZP_DEVTUNNEL_CLIENT_SECRET": credentials.client_secret,
        }
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
            )
        except OSError as exc:
            raise DevTunnelError(f"failed to start devtunnel helper: {exc}") from exc

        output: list[str] = []
        lines: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(target=_read_process_lines, args=(process, lines), daemon=True)
        reader.start()
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                line = ""
            if line:
                clean = line.strip()
                output.append(clean)
                if clean.startswith("READY "):
                    # Register only after the local listener exists. This avoids
                    # listing forwards that failed during SDK authentication.
                    connection = DevTunnelConnection(spec.tunnel_id, spec.remote_port, spec.local_port, process)
                    self._connections[spec.local_port] = connection
                    return connection
                if clean.startswith("ERROR "):
                    self._terminate_process(process)
                    raise DevTunnelError(clean.removeprefix("ERROR ").strip())
            if process.poll() is not None:
                break

        details = "; ".join(item for item in output if item)
        if details:
            details = f"timed out waiting for helper READY after {self.ready_timeout:.0f}s; helper output: {details}"
        else:
            details = f"timed out waiting for helper READY after {self.ready_timeout:.0f}s with no helper output"
        self._terminate_process(process)
        raise DevTunnelError(details)

    def list(self) -> list[DevTunnelConnection]:
        self._reap_finished()
        return sorted(self._connections.values(), key=lambda item: (item.tunnel_id, item.remote_port, item.local_port))

    def stop(self, target: str) -> list[DevTunnelConnection]:
        self._reap_finished()
        if target == "all":
            matches = list(self._connections.values())
        elif target.isdigit():
            connection = self._connections.get(int(target))
            matches = [connection] if connection else []
        else:
            matches = [item for item in self._connections.values() if item.tunnel_id == target]
        for connection in matches:
            self._terminate_process(connection.process)
            self._connections.pop(connection.local_port, None)
            connection.status = "stopped"
        return matches

    def shutdown(self) -> None:
        self.stop("all")

    def _reap_finished(self) -> None:
        for local_port, connection in list(self._connections.items()):
            if connection.process.poll() is not None:
                connection.status = "exited"
                self._connections.pop(local_port, None)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if process.stdin:
                # STOP gives the helper a chance to dispose SDK clients before
                # the process is terminated.
                process.stdin.write("STOP\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.terminate()
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def default_helper_command() -> list[str]:
    configured = os.environ.get("EVILAZP_DEVTUNNEL_HELPER")
    if configured:
        return shlex.split(configured)
    helper_project = Path(__file__).resolve().parents[3] / "helpers" / "devtunnel-client" / "devtunnel-client.csproj"
    helper_config = helper_project.with_name("NuGet.Config")
    return [
        "dotnet",
        "run",
        "--project",
        str(helper_project),
        f"--property:RestoreConfigFile={helper_config}",
        "--",
    ]


def _read_process_lines(process: subprocess.Popen[str], lines: queue.Queue[str]) -> None:
    if not process.stdout:
        return
    for line in process.stdout:
        lines.put(line)
