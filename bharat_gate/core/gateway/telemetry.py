from pathlib import Path
import sqlite3
import threading
from typing import Any

from bharat_gate.core.schemas import TelemetryRecord, TelemetrySummary


class TelemetryLedger:
    """Thread-safe SQLite ledger running in WAL mode to record and summarize gateway telemetry."""

    def __init__(self, db_path: str | Path = ".bharat_gate_cache/gateway.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT,
                    endpoint TEXT,
                    model TEXT,
                    status TEXT,
                    http_status INTEGER,
                    latency_ms REAL,
                    retry_count INTEGER,
                    cache_hit INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    error_message TEXT,
                    timestamp TEXT
                );
                """
            )
            self.conn.commit()

    def record(self, entry: TelemetryRecord) -> None:
        """Safely inserts a telemetry record into SQLite."""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO telemetry_logs (
                    request_id, endpoint, model, status, http_status, latency_ms,
                    retry_count, cache_hit, input_tokens, output_tokens, error_message, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.request_id,
                    entry.endpoint,
                    entry.model,
                    entry.status,
                    entry.http_status,
                    entry.latency_ms,
                    entry.retry_count,
                    1 if entry.cache_hit else 0,
                    entry.input_tokens,
                    entry.output_tokens,
                    entry.error_message,
                    entry.timestamp,
                ),
            )
            self.conn.commit()

    def get_summary(self, cache_stats: dict[str, int] | None = None) -> TelemetrySummary:
        """Aggregates telemetry records and combines them with cache statistics."""
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT 
                    COUNT(*),
                    SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END),
                    SUM(retry_count),
                    AVG(CASE WHEN cache_hit = 0 THEN latency_ms ELSE NULL END),
                    SUM(input_tokens),
                    SUM(output_tokens)
                FROM telemetry_logs
                """
            )
            row = cursor.fetchone()
            total_requests = int(row[0] or 0)
            logged_cache_hits = int(row[1] or 0)
            total_retries = int(row[2] or 0)
            avg_latency_ms = round(float(row[3] or 0.0), 2)
            total_input_tokens = int(row[4] or 0)
            total_output_tokens = int(row[5] or 0)

            if cache_stats is not None:
                cache_hits = int(cache_stats.get("hits", 0))
                cache_misses = int(cache_stats.get("misses", 0))
                estimated_tokens_saved = int(cache_stats.get("tokens_saved", 0))
            else:
                cache_hits = logged_cache_hits
                cache_misses = max(0, total_requests - cache_hits)
                estimated_tokens_saved = 0

            total_cache_events = cache_hits + cache_misses
            hit_rate_pct = (
                round((cache_hits / total_cache_events * 100.0), 2)
                if total_cache_events > 0
                else 0.0
            )

            id_cursor = self.conn.execute(
                """
                SELECT request_id FROM telemetry_logs
                WHERE request_id IS NOT NULL AND request_id != ''
                ORDER BY id DESC
                LIMIT 20
                """
            )
            recent_ids = [r[0] for r in id_cursor.fetchall()]

            return TelemetrySummary(
                total_requests=total_requests,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                cache_hit_rate_pct=hit_rate_pct,
                total_retries=total_retries,
                avg_latency_ms=avg_latency_ms,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                estimated_tokens_saved=estimated_tokens_saved,
                recent_request_ids=recent_ids,
            )

    def close(self) -> None:
        """Closes the underlying database connection."""
        with self._lock:
            self.conn.close()

    def __enter__(self) -> "TelemetryLedger":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
