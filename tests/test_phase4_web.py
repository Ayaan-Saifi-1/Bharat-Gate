"""Phase 4 Web Studio & Hackathon Submission Test Suite.

Contains 12 deterministic unit tests verifying:
- Web Studio UI HTML template rendering
- REST endpoints (/api/scenarios, /api/doctor, /api/telemetry, /api/audit)
- SSE streaming endpoint with keepalive heartbeats (/api/audit/stream)
- Mode switching (replay, offline_mock, live with injected mock client)
- Error handling and validation (empty diff 400)
- BharatCode.txt submission specification compliance
- README.md completeness and zero-placeholder check
- ARCHITECTURE_AND_INTERVIEW_GUIDE.md module coverage
"""

import json
from pathlib import Path
import tempfile

import httpx
import pytest

from bharat_gate.core.gateway.client import BharatCodeClient
from bharat_gate.web.app import create_app


@pytest.fixture
def test_client(tmp_path: Path):
    """Creates a test client for the BharatGate Flask Web Studio."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env").write_text("BHARATCODE_API_KEY=bc_test_key_valid\n", encoding="utf-8")
    app = create_app(repo_root=tmp_path)
    app.config["TESTING"] = True
    return app.test_client()


def test_web_index_renders_studio_html(test_client):
    """Verifies GET / returns HTTP 200 and includes BharatGate, qwen-3.8-27b, and all 4 tab headers."""
    res = test_client.get("/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    assert "BharatGate" in html
    assert "qwen-3.8-27b" in html
    assert "Findings &amp; Unified Patches" in html or "Findings & Unified Patches" in html
    assert "AST Blast-Radius Graph" in html
    assert "Stateless Tool Trace" in html or "Stateless /v1/responses Trace" in html
    assert "Synthesized pytest" in html


def test_api_scenarios_returns_all_three_prebaked(test_client):
    """Verifies GET /api/scenarios returns HTTP 200 with 3 valid scenarios."""
    res = test_client.get("/api/scenarios")
    assert res.status_code == 200
    data = res.get_json()

    assert "scenarios" in data
    scenarios = data["scenarios"]
    assert len(scenarios) == 3

    ids = [s["id"] for s in scenarios]
    assert "sse-gpu-leak" in ids
    assert "webhook-paise-bug" in ids
    assert "auth-sqli-secret" in ids


def test_api_doctor_endpoint_returns_6_checks(test_client):
    """Verifies GET /api/doctor returns HTTP 200 with local_passed is True and 6 checks."""
    res = test_client.get("/api/doctor")
    assert res.status_code == 200
    data = res.get_json()

    assert data.get("local_passed") is True
    assert "checks" in data
    assert len(data["checks"]) == 6


def test_api_telemetry_returns_summary_and_hook_status(test_client):
    """Verifies GET /api/telemetry returns telemetry, hook, model, and endpoint."""
    res = test_client.get("/api/telemetry")
    assert res.status_code == 200
    data = res.get_json()

    assert "telemetry" in data
    assert "hook" in data
    assert "model" in data
    assert "endpoint" in data
    assert data["model"] == "qwen-3.8-27b"
    assert "/v1/responses" in data["endpoint"]


def test_api_audit_replay_mode_returns_instant_verdict(test_client):
    """Verifies POST /api/audit with scenario_id: 2 and mode: replay returns verdict=BLOCK and BREAKING_CALLER findings."""
    res = test_client.post(
        "/api/audit",
        json={"scenario_id": "2", "mode": "replay"},
    )
    assert res.status_code == 200
    data = res.get_json()

    assert "verdict" in data
    assert data["verdict"]["verdict"] == "BLOCK"
    categories = [f["category"] for f in data["verdict"]["findings"]]
    assert "BREAKING_CALLER" in categories
    assert data["verdict"]["risk_score"] > 80


def test_api_audit_offline_mock_mode_redacts_secrets_and_blocks(test_client):
    """Posts a custom diff containing a leaked BHARATCODE_API_KEY with mode=offline_mock and asserts secret_scan and BLOCK."""
    leaked_diff = """--- a/config.py
+++ b/config.py
@@ -1,2 +1,3 @@
+BHARATCODE_API_KEY = "bc_live_leak_test_secret_123456789"
 DEBUG = False
