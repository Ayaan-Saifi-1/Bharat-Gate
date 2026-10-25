import ast
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from bharat_gate.cli.doctor import run_doctor_diagnostics
from bharat_gate.cli.git_ops import (
    HOOK_MARKER,
    get_git_diff,
    get_hook_status,
    install_pre_push_hook,
    load_last_audit_result,
    save_last_audit_result,
    uninstall_pre_push_hook,
)
from bharat_gate.cli.main import run_cli
from bharat_gate.core.gateway.client import BharatCodeClient
from bharat_gate.data.prebaked_replays import (
    SCENARIO_1,
    get_all_scenarios,
    get_scenario_by_id,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_prebaked_scenarios_load_and_validate() -> None:
    """Verifies all 3 scenarios and 1, 2, 3 aliases return valid PrebakedScenario objects with valid test ASTs."""
    scenarios = get_all_scenarios()
    assert len(scenarios) == 3

    s1 = get_scenario_by_id("1")
    s2 = get_scenario_by_id("2")
    s3 = get_scenario_by_id("3")

    assert s1.id == "sse-gpu-leak"
    assert s2.id == "webhook-paise-bug"
    assert s3.id == "auth-sqli-secret"

    assert get_scenario_by_id("sse-gpu-leak").id == "sse-gpu-leak"
    assert get_scenario_by_id("webhook-paise-bug").id == "webhook-paise-bug"
    assert get_scenario_by_id("auth-sqli-secret").id == "auth-sqli-secret"

    for s in scenarios:
        assert len(s.sample_files) > 0
        assert s.raw_diff.strip() != ""
        assert s.recorded_result.verdict.verdict == "BLOCK"
        # Validate synthesized python pytest AST
        tree = ast.parse(s.recorded_result.synthesized_test_code)
        assert tree is not None

    with pytest.raises(KeyError, match="not found"):
        get_scenario_by_id("non-existent-scenario")


def test_doctor_no_llm_passes_local_checks() -> None:
    """Runs run_cli(['doctor', '--no-llm']) and asserts exit code 0."""
    code = run_cli(["doctor", "--no-llm"], repo_root=ROOT_DIR)
    assert code == 0


def test_doctor_with_mock_live_client_passes_all_6(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Injects a mock BharatCodeClient returning x-request-id: req-doc-1 and asserts report.all_passed is True."""
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_mock_api_key_valid_12345")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
            headers={"x-request-id": "req-doc-1"},
        )

    mock_client = BharatCodeClient(
        api_key="bc_live_mock_api_key_valid_12345",
        cache_db_path=tmp_path / "mock_doc.sqlite3",
        transport=httpx.MockTransport(handler),
    )

    report = run_doctor_diagnostics(repo_root=ROOT_DIR, skip_live_llm=False, client=mock_client)
    mock_client.close()

    assert report.local_passed is True
    assert report.all_passed is True
    assert report.live_ping is not None
    assert report.live_ping.ok is True
    assert report.live_ping.request_id == "req-doc-1"


def test_git_hook_install_status_and_uninstall_preserves_backup(tmp_path: Path) -> None:
    """Creates a fake .git/hooks/pre-push, installs hook, verifies backup, uninstalls and restores."""
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)

    orig_hook = hooks_dir / "pre-push"
    orig_content = "#!/bin/sh\n# Custom existing user hook\nexit 0\n"
    orig_hook.write_text(orig_content, encoding="utf-8")

    # 1. Install
    installed_path = install_pre_push_hook(tmp_path)
    assert installed_path.exists()
    new_content = installed_path.read_text(encoding="utf-8")
    assert HOOK_MARKER in new_content

    backup_path = hooks_dir / "pre-push.bharatgate.bak"
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == orig_content

    # 2. Status
    status = get_hook_status(tmp_path)
    assert status["installed"] is True
    assert status["backup_exists"] is True

    # 3. Uninstall
    uninstalled = uninstall_pre_push_hook(tmp_path)
    assert uninstalled is True
    assert not backup_path.exists()
    assert orig_hook.exists()
    assert orig_hook.read_text(encoding="utf-8") == orig_content

    status_after = get_hook_status(tmp_path)
    assert status_after["installed"] is False
    assert status_after["backup_exists"] is False


def test_cli_demo_renders_all_three_scenarios(tmp_path: Path) -> None:
    """Runs run_cli(['demo']) for all 3 scenarios and asserts exit code 0."""
    code1 = run_cli(["demo", "--scenario", "1", "--show-thinking", "--show-test"], repo_root=tmp_path)
    assert code1 == 0

    code2 = run_cli(["demo", "--scenario", "2"], repo_root=tmp_path)
    assert code2 == 0

    code3 = run_cli(["demo", "--scenario", "3"], repo_root=tmp_path)
    assert code3 == 0

    code_json = run_cli(["demo", "--scenario", "sse-gpu-leak", "--json"], repo_root=tmp_path)
    assert code_json == 0


def test_cli_check_diff_file_with_offline_mock_blocks_on_secret(tmp_path: Path) -> None:
    """Writes a sample diff with a leaked key to file, runs check --diff-file, asserts exit code 1 (BLOCK)."""
    diff_file = tmp_path / "secret_leak.diff"
    diff_text = """diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -1,1 +1,2 @@
 # Auth module
+BHARATCODE_API_KEY = "bc_live_supersecrettoken1234567890"
"""
    diff_file.write_text(diff_text, encoding="utf-8")

    code = run_cli(
        ["check", "--diff-file", str(diff_file), "--offline-mock"],
        repo_root=tmp_path,
    )
    assert code == 1


def test_cli_check_docs_only_diff_returns_zero(tmp_path: Path) -> None:
    """Writes a README.md-only diff, runs check --diff-file, and asserts exit code 0 (PASS with 0 tokens)."""
    diff_file = tmp_path / "docs_only.diff"
    diff_text = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 # Project
+Added architectural diagrams and documentation.
"""
    diff_file.write_text(diff_text, encoding="utf-8")

    code = run_cli(
        ["check", "--diff-file", str(diff_file)],
        repo_root=tmp_path,
    )
    assert code == 0


def test_save_and_load_last_audit_result(tmp_path: Path) -> None:
    """Verifies save_last_audit_result and load_last_audit_result round-trip cleanly."""
    saved_path = save_last_audit_result(tmp_path, SCENARIO_1.recorded_result)
    assert saved_path.exists()

    loaded = load_last_audit_result(tmp_path)
    assert loaded is not None
    assert loaded.verdict.verdict == "BLOCK"
    assert loaded.verdict.risk_score == 92
    assert len(loaded.verdict.findings) == len(SCENARIO_1.recorded_result.verdict.findings)
    assert loaded.synthesized_test_code == SCENARIO_1.recorded_result.synthesized_test_code


def test_cli_export_patch_and_regression_test(tmp_path: Path) -> None:
    """Runs demo --scenario 1 followed by export-patch and verifies output files."""
    run_cli(["demo", "--scenario", "1"], repo_root=tmp_path)

    patch_out = tmp_path / "fix.diff"
    test_out = tmp_path / "test_synthesized.py"

    code = run_cli(
        [
            "export-patch",
            "--output",
            str(patch_out),
            "--export-test",
            str(test_out),
        ],
        repo_root=tmp_path,
    )
    assert code == 0
    assert patch_out.exists() and patch_out.stat().st_size > 0
    assert test_out.exists() and test_out.stat().st_size > 0

    # Ensure synthesized test is valid python
    tree = ast.parse(test_out.read_text(encoding="utf-8"))
    assert tree is not None


def test_cli_benchmark_command_json_and_rich(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Runs run_cli(['benchmark', '--json']) and verifies tokens_saved_pct > 50."""
    code_rich = run_cli(["benchmark"], repo_root=tmp_path)
    assert code_rich == 0

    code_json = run_cli(["benchmark", "--json"], repo_root=tmp_path)
    assert code_json == 0

    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
    assert len(lines) > 0
    json_start_idx = captured.out.find("{")
    data = json.loads(captured.out[json_start_idx:])
    assert data["tokens_saved_pct"] > 50.0
    assert data["total_tokens_actual"] == 1420
    assert len(data["workloads"]) == 3


def test_git_diff_extraction_in_temp_git_repo(tmp_path: Path) -> None:
    """Initializes a real git repo in tmp_path and verifies get_git_diff captures staged changes."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)

    app_file = tmp_path / "app.py"
    app_file.write_text("print('version 1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    app_file.write_text("print('version 2')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)

    staged_diff = get_git_diff(tmp_path, staged=True)
    assert "+print('version 2')" in staged_diff
    assert "-print('version 1')" in staged_diff


def test_manage_py_entrypoint_execution() -> None:
    """Runs subprocess executing manage.py doctor --no-llm and asserts exit code 0."""
    proc = subprocess.run(
        [sys.executable, "manage.py", "doctor", "--no-llm"],
        cwd=ROOT_DIR,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10.0,
    )
    assert proc.returncode == 0
