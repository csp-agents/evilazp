import pytest

from azure_pipeline_cli.azure_files import (
    AzureFilesError,
    AzureFilesConfig,
    build_agent_transfer_script,
    create_directory,
    create_share,
    download,
    generate_share_sas_token,
    list_directory,
    list_shares,
    load_azure_files_config,
    parse_azure_files_command,
    parse_azure_files_management_command,
    parse_remote_path,
    remove_directory,
    remove_share,
    save_azure_files_config_values,
    upload,
)


class MissingRemote(Exception):
    status_code = 404


class ExistsRemote(Exception):
    status_code = 409


class FakeDownload:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data


class FakeFileClient:
    def __init__(self, share, path):
        self.share = share
        self.path = path

    def get_file_properties(self):
        if self.path not in self.share.files:
            raise MissingRemote("not found")
        return {}

    def upload_file(self, handle):
        self.share.uploads.append((self.share.name, self.path, handle.read()))
        self.share.files[self.path] = self.share.uploads[-1][2]

    def download_file(self):
        if self.path not in self.share.files:
            raise MissingRemote("not found")
        return FakeDownload(self.share.files[self.path])


class FakeDirectoryClient:
    def __init__(self, share, path):
        self.share = share
        self.path = path

    def create_directory(self):
        if self.path in self.share.directories:
            raise ExistsRemote("already exists")
        self.share.directories.add(self.path)

    def delete_directory(self):
        if self.path not in self.share.directories:
            raise MissingRemote("not found")
        self.share.directories.remove(self.path)

    def list_directories_and_files(self):
        prefix = f"{self.path}/" if self.path else ""
        children = []
        seen_dirs = set()
        for path in self.share.files:
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            if "/" in rest:
                dirname = rest.split("/", 1)[0]
                if dirname not in seen_dirs:
                    children.append({"name": dirname, "type": "directory"})
                    seen_dirs.add(dirname)
            elif rest:
                children.append({"name": rest, "type": "file"})
        if not children and self.path not in self.share.directories:
            raise MissingRemote("not found")
        return children


class FakeShareClient:
    def __init__(self, name):
        self.name = name
        self.files = {}
        self.directories = set()
        self.uploads = []

    def get_file_client(self, path):
        return FakeFileClient(self, path)

    def get_directory_client(self, path):
        return FakeDirectoryClient(self, path)

    def list_directories_and_files(self, directory_name=None):
        return FakeDirectoryClient(self, directory_name or "").list_directories_and_files()


class FakeService:
    def __init__(self):
        self.shares = {}

    def get_share_client(self, name):
        return self.shares.setdefault(name, FakeShareClient(name))

    def list_shares(self):
        return [{"name": name} for name in sorted(self.shares)]

    def create_share(self, name):
        if name in self.shares:
            raise ExistsRemote("already exists")
        self.shares[name] = FakeShareClient(name)

    def delete_share(self, name):
        if name not in self.shares:
            raise MissingRemote("not found")
        del self.shares[name]


def write_config(tmp_path):
    config = tmp_path / ".env"
    config.write_text("azure_files_account: acct\nazure_files_sas: ?sig=secret\n")
    return config


def write_config_with_key(tmp_path):
    config = tmp_path / ".env"
    config.write_text(
        "azure_files_account: acct\n"
        "azure_files_sas: ?sig=secret\n"
        "azure_files_account_key: YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=\n"
    )
    return config


def write_key_only_config(tmp_path):
    config = tmp_path / ".env"
    config.write_text(
        "azure_files_account: acct\n"
        "azure_files_account_key: YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=\n"
    )
    return config


def test_loads_azure_files_config_from_env(tmp_path):
    config_path = write_config(tmp_path)

    config = load_azure_files_config(config_path)

    assert config.account == "acct"
    assert config.sas == "?sig=secret"


def test_loads_optional_account_key_from_env(tmp_path):
    config_path = write_config_with_key(tmp_path)

    config = load_azure_files_config(config_path)

    assert config.account_key.startswith("YWJj")


