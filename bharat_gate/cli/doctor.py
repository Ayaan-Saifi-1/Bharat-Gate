"""Diagnostic doctor engine for checking environment, runtime, cache, and API connectivity."""

import importlib
import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from bharat_gate.cli.git_ops import is_git_repository
from bharat_gate.core.gateway.cache import GatewayCache
from bharat_gate.core.gateway.client import BharatCodeClient
from bharat_gate.core.gateway.telemetry import TelemetryLedger
from bharat_gate.core.schemas import HealthCheckResult


class DoctorCheckItem(BaseModel):
    """Represents the outcome of a single diagnostic check."""

    model_config = ConfigDict(extra="forbid")
    name: str
    passed: bool
    detail: str
    fix_hint: str = ""


class DoctorReport(BaseModel):
    """Represents the complete 6-point diagnostic report."""

    model_config = ConfigDict(extra="forbid")
    all_passed: bool
    local_passed: bool
    checks: list[DoctorCheckItem] = Field(default_factory=list)
    live_ping: HealthCheckResult | None = None


def run_doctor_diagnostics(
    repo_root: Path,
    skip_live_llm: bool = False,
    client: BharatCodeClient | None = None,
) -> DoctorReport:
    """Executes the 6-point system diagnostic check suite."""
    load_dotenv(dotenv_path=repo_root / ".env")
    load_dotenv()  # Fallback to current working directory

    checks: list[DoctorCheckItem] = []
    resolved_root = repo_root.resolve()

    # 1. Python Runtime (>= 3.11)
    py_ok = sys.version_info >= (3, 11)
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(
        DoctorCheckItem(
            name="Python Runtime (>= 3.11)",
            passed=py_ok,
            detail=f"Python {py_ver} on {sys.platform}",
            fix_hint="" if py_ok else "Install Python 3.11 or higher.",
        )
    )

    # 2. Core Dependencies
    required_modules = ["pydantic", "httpx", "flask", "rich"]
    missing_modules: list[str] = []
    for mod in required_modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing_modules.append(mod)

    deps_ok = len(missing_modules) == 0
    checks.append(
        DoctorCheckItem(
            name="Core Dependencies",
            passed=deps_ok,
            detail=(
                "pydantic, httpx, flask, rich available"
                if deps_ok
                else f"Missing modules: {', '.join(missing_modules)}"
            ),
            fix_hint="" if deps_ok else "Run: pip install -r requirements.txt",
        )
    )

    # 3. Git Workspace
    git_ok = is_git_repository(resolved_root)
    checks.append(
        DoctorCheckItem(
            name="Git Workspace",
            passed=git_ok,
            detail=(
                f"Valid git repository detected at {resolved_root}"
                if git_ok
                else f"Not a git repository: {resolved_root}"
            ),
            fix_hint="" if git_ok else "Run 'git init' to initialize a git repository.",
        )
    )

    # 4. SQLite WAL Cache & Telemetry DB
    cache_db_path = resolved_root / ".bharat_gate_cache" / "gateway.sqlite3"
    cache_ok = False
    cache_detail = ""
    cache_hint = ""
    try:
        cache_db_path.parent.mkdir(parents=True, exist_ok=True)
        cache = GatewayCache(cache_db_path)
        ledger = TelemetryLedger(cache_db_path)
        stats = cache.get_stats()
        summary = ledger.get_summary()
        cache.close()
        ledger.close()
        cache_ok = True
        hits = stats.get("hits", 0)
        tokens_saved = stats.get("tokens_saved", 0)
        cache_detail = (
            f"WAL-mode SQLite active (hits={hits}, tokens_saved={tokens_saved}, "
            f"{summary.total_requests} telemetry requests)"
        )
    except Exception as e:
        cache_ok = False
        cache_detail = f"SQLite cache initialization failed: {e}"
        cache_hint = "Ensure directory permissions allow write access to .bharat_gate_cache"

    checks.append(
        DoctorCheckItem(
            name="SQLite WAL Cache & Telemetry DB",
            passed=cache_ok,
            detail=cache_detail,
            fix_hint=cache_hint,
        )
    )

    # 5. BharatCode API Key Config
    api_key = os.getenv("BHARATCODE_API_KEY", "").strip()
    key_configured = bool(api_key and api_key != "your_bharatcode_api_key_here")
    checks.append(
        DoctorCheckItem(
            name="BharatCode API Key Config",
            passed=key_configured,
            detail=(
                f"Configured ({api_key[:7]}...)"
                if key_configured
                else "Missing or placeholder in .env"
            ),
            fix_hint="" if key_configured else "Set BHARATCODE_API_KEY in .env.",
        )
    )

    # 6. BharatCode /v1/responses Handshake
    live_ping: HealthCheckResult | None = None
    if skip_live_llm:
        checks.append(
            DoctorCheckItem(
                name="BharatCode /v1/responses Handshake",
                passed=True,
                detail="Skipped via --no-llm flag",
                fix_hint="",
            )
        )
    elif not key_configured:
        checks.append(
            DoctorCheckItem(
                name="BharatCode /v1/responses Handshake",
                passed=False,
                detail="Skipped because BHARATCODE_API_KEY is not configured",
                fix_hint="Set BHARATCODE_API_KEY in .env",
            )
        )
    else:
        created_client = False
        if client is None:
            c = BharatCodeClient(api_key=api_key)
            created_client = True
        else:
            c = client

        try:
            live_ping = c.ping_health_check()
            checks.append(
                DoctorCheckItem(
                    name="BharatCode /v1/responses Handshake",
                    passed=live_ping.ok,
                    detail=(
                        f"HTTP {live_ping.http_status} · model={live_ping.model} · "
                        f"latency={live_ping.latency_ms:.0f}ms · x-request-id={live_ping.request_id}"
                    ),
                    fix_hint="" if live_ping.ok else (live_ping.error or "Check BharatCode service status"),
                )
            )
        except Exception as e:
            checks.append(
                DoctorCheckItem(
                    name="BharatCode /v1/responses Handshake",
                    passed=False,
                    detail=f"Handshake failed: {e}",
                    fix_hint="Verify internet connection and BharatCode endpoint status",
                )
            )
        finally:
            if created_client:
                c.close()

    local_passed = all(c.passed for c in checks[:4])
    all_passed = all(c.passed for c in checks)

    return DoctorReport(
        all_passed=all_passed,
        local_passed=local_passed,
        checks=checks,
        live_ping=live_ping,
    )
