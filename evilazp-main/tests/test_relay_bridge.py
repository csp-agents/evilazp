import sys

import pytest

from azure_pipeline_cli.relay_bridge import HybridConnectionError, HybridConnectionManager, RelayBridgeError, RelayBridgeManager, parse_relay_bridge_command
from azure_pipeline_cli.relay_bridge.hybrid_connections import format_azure_cli_error


def test_parse_local_forward():
    command = parse_relay_bridge_command('start -x "Endpoint=sb://example/;SharedAccessKey=secret" -L 2222:ssh-relay1234')

    assert command.action == "connect"
    assert command.spec.connection_string.startswith("Endpoint=")
    assert command.spec.local_forwards[0].relay_name == "ssh-relay1234"
    assert command.spec.local_forwards[0].local_ports == (2222,)
    assert command.spec.local_forwards[0].expression == "127.0.0.1:2222:ssh-relay1234"


def test_parse_remote_forward():
    command = parse_relay_bridge_command('start -x cs -T ssh-relay1234:127.0.0.1:22')

    assert command.spec.remote_forwards[0].relay_name == "ssh-relay1234"
    assert command.spec.remote_forwards[0].endpoint == "127.0.0.1:22"


def test_parse_remote_http_forward():
    command = parse_relay_bridge_command('start -x cs -H ssh-relay1234:http/127.0.0.1:8080')

    assert command.spec.remote_http_forwards[0].relay_name == "ssh-relay1234"
    assert command.spec.remote_http_forwards[0].endpoint == "http/127.0.0.1:8080"


def test_parse_repeated_forwards():
    command = parse_relay_bridge_command("start -x cs -L 2222:ssh -L 3333:web -T ssh:127.0.0.1:22 -H web:http/127.0.0.1:8080")

    assert [item.local_ports for item in command.spec.local_forwards] == [(2222,), (3333,)]
    assert len(command.spec.forwards) == 4


def test_parse_list_and_stop():
    assert parse_relay_bridge_command("list").action == "list"
    stop = parse_relay_bridge_command("stop all")
    assert stop.action == "stop"
    assert stop.target == "all"
    assert parse_relay_bridge_command("stop 1").target == "1"


def test_parse_rejects_missing_connection_string_or_forward():
    with pytest.raises(ValueError, match="usage: /relay-bridge start"):
        parse_relay_bridge_command("-L 2222:ssh")
    with pytest.raises(ValueError, match="missing required option"):
        parse_relay_bridge_command("start -L 2222:ssh")
    with pytest.raises(ValueError, match="at least one"):
        parse_relay_bridge_command("start -x cs")
    with pytest.raises(ValueError, match="invalid local port"):
        parse_relay_bridge_command("start -x cs -L 70000:ssh")


def test_manager_starts_lists_and_stops_fake_helper():
    manager = RelayBridgeManager([sys.executable, "tests/fixtures/fake_relay_bridge_helper.py"], ready_timeout=2)
    spec = parse_relay_bridge_command("start -x cs -L 2222:ssh-relay1234").spec

    connection = manager.start(spec)

    assert connection.id == 1
    assert manager.list()[0].forwards[0].relay_name == "ssh-relay1234"
    stopped = manager.stop("2222")
    assert stopped[0].status == "stopped"
    assert manager.list() == []


def test_manager_blocks_duplicate_local_port():
    manager = RelayBridgeManager([sys.executable, "tests/fixtures/fake_relay_bridge_helper.py"], ready_timeout=2)
    spec = parse_relay_bridge_command("start -x cs -L 2222:ssh").spec
    manager.start(spec)

    with pytest.raises(RelayBridgeError, match="local port already in use"):
        manager.start(spec)

    manager.shutdown()


def test_manager_surfaces_sanitized_helper_error():
    manager = RelayBridgeManager([sys.executable, "tests/fixtures/fake_relay_bridge_helper.py"], ready_timeout=2)
    spec = parse_relay_bridge_command("start -x Endpoint=sb://example/;SharedAccessKey=secret;fail=true -L 2222:ssh").spec

    with pytest.raises(RelayBridgeError, match="authentication failed"):
        manager.start(spec)


def test_manager_reaps_exited_helper():
    manager = RelayBridgeManager([sys.executable, "tests/fixtures/fake_relay_bridge_helper.py"], ready_timeout=2)
    spec = parse_relay_bridge_command("start -x cs -L 2222:ssh").spec
    connection = manager.start(spec)
    connection.process.terminate()
    connection.process.wait(timeout=3)

    assert manager.list() == []


def test_parse_hc_commands():
    command = parse_relay_bridge_command("hc list -g rg-relay -n relayns")
    assert command.action == "hc-list"
    assert command.resource_group == "rg-relay"
    assert command.namespace == "relayns"

    create = parse_relay_bridge_command("hc create -g rg-relay -n relayns winvm03-ssh --no-auth")
    assert create.action == "hc-create"
    assert create.hc_name == "winvm03-ssh"
    assert create.requires_client_authorization is False

    delete = parse_relay_bridge_command("hc remove -g rg-relay -n relayns winvm03-ssh")
    assert delete.action == "hc-delete"
    assert delete.requires_client_authorization is True


def test_parse_namespace_commands():
    command = parse_relay_bridge_command("ns list -g rg-relay")
    assert command.action == "ns-list"
    assert command.resource_group == "rg-relay"

    create = parse_relay_bridge_command("ns create -g rg-relay -n relayns -l koreacentral")
    assert create.action == "ns-create"
    assert create.namespace == "relayns"
    assert create.location == "koreacentral"

    delete = parse_relay_bridge_command("ns remove -g rg-relay -n relayns")
    assert delete.action == "ns-delete"
    assert delete.namespace == "relayns"

    keys = parse_relay_bridge_command("ns keys -g rg-relay -n relayns --rule listen")
    assert keys.action == "ns-keys"
    assert keys.auth_rule == "listen"


