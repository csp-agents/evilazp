import pytest

from azure_pipeline_cli.storage_accounts import StorageAccountError, StorageAccountManager, parse_storage_account_command


def test_parse_storage_account_commands():
    assert parse_storage_account_command("list").action == "list"

    create = parse_storage_account_command("create mystorage -g rg -l koreacentral")
    assert create.action == "create"
    assert create.name == "mystorage"
    assert create.resource_group == "rg"
    assert create.location == "koreacentral"
    assert create.sku == "Standard_LRS"
    assert create.kind == "StorageV2"

    remove = parse_storage_account_command("remove -n mystorage -g rg")
    assert remove.action == "remove"
    assert remove.name == "mystorage"

    key = parse_storage_account_command("key --account mystorage --resource-group rg")
    assert key.action == "key"
    assert key.name == "mystorage"
    assert key.resource_group == "rg"


def test_parse_storage_account_rejects_missing_required_options():
    with pytest.raises(ValueError, match="missing required option: -g"):
        parse_storage_account_command("create mystorage -l koreacentral")
    with pytest.raises(ValueError, match="missing required option: -l"):
        parse_storage_account_command("create mystorage -g rg")
    with pytest.raises(ValueError, match="usage: /azure-files storage-account list"):
        parse_storage_account_command("list mystorage")


def test_storage_account_manager_uses_azure_cli():
    calls = []

    class FakeResult:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, check, stdout, stderr, text):
        calls.append(command)
        if command[1:4] == ["storage", "account", "list"]:
            return FakeResult('[{"name":"st","resourceGroup":"rg","location":"koreacentral","sku":{"name":"Standard_LRS"},"kind":"StorageV2"}]')
        if command[1:4] == ["storage", "account", "create"]:
            return FakeResult('{"name":"st","resourceGroup":"rg","location":"koreacentral","sku":{"name":"Standard_LRS"},"kind":"StorageV2"}')
        if command[1:5] == ["storage", "account", "keys", "list"]:
            return FakeResult('[{"value":"secret-key"}]')
        return FakeResult("")

    import azure_pipeline_cli.storage_accounts as storage_accounts

    original = storage_accounts.subprocess.run
    storage_accounts.subprocess.run = fake_run
    try:
        manager = StorageAccountManager(["az"])
        assert manager.list()[0].name == "st"
        assert manager.create("st", "rg", "koreacentral").resource_group == "rg"
        manager.remove("st", "rg")
        assert manager.key("st", "rg") == "secret-key"
    finally:
        storage_accounts.subprocess.run = original

    assert calls[0][:4] == ["az", "storage", "account", "list"]
    assert calls[1][:4] == ["az", "storage", "account", "create"]
    assert "--sku" in calls[1]
    assert calls[2][:4] == ["az", "storage", "account", "delete"]
    assert "--yes" in calls[2]
    assert calls[3][:5] == ["az", "storage", "account", "keys", "list"]
    assert "-g" in calls[3]


def test_storage_account_manager_raises_formatted_error():
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "ERROR: (AuthorizationFailed) denied"

    def fake_run(command, check, stdout, stderr, text):
        return FakeResult()

    import azure_pipeline_cli.storage_accounts as storage_accounts

    original = storage_accounts.subprocess.run
    storage_accounts.subprocess.run = fake_run
    try:
        manager = StorageAccountManager(["az"])
        with pytest.raises(StorageAccountError, match="AuthorizationFailed"):
            manager.list()
    finally:
        storage_accounts.subprocess.run = original
