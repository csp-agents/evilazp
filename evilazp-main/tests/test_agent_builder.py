import argparse
import subprocess


def test_sign_windows_binary_uses_pfx_and_replaces_binary(monkeypatch, tmp_path):
    from azure_pipeline_cli import agent_builder

    binary = tmp_path / "Agent.Listener.exe"
    binary.write_bytes(b"unsigned")
    pfx = tmp_path / "cert.pfx"
    pfx.write_bytes(b"pfx")
    calls = []

    def fake_which(name):
        return "/usr/bin/osslsigncode" if name == "osslsigncode" else None

    def fake_run(cmd, capture_output=False, text=False):
        calls.append(cmd)
        signed_path = tmp_path / "Agent.Listener.exe.signed"
        signed_path.write_bytes(b"signed")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(agent_builder.shutil, "which", fake_which)
    monkeypatch.setattr(agent_builder.subprocess, "run", fake_run)

    args = argparse.Namespace(
        runtime="win-x64",
        pfx=str(pfx),
        pfx_pass="secret",
        pfx_pass_env="EVILAZP_PFX_PASS",
        timestamp="",
        sign_name="Agent",
        sign_url="https://example.invalid",
    )

    agent_builder.sign_windows_binary(binary, args, quiet=True)

    assert binary.read_bytes() == b"signed"
    assert calls[0][:8] == [
        "/usr/bin/osslsigncode",
        "sign",
        "-pkcs12",
        str(pfx),
        "-pass",
        "secret",
        "-h",
        "sha256",
    ]
    assert "-ts" not in calls[0]


def test_sign_windows_binary_ignores_non_windows_runtime(monkeypatch, tmp_path):
    from azure_pipeline_cli import agent_builder

    binary = tmp_path / "Agent.Listener"
    binary.write_bytes(b"unsigned")

    def fail_which(_name):
        raise AssertionError("osslsigncode should not be checked for non-Windows runtimes")

    monkeypatch.setattr(agent_builder.shutil, "which", fail_which)

    args = argparse.Namespace(runtime="linux-x64")

    agent_builder.sign_windows_binary(binary, args, quiet=True)

    assert binary.read_bytes() == b"unsigned"