def test_save_azure_files_config_values_preserves_other_lines(tmp_path):
    config = tmp_path / ".env"
    config.write_text("# keep\nurl: https://dev.azure.com/org\nazure_files_account: old\n")

    save_azure_files_config_values(
        {
            "azure_files_account": "acct",
            "azure_files_account_key": "key-secret",
            "azure_files_sas": "sig=sas-secret",
        },
        config,
    )

    text = config.read_text()
    assert "# keep" in text
    assert "url: https://dev.azure.com/org" in text
    assert "azure_files_account: 'acct'" in text
    assert "azure_files_account_key: 'key-secret'" in text
    assert "azure_files_sas: 'sig=sas-secret'" in text


def test_parse_target_option_defaults_to_local_and_accepts_agent():
    local = parse_azure_files_command("upload", "/tmp/a.txt /share/a.txt")
    agent = parse_azure_files_command("download", "--target agent /share/a.txt /tmp/a.txt")

    assert local.target == "local"
    assert local.overwrite is True
    assert agent.target == "agent"
    assert agent.overwrite is False


def test_parse_transfer_preserves_windows_paths():
    upload_command = parse_azure_files_command("upload", r"--target agent C:\Windows\Temp\a.txt /share/dir/")
    download_command = parse_azure_files_command("download", "/share/dir/a.txt C:\\Windows\\Temp\\")

    assert upload_command.source == r"C:\Windows\Temp\a.txt"
    assert upload_command.destination == "/share/dir/"
    assert download_command.destination == "C:\\Windows\\Temp\\"


def test_parse_rejects_invalid_target():
    with pytest.raises(AzureFilesError, match="target must be local or agent"):
        parse_azure_files_command("upload", "--target nowhere /tmp/a.txt /share/a.txt")


def test_parse_azure_files_management_commands():
    assert parse_azure_files_management_command("share list").action == "share-list"
    assert parse_azure_files_management_command("share create data").share == "data"
    assert parse_azure_files_management_command("share remove data").action == "share-remove"

    directory = parse_azure_files_management_command("directory create --share data uploads/a")
    assert directory.action == "directory-create"
    assert directory.share == "data"
    assert directory.path == "uploads/a"

    listing = parse_azure_files_management_command("list --share /data/uploads")
    assert listing.action == "list"
    assert listing.share == "data"
    assert listing.path == "uploads"

    root_listing = parse_azure_files_management_command("list --share /data")
    assert root_listing.path == ""

    sas = parse_azure_files_management_command("sas")
    assert sas.permissions == "rwld"
    assert sas.hours == 24

    sas_without_share = parse_azure_files_management_command("sas")
    assert sas_without_share.share == ""
    assert sas_without_share.permissions == "rwld"
    assert sas_without_share.hours == 24

    key = parse_azure_files_management_command("storage-account key --account st --resource-group rg")
    assert key.action == "storage-account-key"
    assert key.share == "st"
    assert key.resource_group == "rg"

    storage = parse_azure_files_management_command("storage-account create st -g rg -l koreacentral")
    assert storage.action == "storage-account-create"
    assert storage.share == "st"
    assert storage.resource_group == "rg"
    assert storage.location == "koreacentral"


def test_parse_azure_files_management_rejects_bad_inputs():
    with pytest.raises(AzureFilesError, match="usage: /azure-files share create"):
        parse_azure_files_management_command("share create")
    with pytest.raises(AzureFilesError, match="usage: /azure-files directory remove"):
        parse_azure_files_management_command("directory remove --share data")
    with pytest.raises(AzureFilesError, match="share path"):
        parse_azure_files_management_command("list --share data/uploads")
    with pytest.raises(AzureFilesError, match="permissions"):
        parse_azure_files_management_command("sas --permissions xyz")
    with pytest.raises(AzureFilesError, match="missing required option: -g"):
        parse_azure_files_management_command("storage-account create st -l koreacentral")