def test_hybrid_connection_manager_uses_azure_cli():
    calls = []

    class FakeResult:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, check, stdout, stderr, text):
        calls.append(command)
        if "list" in command:
            return FakeResult('[{"name":"ssh","requiresClientAuthorization":true}]')
        if "create" in command:
            return FakeResult('{"name":"ssh","requiresClientAuthorization":false}')
        return FakeResult("")

    import azure_pipeline_cli.relay_bridge.hybrid_connections as hc

    original = hc.subprocess.run
    hc.subprocess.run = fake_run
    try:
        manager = HybridConnectionManager(["az"])
        assert manager.list("rg", "ns")[0].name == "ssh"
        assert manager.create("rg", "ns", "ssh", requires_client_authorization=False).requires_client_authorization is False
        manager.delete("rg", "ns", "ssh")
    finally:
        hc.subprocess.run = original

    assert calls[0][:3] == ["az", "relay", "hyco"]
    assert "--requires-client-authorization" in calls[1]
    assert "--yes" in calls[2]


def test_hybrid_connection_manager_manages_namespaces_with_azure_cli():
    calls = []

    class FakeResult:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, check, stdout, stderr, text):
        calls.append(command)
        if command[1:4] == ["relay", "namespace", "list"]:
            return FakeResult('[{"name":"relayns","location":"koreacentral","provisioningState":"Succeeded"}]')
        if command[1:4] == ["relay", "namespace", "create"]:
            return FakeResult('{"name":"relayns","location":"koreacentral","provisioningState":"Succeeded"}')
        if command[1:6] == ["relay", "namespace", "authorization-rule", "keys", "list"]:
            return FakeResult('{"primaryConnectionString":"Endpoint=sb://relayns/;SharedAccessKey=secret","keyName":"RootManageSharedAccessKey"}')
        return FakeResult("")

    import azure_pipeline_cli.relay_bridge.hybrid_connections as hc

    original = hc.subprocess.run
    hc.subprocess.run = fake_run
    try:
        manager = HybridConnectionManager(["az"])
        assert manager.list_namespaces("rg")[0].name == "relayns"
        assert manager.create_namespace("rg", "relayns", "koreacentral").location == "koreacentral"
        assert manager.get_namespace_keys("rg", "relayns").primary_connection_string.startswith("Endpoint=sb://relayns/")
        manager.delete_namespace("rg", "relayns")
    finally:
        hc.subprocess.run = original

    assert calls[0][:3] == ["az", "relay", "namespace"]
    assert "-l" in calls[1]
    assert calls[2][1:6] == ["relay", "namespace", "authorization-rule", "keys", "list"]
    assert calls[3][1:4] == ["relay", "namespace", "delete"]


def test_formats_parent_resource_not_found_for_namespace_keys():
    message = (
        "ERROR: (ParentResourceNotFound) Failed to perform 'action' on resource(s) of type "
        "'namespaces/authorizationrules', because the parent resource "
        "'/subscriptions/sub/resourceGroups/rg-relay-bridge/providers/Microsoft.Relay/namespaces/relaybridge1337' "
        "could not be found.\nCode: ParentResourceNotFound"
    )
    command = [
        "az",
        "relay",
        "namespace",
        "authorization-rule",
        "keys",
        "list",
        "-g",
        "rg-relay-bridge",
        "--namespace-name",
        "relaybridge1337",
        "-n",
        "RootManageSharedAccessKey",
    ]

    formatted = format_azure_cli_error(message, command)

    assert "Azure Relay namespace를 찾지 못해서 namespace key 조회 작업을 할 수 없습니다" in formatted
    assert "rg-relay-bridge" in formatted
    assert "relaybridge1337" in formatted
    assert "/relay-bridge ns list -g rg-relay-bridge" in formatted
    assert "namespaces/authorizationrules" not in formatted


def test_formats_common_azure_cli_errors():
    auth = format_azure_cli_error(
        "ERROR: (AuthorizationFailed) The client does not have authorization to perform action.",
        ["az", "relay", "hyco", "list", "-g", "rg", "--namespace-name", "ns"],
    )
    provider = format_azure_cli_error(
        'Code="MissingSubscriptionRegistration" Message="The subscription is not registered to use namespace Microsoft.Relay"',
        ["az", "relay", "namespace", "create", "-g", "rg", "-n", "ns"],
    )

    assert "권한이 없습니다" in auth
    assert "tenant/subscription" in auth
    assert "Microsoft.Relay resource provider" in provider
    assert "az provider register --namespace Microsoft.Relay" in provider


def test_hybrid_connection_manager_raises_formatted_azure_cli_error():
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "ERROR: (ResourceGroupNotFound) Resource group 'rg-missing' could not be found."

    def fake_run(command, check, stdout, stderr, text):
        return FakeResult()

    import azure_pipeline_cli.relay_bridge.hybrid_connections as hc

    original = hc.subprocess.run
    hc.subprocess.run = fake_run
    try:
        manager = HybridConnectionManager(["az"])
        with pytest.raises(HybridConnectionError, match="resource group 'rg-missing'를 찾지 못했습니다"):
            manager.list_namespaces("rg-missing")
    finally:
        hc.subprocess.run = original
