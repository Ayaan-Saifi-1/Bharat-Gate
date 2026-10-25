"""Harness and orchestrator module for BharatGate."""

from bharat_gate.core.harness.orchestrator import BharatGateOrchestrator
from bharat_gate.core.harness.patch_builder import (
    format_finding_as_unified_diff,
    synthesize_regression_pytest,
    verify_snippet_replacement,
)
from bharat_gate.core.harness.prompts import (
    INVESTIGATION_SYSTEM_PROMPT,
    JSON_REPAIR_SYSTEM_PROMPT,
    TRIAGE_SYSTEM_PROMPT,
)

__all__ = [
    "BharatGateOrchestrator",
    "verify_snippet_replacement",
    "format_finding_as_unified_diff",
    "synthesize_regression_pytest",
    "TRIAGE_SYSTEM_PROMPT",
    "INVESTIGATION_SYSTEM_PROMPT",
    "JSON_REPAIR_SYSTEM_PROMPT",
]
