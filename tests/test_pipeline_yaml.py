from pathlib import Path

from azure_pipeline_cli.pipeline_yaml import render_pipeline_yaml


def test_checked_in_yaml_matches_template():
    root = Path(__file__).resolve().parents[1]
    assert (root / "azure-pipelines.yml").read_text() == render_pipeline_yaml()


def test_yaml_executes_command_without_nested_temp_script():
    yaml = render_pipeline_yaml()

    assert "[ScriptBlock]::Create($command)" in yaml
    assert "& $scriptBlock *>&1" in yaml
    assert "EVILAZP_RESULT_B64" not in yaml
    assert "EVILAZP_RESULT_EXIT" not in yaml
    assert "::EVILAZP_START::$runId" in yaml
    assert "::EVILAZP_END::$runId::EXIT::$exitCode" in yaml
    assert "condition: eq(variables['Agent.OS'], 'Windows_NT')" not in yaml
    assert "condition: ne(variables['Agent.OS'], 'Windows_NT')" not in yaml
    assert "- bash: |" not in yaml
    assert "workingDirectory: $(Agent.HomeDirectory)" in yaml
    assert "evilazp-$runId.ps1" not in yaml
    assert "powershell -NoProfile -ExecutionPolicy Bypass -File" not in yaml