"""
    res = test_client.post(
        "/api/audit",
        json={"diff_text": leaked_diff, "mode": "offline_mock"},
    )
    assert res.status_code == 200
    data = res.get_json()

    assert data["secret_scan"]["has_secrets"] is True
    assert len(data["secret_scan"]["findings"]) > 0
    assert data["verdict"]["verdict"] == "BLOCK"
    finding_titles = [f["title"] for f in data["verdict"]["findings"]]
    assert any("Secret" in t or "Credential" in t for t in finding_titles)


def test_api_audit_empty_diff_returns_400(test_client):
    """Verifies POST /api/audit with mode=offline_mock and empty diff_text returns HTTP 400."""
    res = test_client.post(
        "/api/audit",
        json={"mode": "offline_mock", "diff_text": "   "},
    )
    assert res.status_code == 400
    data = res.get_json()
    assert "diff_text is required" in data["error"]


def test_api_audit_live_mode_with_injected_mock_client(tmp_path: Path):
    """Injects a mock BharatCodeClient into create_app(client=mock_client) and verifies live mode."""
    turn = 0

    def mock_transport_handler(req: httpx.Request) -> httpx.Response:
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
                            "content": "<think>Scanning callers.</think>Analyzing.",
                        },
                        {
                            "type": "function_call",
                            "call_id": "call_phase4_live_1",
                            "name": "find_symbol_callers",
                            "arguments": json.dumps({"symbol_name": "capture_payment"}),
                        },
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
                },
                headers={"x-request-id": "req-phase4-live-1"},
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
                                "risk_score": 88,
                                "summary": "Breaking payment signature change detected.",
                                "findings": [
                                    {
                                        "id": "LIVE-MOCK-01",
                                        "severity": "HIGH",
                                        "category": "BREAKING_CALLER",
                                        "file_path": "processor.py",
                                        "start_line": 1,
                                        "end_line": 5,
                                        "title": "Breaking payment signature",
                                        "explanation": "Callers not updated for currency units.",
                                        "suggested_patch": "--- a/processor.py\n+++ b/processor.py\n@@ -1,1 +1,1 @@\n-def old():\n+def new():\n",
                                    }
                                ],
                            }
                        ),
                    }
                ],
                "usage": {"prompt_tokens": 180, "completion_tokens": 70, "total_tokens": 250},
            },
            headers={"x-request-id": "req-phase4-live-2"},
        )

    mock_client = BharatCodeClient(
        api_key="mock-key-live",
        cache_db_path=tmp_path / "cache.sqlite3",
        transport=httpx.MockTransport(mock_transport_handler),
    )

    app = create_app(repo_root=tmp_path, client=mock_client)
    app.config["TESTING"] = True
    c = app.test_client()

    diff_content = """--- a/processor.py
+++ b/processor.py
@@ -1,2 +1,2 @@
-def capture_payment(order_id, amount_paise):
+def capture_payment(order_id, amount_rupees):
"""

    res = c.post(
        "/api/audit",
        json={"mode": "live", "diff_text": diff_content},
    )
    assert res.status_code == 200
    data = res.get_json()

    assert "request_ids" in data
    assert len(data["request_ids"]) > 0
    assert "req-phase4-live" in data["request_ids"][0]
    assert data["verdict"]["verdict"] == "BLOCK"


def test_api_audit_sse_stream_emits_keepalive_and_stages(test_client):
    """Verifies POST /api/audit/stream returns text/event-stream and contains keepalives and stages."""
    res = test_client.post(
        "/api/audit/stream",
        json={"scenario_id": "1", "mode": "replay"},
    )
    assert res.status_code == 200
    assert "text/event-stream" in res.mimetype

    stream_content = res.get_data(as_text=True)
    assert ": keepalive" in stream_content
    assert "STAGE_0_PREPASS" in stream_content
    assert "STAGE_1_HARNESS" in stream_content
    assert '"type": "complete"' in stream_content


def test_bharatcode_txt_root_file_compliance():
    """Verifies BharatCode.txt exists in the repo root, is non-empty, and documents core requirements."""
    bc_file = Path("BharatCode.txt")
    assert bc_file.exists(), "BharatCode.txt must exist in repo root"

    text = bc_file.read_text(encoding="utf-8")
    assert len(text) > 500
    assert "BharatGate" in text
    assert "qwen-3.8-27b" in text
    assert "/v1/responses" in text
    assert "store: false" in text.lower() or '"store": false' in text
    assert "x-request-id" in text
    assert "503" in text
    assert "Coding & developer tools" in text or "Coding & Developer Tools" in text


def test_readme_has_no_placeholders_and_documents_serve():
    """Verifies README.md contains official repository URL, zero placeholders, and documents serve."""
    readme_file = Path("README.md")
    assert readme_file.exists()

    text = readme_file.read_text(encoding="utf-8")
    assert "Ayaan-Saifi-1/Bharat-Gate" in text
    assert "<your-username>" not in text
    assert "manage.py serve" in text
    assert "50/50 Passing" in text


def test_interview_guide_exists_and_covers_core_modules():
    """Verifies ARCHITECTURE_AND_INTERVIEW_GUIDE.md exists and references core architectural modules."""
    guide_file = Path("ARCHITECTURE_AND_INTERVIEW_GUIDE.md")
    assert guide_file.exists()

    text = guide_file.read_text(encoding="utf-8")
    assert len(text) > 2000
    assert "client.py" in text
    assert "orchestrator.py" in text
    assert "blast_radius.py" in text
    assert "secret_scanner.py" in text
