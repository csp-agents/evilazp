from azure_pipeline_cli.sessions import (
    SessionRecord,
    delete_session,
    find_session,
    load_sessions,
    masked_pat,
    masked_secret,
    print_sessions_table,
    save_session,
)


def test_save_and_load_session(tmp_path):
    path = tmp_path / "sessions.json"

    save_session(
        SessionRecord(
            name="lab",
            organization="https://dev.azure.com/mick3y",
            pat="token",
            project="proj",
            pipeline="pipe",
            pool="pool",
            sp_tenant_id="tenant",
            sp_client_id="client",
            sp_client_secret="secret",
        ),
        path,
    )

    sessions = load_sessions(path)

    assert len(sessions) == 1
    assert sessions[0].name == "lab"
    assert sessions[0].organization == "https://dev.azure.com/mick3y"
    assert sessions[0].pat == "token"
    assert sessions[0].project == "proj"
    assert sessions[0].pipeline == "pipe"
    assert sessions[0].pool == "pool"
    assert sessions[0].sp_tenant_id == "tenant"
    assert sessions[0].sp_client_id == "client"
    assert sessions[0].sp_client_secret == "secret"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_load_session_without_sp_fields_is_backward_compatible(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(
        '{"sessions":[{"name":"lab","organization":"org","pat":"token"}]}',
        encoding="utf-8",
    )

    session = load_sessions(path)[0]

    assert session.name == "lab"
    assert session.sp_tenant_id is None
    assert session.sp_client_id is None
    assert session.sp_client_secret is None


def test_save_session_replaces_same_name(tmp_path):
    path = tmp_path / "sessions.json"

    save_session(SessionRecord("lab", "org1", "token1"), path)
    save_session(SessionRecord("lab", "org2", "token2"), path)

    sessions = load_sessions(path)

    assert len(sessions) == 1
    assert sessions[0].organization == "org2"
    assert sessions[0].pat == "token2"


def test_find_and_delete_session(tmp_path):
    path = tmp_path / "sessions.json"
    save_session(SessionRecord("one", "org1", "token1"), path)
    save_session(SessionRecord("two", "org2", "token2"), path)
    sessions = load_sessions(path)

    assert find_session("2", sessions).name == "two"
    assert find_session("one", sessions).organization == "org1"
    assert delete_session("one", path)
    assert [item.name for item in load_sessions(path)] == ["two"]


def test_print_sessions_table(capsys):
    print_sessions_table([SessionRecord("lab", "https://dev.azure.com/mick3y", "token", project="proj", pool="pool")])

    captured = capsys.readouterr()
    assert "Name" in captured.out
    assert "lab" in captured.out
    assert "proj" in captured.out


def test_masked_pat():
    assert masked_pat("abcdefghijkl") == "abcd...ijkl"


def test_masked_secret():
    assert masked_secret("secret") == "se...et"
    assert masked_secret(None) == "-"
