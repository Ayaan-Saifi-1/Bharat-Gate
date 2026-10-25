"""Git operations, pre-push hook management, and audit result persistence."""

from pathlib import Path
import shutil
import subprocess
from typing import Any

from bharat_gate.core.exceptions import BharatGateError
from bharat_gate.core.schemas import HarnessRunResult

HOOK_MARKER = "# BHARAT-GATE-MANAGED-HOOK"


def is_git_repository(repo_root: Path) -> bool:
    """Checks whether the specified directory is inside a valid git working tree."""
    resolved = repo_root.resolve()
    if (resolved / ".git").exists():
        return True
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=resolved,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except Exception:
        return False


def get_git_diff(
    repo_root: Path,
    staged: bool = False,
    target_ref: str | None = None,
) -> str:
    """Extracts git unified diff from the repository."""
    resolved = repo_root.resolve()

    if staged:
        cmd = ["git", "diff", "--staged", "--unified=3"]
        proc = subprocess.run(cmd, cwd=resolved, capture_output=True, text=True, errors="replace")
        return proc.stdout

    if target_ref:
        cmd = ["git", "diff", target_ref, "--unified=3"]
        proc = subprocess.run(cmd, cwd=resolved, capture_output=True, text=True, errors="replace")
        return proc.stdout

    # Default: diff against HEAD
    cmd = ["git", "diff", "HEAD", "--unified=3"]
    proc = subprocess.run(cmd, cwd=resolved, capture_output=True, text=True, errors="replace")
    if proc.returncode == 0:
        return proc.stdout

    # Fallback for an unborn branch where HEAD does not exist yet
    p_unstaged = subprocess.run(
        ["git", "diff", "--unified=3"],
        cwd=resolved,
        capture_output=True,
        text=True,
        errors="replace",
    )
    p_staged = subprocess.run(
        ["git", "diff", "--staged", "--unified=3"],
        cwd=resolved,
        capture_output=True,
        text=True,
        errors="replace",
    )
    diffs = []
    if p_unstaged.stdout.strip():
        diffs.append(p_unstaged.stdout)
    if p_staged.stdout.strip():
        diffs.append(p_staged.stdout)
    return "\n".join(diffs)


def install_pre_push_hook(repo_root: Path) -> Path:
    """Installs the BharatGate pre-push git hook into .git/hooks/pre-push."""
    resolved = repo_root.resolve()
    git_dir = resolved / ".git"
    if not git_dir.exists():
        raise BharatGateError(f"Cannot install pre-push hook: '{resolved}' is not a git repository.")

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-push"
    backup_path = hooks_dir / "pre-push.bharatgate.bak"

    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8", errors="ignore")
        if HOOK_MARKER not in content:
            shutil.copy2(hook_path, backup_path)

    hook_script = f"""#!/bin/sh
{HOOK_MARKER}
# Pre-push verification gate powered by BharatGate

echo "[BharatGate] Running autonomous pre-push verification gate..."
if command -v python3 >/dev/null 2>&1; then
    python3 -m bharat_gate.cli check --ci
else
    python -m bharat_gate.cli check --ci
fi

exit $?
"""

    hook_path.write_text(hook_script, encoding="utf-8")
    try:
        hook_path.chmod(0o755)
    except Exception:
        pass

    return hook_path


def uninstall_pre_push_hook(repo_root: Path) -> bool:
    """Uninstalls the BharatGate pre-push hook and restores any backup."""
    resolved = repo_root.resolve()
    hook_path = resolved / ".git" / "hooks" / "pre-push"
    backup_path = resolved / ".git" / "hooks" / "pre-push.bharatgate.bak"

    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8", errors="ignore")
        if HOOK_MARKER in content:
            hook_path.unlink()
            if backup_path.exists():
                backup_path.rename(hook_path)
            return True

    return False


def get_hook_status(repo_root: Path) -> dict[str, Any]:
    """Returns the installation status and metadata of the BharatGate pre-push hook."""
    resolved = repo_root.resolve()
    hook_path = resolved / ".git" / "hooks" / "pre-push"
    backup_path = resolved / ".git" / "hooks" / "pre-push.bharatgate.bak"

    if not hook_path.exists():
        return {
            "installed": False,
            "hook_path": str(hook_path),
            "backup_exists": backup_path.exists(),
        }

    try:
        content = hook_path.read_text(encoding="utf-8", errors="ignore")
        installed = HOOK_MARKER in content
    except Exception:
        installed = False

    return {
        "installed": installed,
        "hook_path": str(hook_path),
        "backup_exists": backup_path.exists(),
    }


def save_last_audit_result(repo_root: Path, result: HarnessRunResult) -> Path:
    """Persists the HarnessRunResult to .bharat_gate_cache/last_audit.json."""
    resolved = repo_root.resolve()
    cache_dir = resolved / ".bharat_gate_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_file = cache_dir / "last_audit.json"
    out_file.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return out_file


def load_last_audit_result(repo_root: Path) -> HarnessRunResult | None:
    """Loads the last persisted HarnessRunResult from .bharat_gate_cache/last_audit.json."""
    resolved = repo_root.resolve()
    audit_file = resolved / ".bharat_gate_cache" / "last_audit.json"
    if not audit_file.exists():
        return None
    try:
        return HarnessRunResult.model_validate_json(audit_file.read_text(encoding="utf-8"))
    except Exception:
        return None
