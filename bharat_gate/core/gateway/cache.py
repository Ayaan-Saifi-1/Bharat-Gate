import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from pydantic import BaseModel

from bharat_gate.core.schemas import GatewayResponse


def normalize_for_hash(value: Any) -> Any:
    """Recursively normalizes Python/Pydantic structures into sorted dictionaries and primitives for hashing."""
    if isinstance(value, BaseModel):
        return normalize_for_hash(value.model_dump())
    if isinstance(value, dict):
        return {str(k): normalize_for_hash(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [normalize_for_hash(x) for x in value]
    return value


def compute_cache_key(payload: dict[str, Any]) -> str:
    """Computes a deterministic SHA-256 hash key for a request payload."""
    normalized = normalize_for_hash(payload)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class GatewayCache:
    """Thread-safe SQLite cache running in WAL mode with TTL and token usage metrics."""

    def __init__(
        self,
        db_path: str | Path = ".bharat_gate_cache/gateway.sqlite3",
        ttl_seconds: int = 86400,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS response_cache (
                    cache_key TEXT PRIMARY KEY,
                    endpoint TEXT,
                    model TEXT,
                    response_json TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    created_at REAL
                );
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_stats (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    hits INTEGER DEFAULT 0,
                    misses INTEGER DEFAULT 0,
                    tokens_saved INTEGER DEFAULT 0
                );
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO cache_stats (id, hits, misses, tokens_saved) VALUES (1, 0, 0, 0);"
            )
            self.conn.commit()

    def get(self, cache_key: str) -> GatewayResponse | None:
        """Retrieves a cached response if valid and not expired, updating stats."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT response_json, input_tokens, output_tokens, created_at FROM response_cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cursor.fetchone()
            now = time.time()
            if row is not None:
                response_json, in_tok, out_tok, created_at = row
                if (now - created_at) <= self.ttl_seconds:
                    tokens_saved = (in_tok or 0) + (out_tok or 0)
                    self.conn.execute(
                        "UPDATE cache_stats SET hits = hits + 1, tokens_saved = tokens_saved + ? WHERE id = 1",
                        (tokens_saved,),
                    )
                    self.conn.commit()
                    cached_resp = GatewayResponse.model_validate_json(response_json)
                    cached_resp.cache_hit = True
                    cached_resp.latency_ms = 0.0
                    cached_resp.retry_count = 0
                    return cached_resp

            self.conn.execute("UPDATE cache_stats SET misses = misses + 1 WHERE id = 1")
            self.conn.commit()
            return None

    def set(self, cache_key: str, response: GatewayResponse) -> None:
        """Caches a complete (non-truncated) response."""
        if response.is_truncated:
            return
        with self._lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO response_cache
                (cache_key, endpoint, model, response_json, input_tokens, output_tokens, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    response.endpoint,
                    response.model,
                    response.model_dump_json(),
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                    time.time(),
                ),
            )
            self.conn.commit()

    def get_stats(self) -> dict[str, int]:
        """Returns the cache hits, misses, and tokens saved."""
        with self._lock:
            cursor = self.conn.execute("SELECT hits, misses, tokens_saved FROM cache_stats WHERE id = 1")
            row = cursor.fetchone()
            if row:
                return {"hits": int(row[0]), "misses": int(row[1]), "tokens_saved": int(row[2])}
            return {"hits": 0, "misses": 0, "tokens_saved": 0}

    def close(self) -> None:
        """Closes the underlying database connection."""
        with self._lock:
            self.conn.close()

    def __enter__(self) -> "GatewayCache":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
