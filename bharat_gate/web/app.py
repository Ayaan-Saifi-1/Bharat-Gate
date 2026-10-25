"""Flask application factory and REST/SSE API endpoints for BharatGate Web Studio."""

import json
import os
from pathlib import Path
import queue
import tempfile
import threading
from typing import Any, Generator

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
import httpx

from bharat_gate.cli.doctor import run_doctor_diagnostics
from bharat_gate.cli.git_ops import (
    get_hook_status,
    save_last_audit_result,
)
from bharat_gate.core.exceptions import BharatGateError
from bharat_gate.core.gateway.cache import GatewayCache
from bharat_gate.core.gateway.client import BharatCodeClient
from bharat_gate.core.gateway.telemetry import TelemetryLedger
from bharat_gate.core.graph.diff_parser import parse_unified_diff
from bharat_gate.core.graph.secret_scanner import scan_and_redact_secrets
from bharat_gate.core.harness.orchestrator import BharatGateOrchestrator
from bharat_gate.core.schemas import HarnessRunResult
from bharat_gate.data.prebaked_replays import (
    get_all_scenarios,
    get_scenario_by_id,
)


def _build_mock_client(cache_path: Path | None = None) -> BharatCodeClient:
    """Builds a deterministic mock BharatCodeClient for offline mock audits."""
    turn = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        if turn == 1:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": "<think>Inspecting symbol callers across repository.</think>Checking callers.",
                        },
                        {
                            "type": "function_call",
                            "call_id": "call_web_mock_1",
                            "name": "find_symbol_callers",
                            "arguments": json.dumps({"symbol_name": "capture_payment"}),
                        },
                    ],
                    "usage": {"prompt_tokens": 140, "completion_tokens": 50, "total_tokens": 190},
                },
                headers={"x-request-id": "req-web-mock-1"},
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": json.dumps(
                            {
                                "verdict": "BLOCK",
                                "risk_score": 90,
                                "summary": "High risk breaking change detected in modified interface signatures.",
                                "findings": [
                                    {
                                        "id": "WEB-MOCK-01",
                                        "severity": "HIGH",
                                        "category": "BREAKING_CALLER",
                                        "file_path": "service.py",
                                        "start_line": 1,
                                        "end_line": 5,
                                        "title": "Breaking interface modification",
                                        "explanation": "Function signature changed without updating callers.",
                                        "suggested_patch": "--- a/service.py\n+++ b/service.py\n@@ -1,1 +1,1 @@\n-def old():\n+def new():\n",
                                    }
                                ],
                            }
                        ),
                    }
                ],
                "usage": {"prompt_tokens": 220, "completion_tokens": 90, "total_tokens": 310},
            },
            headers={"x-request-id": "req-web-mock-2"},
        )

    return BharatCodeClient(
        api_key="mock-web-key",
        cache_db_path=cache_path,
        transport=httpx.MockTransport(handler),
    )


