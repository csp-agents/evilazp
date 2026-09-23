import pytest

from azure_pipeline_cli.resource_groups import ResourceGroupManager, parse_resource_group_command


def test_parse_resource_group_commands():
    assert parse_resource_group_command("list").action == "list"

    create = parse_resource_group_command("create -g rg-relay -l koreacentral")
    assert create.action == "create"
    assert create.name == "rg-relay"
    assert create.location == "koreacentral"

    positional_create = parse_resource_group_command("create rg-relay --location koreacentral")
    assert positional_create.name == "rg-relay"
    assert positional_create.location == "koreacentral"

    remove = parse_resource_group_command("remove -g rg-relay --yes")
    assert remove.action == "remove"
    assert remove.name == "rg-relay"
    assert remove.yes is True


def test_parse_resource_group_requires_location():
    with pytest.raises(ValueError, match="missing required option: -l"):
        parse_resource_group_command("create -g rg-relay")


def test_resource_group_manager_uses_azure_cli(monkeypatch):
    calls = []

    class FakeResult:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, check, stdout, stderr, text):
        calls.append(command)
        if command[1:3] == ["group", "list"]:
            return FakeResult('[{"name":"rg","location":"koreacentral","properties":{"provisioningState":"Succeeded"}}]')
        if command[1:3] == ["group", "create"]:
            return FakeResult('{"name":"rg","location":"koreacentral","properties":{"provisioningState":"Succeeded"}}')
        return FakeResult("")

    monkeypatch.setattr("subprocess.run", fake_run)

    manager = ResourceGroupManager(["az"])
    assert manager.list()[0].name == "rg"
    assert manager.create("rg", "koreacentral").location == "koreacentral"
    manager.remove("rg")

    assert calls == [
        ["az", "group", "list", "-o", "json"],
        ["az", "group", "create", "-n", "rg", "-l", "koreacentral", "-o", "json"],
        ["az", "group", "delete", "-n", "rg", "--yes"],
    ]