@pytest.mark.parametrize("arg", ["", "one", "one two three"])
def test_upload_requires_two_positional_args(arg):
    with pytest.raises(AzureFilesError, match="usage: /upload"):
        parse_azure_files_command("upload", arg)


@pytest.mark.parametrize("arg", ["", "one", "one two three"])
def test_download_requires_two_positional_args(arg):
    with pytest.raises(AzureFilesError, match="usage: /download"):
        parse_azure_files_command("download", arg)


@pytest.mark.parametrize("value", ["share/file.txt", "/share", "/"])
def test_remote_path_must_include_share_and_file_path(value):
    with pytest.raises(AzureFilesError, match="remote path"):
        parse_remote_path(value)


def test_agent_transfer_script_uses_azure_files_rest():
    command = parse_azure_files_command("upload", "--target agent --overwrite /tmp/a.txt /share/dir/a.txt")

    script = build_agent_transfer_script(command, AzureFilesConfig("acct", "?sig=secret"))

    assert "https://$Account.file.core.windows.net" in script
    assert "Send-AzFileOne" in script
    assert "$Overwrite = $true" in script
    assert "[upload] $LocalPath -> /$Share/$effectiveRemoteRoot" in script


def test_agent_transfer_script_supports_bash_rest():
    command = parse_azure_files_command("download", "--target agent /share/dir/a.txt /tmp/a.txt")

    script = build_agent_transfer_script(command, AzureFilesConfig("acct", "?sig=secret"), shell="bash")

    assert "python3 - <<'PY'" in script
    assert "EVILAZP_AF_ACTION=download" in script
    assert "urllib.request" in script
    assert "[download] /{SHARE}/{REMOTE_ROOT} -> {LOCAL_PATH}" in script
    assert "Invoke-WebRequest" not in script


def test_share_and_directory_management_calls_sdk(tmp_path):
    config_path = write_config(tmp_path)
    service = FakeService()

    assert create_share("data", config_path=config_path, service_client_factory=lambda _config: service) == "data"
    assert list_shares(config_path=config_path, service_client_factory=lambda _config: service) == ["data"]

    remote = create_directory("data", "uploads/a", config_path=config_path, service_client_factory=lambda _config: service)
    assert remote.display == "/data/uploads/a"
    assert {"uploads", "uploads/a"}.issubset(service.get_share_client("data").directories)

    removed = remove_directory("data", "uploads/a", config_path=config_path, service_client_factory=lambda _config: service)
    assert removed.display == "/data/uploads/a"
    assert "uploads/a" not in service.get_share_client("data").directories

    assert remove_share("data", config_path=config_path, service_client_factory=lambda _config: service) == "data"
    assert list_shares(config_path=config_path, service_client_factory=lambda _config: service) == []


def test_list_directory_returns_one_level_entries(tmp_path):
    config_path = write_config(tmp_path)
    service = FakeService()
    share = service.get_share_client("data")
    share.directories.update({"uploads", "uploads/a"})
    share.files["uploads/a.txt"] = b"a"
    share.files["uploads/a/child.txt"] = b"child"

    items = list_directory("data", "uploads", config_path=config_path, service_client_factory=lambda _config: service)

    assert [(item.type, item.name, item.size) for item in items] == [
        ("directory", "a", "-"),
        ("file", "a.txt", "-"),
    ]


def test_generate_share_sas_uses_account_key(tmp_path):
    config_path = write_key_only_config(tmp_path)

    token = generate_share_sas_token("data", config_path=config_path)

    assert "sig=" in token
    assert "ss=f" in token
    assert "srt=sco" in token


def test_generate_share_sas_requires_account_key(tmp_path):
    config_path = write_config(tmp_path)

    with pytest.raises(AzureFilesError, match="azure_files_account_key"):
        generate_share_sas_token("data", config_path=config_path)