def create_app(
    repo_root: Path | None = None,
    client: BharatCodeClient | None = None,
) -> Flask:
    """Creates and configures the Flask application instance."""
    target_root = (repo_root or Path.cwd()).resolve()
    load_dotenv(dotenv_path=target_root / ".env")
    load_dotenv()

    template_dir = Path(__file__).resolve().parent / "templates"
    static_dir = Path(__file__).resolve().parent / "static"

    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir) if static_dir.exists() else None,
    )

    # ------------------------------------------------------------------------
    # 1. GET / — Studio Dashboard
    # ------------------------------------------------------------------------
    @app.route("/", methods=["GET"])
    def index() -> Any:
        try:
            return render_template("studio.html")
        except Exception:
            return (
                "<!DOCTYPE html><html><head><title>BharatGate Web Studio</title></head>"
                "<body style='font-family: sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem;'>"
                "<h1>BharatGate Web Studio</h1>"
                "<p>Studio UI template loading. Access the JSON API at <code>/api/scenarios</code>, "
                "<code>/api/doctor</code>, or <code>/api/telemetry</code>.</p>"
                "</body></html>"
            )

    # ------------------------------------------------------------------------
    # 2. GET /api/scenarios — Prebaked Replay Scenarios
    # ------------------------------------------------------------------------
    @app.route("/api/scenarios", methods=["GET"])
    def get_scenarios() -> Any:
        scenarios = get_all_scenarios()
        return jsonify({"scenarios": [s.model_dump() for s in scenarios]}), 200

    # ------------------------------------------------------------------------
    # 3. GET /api/doctor — 6-Point System Diagnostics
    # ------------------------------------------------------------------------
    @app.route("/api/doctor", methods=["GET"])
    def get_doctor() -> Any:
        is_live = request.args.get("live", "0") == "1"
        rep = run_doctor_diagnostics(
            repo_root=target_root,
            skip_live_llm=not is_live,
            client=client,
        )
        return jsonify(rep.model_dump()), 200

    # ------------------------------------------------------------------------
    # 4. GET /api/telemetry — Shared GPU & Gateway Cache Metrics
    # ------------------------------------------------------------------------
    @app.route("/api/telemetry", methods=["GET"])
    def get_telemetry() -> Any:
        cache_db_path = target_root / ".bharat_gate_cache" / "gateway.sqlite3"
        cache_db_path.parent.mkdir(parents=True, exist_ok=True)

        cache = GatewayCache(cache_db_path)
        ledger = TelemetryLedger(cache_db_path)
        try:
            stats = cache.get_stats()
            summary = ledger.get_summary(cache_stats=stats)
        finally:
            cache.close()
            ledger.close()

        hook_status = get_hook_status(target_root)
        model_name = os.getenv("BHARATCODE_MODEL", "qwen-3.8-27b")

        return (
            jsonify(
                {
                    "telemetry": summary.model_dump(),
                    "hook": hook_status,
                    "model": model_name,
                    "endpoint": "/api/model/v1/responses (store: false)",
                }
            ),
            200,
        )

    # ------------------------------------------------------------------------
    # 5. POST /api/audit — Synchronous Audit Execution
    # ------------------------------------------------------------------------
    @app.route("/api/audit", methods=["POST"])
    def run_audit_endpoint() -> Any:
        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "replay")
        scenario_id = data.get("scenario_id")
        diff_text = data.get("diff_text", "")

        # Mode 1: Replay (0ms)
        if mode == "replay":
            scenario = get_scenario_by_id(scenario_id or "sse-gpu-leak")
            save_last_audit_result(target_root, scenario.recorded_result)
            return jsonify(scenario.recorded_result.model_dump()), 200

        # Mode 2: Offline Mock
        if mode == "offline_mock":
            if not diff_text.strip():
                return jsonify({"error": "diff_text is required for offline_mock mode"}), 400

            cache_path = target_root / ".bharat_gate_cache" / "gateway.sqlite3"
            active_client = client or _build_mock_client(cache_path)
            created = client is None

            try:
                orch = BharatGateOrchestrator(client=active_client, repo_root=target_root)
                res = orch.run_audit(diff_text)
                save_last_audit_result(target_root, res)
                return jsonify(res.model_dump()), 200
            finally:
                if created:
                    active_client.close()

        # Mode 3: Live
        if mode == "live":
            if not diff_text.strip():
                return jsonify({"error": "diff_text is required for live audit mode"}), 400

            api_key = os.getenv("BHARATCODE_API_KEY", "").strip()
            if not api_key or api_key == "your_bharatcode_api_key_here":
                if client is None:
                    return (
                        jsonify(
                            {
                                "error": "BHARATCODE_API_KEY is not configured in .env",
                                "request_id": None,
                            }
                        ),
                        400,
                    )

            # Check if we should materialize scenario sample files for sandbox context
            temp_dir_ctx = None
            sandbox_dir = target_root
            if scenario_id:
                try:
                    sc = get_scenario_by_id(scenario_id)
                    temp_dir_ctx = tempfile.TemporaryDirectory()
                    td_path = Path(temp_dir_ctx.name)
                    for rel_p, code in sc.sample_files.items():
                        f = td_path / rel_p
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_text(code, encoding="utf-8")
                    sandbox_dir = td_path
                except Exception:
                    pass

            active_client = client or BharatCodeClient(
                api_key=api_key,
                model="qwen-3.8-27b",
                cache_db_path=target_root / ".bharat_gate_cache" / "gateway.sqlite3",
            )
            created = client is None

            try:
                orch = BharatGateOrchestrator(client=active_client, repo_root=sandbox_dir)
                res = orch.run_audit(diff_text)
                save_last_audit_result(target_root, res)
                return jsonify(res.model_dump()), 200
            except BharatGateError as bge:
                req_id = getattr(bge, "request_id", None)
                return jsonify({"error": str(bge), "request_id": req_id}), 502
            except Exception as e:
                return jsonify({"error": str(e), "request_id": None}), 500
            finally:
                if created:
                    active_client.close()
                if temp_dir_ctx is not None:
                    try:
                        temp_dir_ctx.cleanup()
                    except Exception:
                        pass

        return jsonify({"error": f"Unknown mode: {mode}"}), 400

    # ------------------------------------------------------------------------
    # 6. POST /api/audit/stream — SSE Streaming Audit with Heartbeat
    # ------------------------------------------------------------------------
    @app.route("/api/audit/stream", methods=["POST"])
    def stream_audit_endpoint() -> Response:
        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "replay")
        scenario_id = data.get("scenario_id")
        diff_text = data.get("diff_text", "")

        def generate_sse() -> Generator[str, None, None]:
            # Initial heartbeat
            yield ": keepalive\n\n"

            # Stage 0 Pre-Pass Announcement
            stage_0_announcement = {
                "type": "stage",
                "stage": "STAGE_0_PREPASS",
                "message": "Running Stage 0 local secret redaction & Python AST blast-radius scan...",
            }
            yield f"data: {json.dumps(stage_0_announcement)}\n\n"

            # Execute Stage 0 locally
            raw_diff = diff_text
            if not raw_diff and scenario_id:
                try:
                    sc = get_scenario_by_id(scenario_id)
                    raw_diff = sc.raw_diff
                except Exception:
                    pass

            scan = scan_and_redact_secrets(raw_diff)
            parsed = parse_unified_diff(scan.sanitized_diff_text)

            prepass_info = {
                "type": "prepass_complete",
                "files_count": len(parsed.files),
                "secrets_redacted": len(scan.findings),
            }
            yield f"data: {json.dumps(prepass_info)}\n\n"
            yield ": keepalive\n\n"

            # Stage 1 Harness Announcement
            stage_1_announcement = {
                "type": "stage",
                "stage": "STAGE_1_HARNESS",
                "message": "Executing stateless /v1/responses (store: false) verification loop...",
            }
            yield f"data: {json.dumps(stage_1_announcement)}\n\n"

            # Execution Logic
            if mode == "replay":
                sc = get_scenario_by_id(scenario_id or "sse-gpu-leak")
                res = sc.recorded_result
                save_last_audit_result(target_root, res)
                yield f"data: {json.dumps({'type': 'complete', 'result': res.model_dump()})}\n\n"
                return

            if mode == "offline_mock":
                if not raw_diff.strip():
                    yield f"data: {json.dumps({'type': 'error', 'message': 'diff_text is required'})}\n\n"
                    return
                cache_path = target_root / ".bharat_gate_cache" / "gateway.sqlite3"
                active_client = client or _build_mock_client(cache_path)
                created = client is None
                try:
                    orch = BharatGateOrchestrator(client=active_client, repo_root=target_root)
                    res = orch.run_audit(raw_diff)
                    save_last_audit_result(target_root, res)
                    yield f"data: {json.dumps({'type': 'complete', 'result': res.model_dump()})}\n\n"
                finally:
                    if created:
                        active_client.close()
                return

            if mode == "live":
                if not raw_diff.strip():
                    yield f"data: {json.dumps({'type': 'error', 'message': 'diff_text is required'})}\n\n"
                    return

                api_key = os.getenv("BHARATCODE_API_KEY", "").strip()
                if not api_key or api_key == "your_bharatcode_api_key_here":
                    if client is None:
                        yield f"data: {json.dumps({'type': 'error', 'message': 'BHARATCODE_API_KEY is not configured in .env'})}\n\n"
                        return

                temp_dir_ctx = None
                sandbox_dir = target_root
                if scenario_id:
                    try:
                        sc = get_scenario_by_id(scenario_id)
                        temp_dir_ctx = tempfile.TemporaryDirectory()
                        td_path = Path(temp_dir_ctx.name)
                        for rel_p, code in sc.sample_files.items():
                            f = td_path / rel_p
                            f.parent.mkdir(parents=True, exist_ok=True)
                            f.write_text(code, encoding="utf-8")
                        sandbox_dir = td_path
                    except Exception:
                        pass

                active_client = client or BharatCodeClient(
                    api_key=api_key,
                    model="qwen-3.8-27b",
                    cache_db_path=target_root / ".bharat_gate_cache" / "gateway.sqlite3",
                )
                created = client is None

                res_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

                def audit_worker() -> None:
                    try:
                        orch = BharatGateOrchestrator(client=active_client, repo_root=sandbox_dir)
                        result_obj = orch.run_audit(raw_diff)
                        res_queue.put(("result", result_obj))
                    except Exception as exc:
                        res_queue.put(("error", exc))

                worker_thread = threading.Thread(target=audit_worker, daemon=True)
                worker_thread.start()

                # Stream keepalives while worker thread runs
                while worker_thread.is_alive():
                    try:
                        msg_type, val = res_queue.get(timeout=2.0)
                        if msg_type == "result":
                            save_last_audit_result(target_root, val)
                            yield f"data: {json.dumps({'type': 'complete', 'result': val.model_dump()})}\n\n"
                            return
                        else:
                            yield f"data: {json.dumps({'type': 'error', 'message': str(val)})}\n\n"
                            return
                    except queue.Empty:
                        yield ": keepalive\n\n"

                # Check queue if worker thread finished
                if not res_queue.empty():
                    msg_type, val = res_queue.get_nowait()
                    if msg_type == "result":
                        save_last_audit_result(target_root, val)
                        yield f"data: {json.dumps({'type': 'complete', 'result': val.model_dump()})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'error', 'message': str(val)})}\n\n"

                if created:
                    active_client.close()
                if temp_dir_ctx is not None:
                    try:
                        temp_dir_ctx.cleanup()
                    except Exception:
                        pass
                return

        return Response(
            stream_with_context(generate_sse()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return app
