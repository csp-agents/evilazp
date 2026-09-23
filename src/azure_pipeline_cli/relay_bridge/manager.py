"""Process lifecycle management for Azure Relay Bridge helpers.

The shell tracks helper processes and user-safe summaries here. The actual
Relay Bridge SDK integration runs in the repo-local .NET helper process.
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

from .commands import RelayBridgeForward, RelayBridgeSpec


@dataclass
class RelayBridgeConnection:
    """A running helper process associated with one or more forwards."""

    id: int
    forwards: tuple[RelayBridgeForward, ...]
    process: subprocess.Popen[str]
    status: str = "running"


class RelayBridgeError(RuntimeError):
    """A user-facing Relay Bridge command failure."""


class RelayBridgeManager:
    """Start, list, and stop Azure Relay Bridge helper processes."""

    def __init__(self, helper_command: list[str] | None = None, ready_timeout: float = 15) -> None:
        self.helper_command = helper_command or default_helper_command()
        self.ready_timeout = ready_timeout
        self._connections: dict[int, RelayBridgeConnection] = {}
        self._next_id = 1

    def start(self, spec: RelayBridgeSpec) -> RelayBridgeConnection:
        self._reap_finished()
        self._ensure_local_ports_available(spec)
        # Secrets stay in the helper command line because the upstream bridge
        # accepts connection strings there; all helper output is sanitized below.
        command = [
            *self.helper_command,
            "--connection-string",
            spec.connection_string,
        ]
        for forward in spec.local_forwards:
            command.extend(("--local-forward", forward.expression))
        for forward in spec.remote_forwards:
            command.extend(("--remote-forward", forward.expression))
        for forward in spec.remote_http_forwards:
            command.extend(("--remote-http-forward", forward.expression))

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise RelayBridgeError(f"failed to start relay-bridge helper: {exc}") from exc

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
                clean = _sanitize(line.strip(), spec.connection_string)
                output.append(clean)
                if clean.startswith("READY "):
                    # READY means the SDK host is running and the forward set is
                    # safe to show in `/relay-bridge list`.
                    connection = RelayBridgeConnection(self._next_id, spec.forwards, process)
                    self._connections[connection.id] = connection
                    self._next_id += 1
                    return connection
                if clean.startswith("ERROR "):
                    self._terminate_process(process)
                    raise RelayBridgeError(clean.removeprefix("ERROR ").strip())
            if process.poll() is not None:
                break

        details = "; ".join(item for item in output if item) or "helper did not report READY"
        self._terminate_process(process)
        raise RelayBridgeError(details)

    def list(self) -> list[RelayBridgeConnection]:
        self._reap_finished()
        return sorted(self._connections.values(), key=lambda item: item.id)

    def stop(self, target: str) -> list[RelayBridgeConnection]:
        self._reap_finished()
        if target == "all":
            matches = list(self._connections.values())
        elif target.isdigit():
            numeric = int(target)
            matches = [item for item in self._connections.values() if item.id == numeric or numeric in _local_ports(item)]
        else:
            matches = [item for item in self._connections.values() if any(forward.relay_name == target for forward in item.forwards)]
        for connection in matches:
            self._terminate_process(connection.process)
            self._connections.pop(connection.id, None)
            connection.status = "stopped"
        return matches

    def shutdown(self) -> None:
        self.stop("all")

    def _ensure_local_ports_available(self, spec: RelayBridgeSpec) -> None:
        # Only local forwards bind sockets in this shell process tree. Remote
        # forwards target the agent side and therefore do not reserve local ports.
        requested = [port for forward in spec.local_forwards for port in forward.local_ports]
        duplicates = sorted({port for port in requested if requested.count(port) > 1})
        if duplicates:
            raise RelayBridgeError(f"duplicate local port in command: {duplicates[0]}")
        active = {port for connection in self._connections.values() for port in _local_ports(connection)}
        for port in requested:
            if port in active:
                raise RelayBridgeError(f"local port already in use by this shell: {port}")

    def _reap_finished(self) -> None:
        for connection_id, connection in list(self._connections.items()):
            if connection.process.poll() is not None:
                connection.status = "exited"
                self._connections.pop(connection_id, None)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if process.stdin:
                # Prefer graceful SDK shutdown before falling back to terminate.
                process.stdin.write("STOP\n")
                process.stdin.flush()
                process.wait(timeout=1)
                return
        except (BrokenPipeError, OSError):
            pass
        except subprocess.TimeoutExpired:
            pass
        try:
            process.terminate()
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def default_helper_command() -> list[str]:
    configured = os.environ.get("EVILAZP_RELAY_BRIDGE_HELPER")
    if configured:
        return shlex.split(configured)
    helper_project = Path(__file__).resolve().parents[3] / "helpers" / "relay-bridge-client" / "relay-bridge-client.csproj"
    return ["dotnet", "run", "--project", str(helper_project), "--"]


def _local_ports(connection: RelayBridgeConnection) -> tuple[int, ...]:
    return tuple(port for forward in connection.forwards for port in forward.local_ports)


def _sanitize(value: str, connection_string: str) -> str:
    return value.replace(connection_string, "[connection-string]") if connection_string else value


def _read_process_lines(process: subprocess.Popen[str], lines: queue.Queue[str]) -> None:
    if not process.stdout:
        return
    for line in process.stdout:
        lines.put(line)