def test_single_file_upload_calls_expected_share_and_path(tmp_path):
    config_path = write_config(tmp_path)
    local = tmp_path / "local.txt"
    local.write_text("hello")
    service = FakeService()

    result = upload(local, "/share/dir/remote.txt", config_path=config_path, service_client_factory=lambda _config: service)

    share = service.shares["share"]
    assert result.files == 1
    assert share.uploads == [("share", "dir/remote.txt", b"hello")]
    assert "dir" in share.directories


def test_download_single_file_writes_local_path(tmp_path):
    config_path = write_config(tmp_path)
    service = FakeService()
    service.get_share_client("share").files["dir/remote.txt"] = b"hello"
    local = tmp_path / "out.txt"

    result = download("/share/dir/remote.txt", local, config_path=config_path, service_client_factory=lambda _config: service)

    assert result.files == 1
    assert local.read_bytes() == b"hello"


def test_upload_existing_file_overwrites_by_default(tmp_path):
    config_path = write_config(tmp_path)
    local = tmp_path / "local.txt"
    local.write_text("new")
    service = FakeService()
    service.get_share_client("share").files["remote.txt"] = b"old"

    upload(local, "/share/remote.txt", config_path=config_path, service_client_factory=lambda _config: service)
    assert service.get_share_client("share").files["remote.txt"] == b"new"

    local.write_text("newer")
    with pytest.raises(AzureFilesError, match="already exists"):
        upload(local, "/share/remote.txt", overwrite=False, config_path=config_path, service_client_factory=lambda _config: service)


def test_download_existing_file_requires_overwrite(tmp_path):
    config_path = write_config(tmp_path)
    service = FakeService()
    service.get_share_client("share").files["remote.txt"] = b"new"
    local = tmp_path / "out.txt"
    local.write_text("old")

    with pytest.raises(AzureFilesError, match="already exists"):
        download("/share/remote.txt", local, config_path=config_path, service_client_factory=lambda _config: service)

    download("/share/remote.txt", local, overwrite=True, config_path=config_path, service_client_factory=lambda _config: service)
    assert local.read_bytes() == b"new"


def test_recursive_directory_upload_preserves_relative_paths(tmp_path):
    config_path = write_config(tmp_path)
    root = tmp_path / "src"
    (root / "a").mkdir(parents=True)
    (root / "a" / "one.txt").write_text("1")
    (root / "two.txt").write_text("2")
    service = FakeService()

    result = upload(root, "/share/base", config_path=config_path, service_client_factory=lambda _config: service)

    assert result.files == 2
    assert service.get_share_client("share").files == {
        "base/a/one.txt": b"1",
        "base/two.txt": b"2",
    }


def test_upload_directory_target_appends_source_basename(tmp_path):
    config_path = write_config(tmp_path)
    local = tmp_path / "local.txt"
    local.write_text("hello")
    service = FakeService()

    result = upload(local, "/share/dir/", config_path=config_path, service_client_factory=lambda _config: service)

    assert result.destination == "/share/dir/local.txt"
    assert service.get_share_client("share").files["dir/local.txt"] == b"hello"


def test_recursive_directory_download_preserves_relative_paths(tmp_path):
    config_path = write_config(tmp_path)
    service = FakeService()
    share = service.get_share_client("share")
    share.directories.add("base")
    share.files["base/a/one.txt"] = b"1"
    share.files["base/two.txt"] = b"2"
    destination = tmp_path / "dst"

    result = download("/share/base", destination, config_path=config_path, service_client_factory=lambda _config: service)

    assert result.files == 2
    assert (destination / "a" / "one.txt").read_bytes() == b"1"
    assert (destination / "two.txt").read_bytes() == b"2"


def test_download_directory_target_appends_remote_basename(tmp_path):
    config_path = write_config(tmp_path)
    service = FakeService()
    service.get_share_client("share").files["dir/remote.txt"] = b"hello"
    destination = tmp_path / "dst"

    result = download("/share/dir/remote.txt", str(destination) + "/", config_path=config_path, service_client_factory=lambda _config: service)

    assert result.destination == str(destination / "remote.txt")
    assert (destination / "remote.txt").read_bytes() == b"hello"
