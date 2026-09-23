import sys

import pytest

from azure_pipeline_cli.devtunnels import (
    DevTunnelCredentials,
    DevTunnelError,
    DevTunnelManager,
    parse_devtunnels_command,
)


def test_parse_connect_with_explicit_local_port():
    command = parse_devtunnels_command("--tunnel-id abc123 --port 22 --local 2222")

    assert command.action == "connect"
    assert command.spec.tunnel_id == "abc123"
    assert command.spec.remote_port == 22
    assert command.spec.local_port == 2222


def test_parse_connect_defaults_local_port_to_remote():
    command = parse_devtunnels_command("--tunnel-id abc123 --port 8080")

    assert command.spec.local_port == 8080


def test_parse_rejects_invalid_ports():
    with pytest.raises(ValueError, match="invalid remote port"):
        parse_devtunnels_command("--tunnel-id abc123 --port 0")

    with pytest.raises(ValueError, match="invalid local port"):
        parse_devtunnels_command("--tunnel-id abc123 --port 22 --local 70000")

    with pytest.raises(ValueError, match="missing required option: --tunnel-id"):
        parse_devtunnels_command("--port 22")


def test_parse_sp_command():
    command = parse_devtunnels_command("sp --tenant-id t --sp-client-id c --sp-secret s")

    assert command.action == "sp"
    assert command.sp_tenant_id == "t"
    assert command.sp_client_id == "c"
    assert command.sp_client_secret == "s"


def test_parse_legacy_devtunnel_aliases():
    command = parse_devtunnels_command("start -id abc123 -port 22 -local 2222")

    assert command.action == "connect"
    assert command.spec.tunnel_id == "abc123"
    assert command.spec.remote_port == 22
    assert command.spec.local_port == 2222


def test_manager_starts_lists_and_stops_fake_helper():
    manager = DevTunnelManager([sys.executable, "tests/fixtures/fake_devtunnel_helper.py"], ready_timeout=2)
    spec = parse_devtunnels_command("--tunnel-id abc123 --port 22 --local 2222").spec

    connection = manager.start(spec, DevTunnelCredentials("tenant", "client", "secret"))

    assert connection.local_port == 2222
    assert manager.list()[0].tunnel_id == "abc123"
    stopped = manager.stop("2222")
    assert stopped[0].status == "stopped"
    assert manager.list() == []


def test_manager_blocks_duplicate_local_port():
    manager = DevTunnelManager([sys.executable, "tests/fixtures/fake_devtunnel_helper.py"], ready_timeout=2)
    spec = parse_devtunnels_command("--tunnel-id abc123 --port 22 --local 2222").spec
    manager.start(spec, DevTunnelCredentials("tenant", "client", "secret"))

    with pytest.raises(DevTunnelError, match="local port already in use"):
        manager.start(spec, DevTunnelCredentials("tenant", "client", "secret"))

    manager.shutdown()


def test_manager_surfaces_helper_error():
    manager = DevTunnelManager([sys.executable, "tests/fixtures/fake_devtunnel_helper.py"], ready_timeout=2)
    spec = parse_devtunnels_command("--tunnel-id fail --port 22 --local 2222").spec

    with pytest.raises(DevTunnelError, match="authentication failed"):
        manager.start(spec, DevTunnelCredentials("tenant", "client", "secret"))
