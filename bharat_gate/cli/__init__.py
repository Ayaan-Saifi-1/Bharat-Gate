"""BharatGate Command Line Interface (CLI) module."""

from bharat_gate.cli.doctor import DoctorCheckItem, DoctorReport, run_doctor_diagnostics
from bharat_gate.cli.git_ops import (
    HOOK_MARKER,
    get_git_diff,
    get_hook_status,
    install_pre_push_hook,
    is_git_repository,
    load_last_audit_result,
    save_last_audit_result,
    uninstall_pre_push_hook,
)

__all__ = [
    "HOOK_MARKER",
    "DoctorCheckItem",
    "DoctorReport",
    "get_git_diff",
    "get_hook_status",
    "install_pre_push_hook",
    "is_git_repository",
    "load_last_audit_result",
    "run_doctor_diagnostics",
    "save_last_audit_result",
    "uninstall_pre_push_hook",
]
