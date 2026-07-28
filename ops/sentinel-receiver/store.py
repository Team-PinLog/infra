"""Bounded SQLite metadata, cooldown, budget and sanitized analysis cache."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from schema import validate_analysis

HOUR = 3600
DAY = 86400
CACHE_TTL = DAY
METADATA_TTL = 7 * DAY


class DeliveryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    incident_key TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
                    delivered_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS deliveries_time ON deliveries(delivered_at);
                CREATE TABLE IF NOT EXISTS analyses (
                    incident_key TEXT NOT NULL, severity TEXT NOT NULL, analyzed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS analyses_time ON analyses(analyzed_at);
                CREATE TABLE IF NOT EXISTS analysis_cache (
                    incident_key TEXT PRIMARY KEY, sanitized_analysis TEXT NOT NULL, cached_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS failures (
                    incident_key TEXT NOT NULL, stage TEXT NOT NULL, error_type TEXT NOT NULL, failed_at REAL NOT NULL
                );
            """)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def should_deliver(self, key: str, severity: str, status: str, now: float) -> bool:
        if status == "resolved":
            return True
        cooldown = HOUR if severity == "critical" else 6 * HOUR
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(delivered_at) FROM deliveries WHERE incident_key=? AND severity=? AND status=?",
                (key, severity, status),
            ).fetchone()
        return not row or row[0] is None or now - float(row[0]) >= cooldown

    def record_delivery(self, key: str, severity: str, status: str, now: float) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO deliveries VALUES(?,?,?,?)", (key, severity, status, now))

    def analysis_budget_available(self, severity: str, now: float) -> bool:
        with self._connect() as db:
            hourly, daily = db.execute(
                "SELECT SUM(analyzed_at>?), COUNT(*) FROM analyses WHERE analyzed_at>?",
                (now - HOUR, now - DAY),
            ).fetchone()
            warning_hourly, warning_daily = db.execute(
                "SELECT SUM(analyzed_at>?), COUNT(*) FROM analyses WHERE severity='warning' AND analyzed_at>?",
                (now - HOUR, now - DAY),
            ).fetchone()
        if int(hourly or 0) >= 6 or int(daily or 0) >= 30:
            return False
        if severity == "warning" and (int(warning_hourly or 0) >= 2 or int(warning_daily or 0) >= 10):
            return False
        return True

    def record_analysis(self, key: str, severity: str, now: float) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO analyses VALUES(?,?,?)", (key, severity, now))

    def reserve_analysis(self, key: str, severity: str, now: float) -> bool:
        """Atomically enforce rolling budgets and reserve one GMS call."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            hourly, daily = db.execute(
                "SELECT SUM(analyzed_at>?), COUNT(*) FROM analyses WHERE analyzed_at>?",
                (now - HOUR, now - DAY),
            ).fetchone()
            warning_hourly, warning_daily = db.execute(
                "SELECT SUM(analyzed_at>?), COUNT(*) FROM analyses WHERE severity='warning' AND analyzed_at>?",
                (now - HOUR, now - DAY),
            ).fetchone()
            allowed = int(hourly or 0) < 6 and int(daily or 0) < 30
            if severity == "warning":
                allowed = allowed and int(warning_hourly or 0) < 2 and int(warning_daily or 0) < 10
            if allowed:
                db.execute("INSERT INTO analyses VALUES(?,?,?)", (key, severity, now))
            return allowed

    def cache_analysis(self, key: str, analysis: dict, now: float) -> None:
        clean = validate_analysis(analysis)
        with self._connect() as db:
            db.execute(
                "INSERT INTO analysis_cache VALUES(?,?,?) ON CONFLICT(incident_key) DO UPDATE SET sanitized_analysis=excluded.sanitized_analysis,cached_at=excluded.cached_at",
                (key, json.dumps(clean, ensure_ascii=False, separators=(",", ":")), now),
            )

    def get_cached_analysis(self, key: str, now: float):
        with self._connect() as db:
            row = db.execute("SELECT sanitized_analysis,cached_at FROM analysis_cache WHERE incident_key=?", (key,)).fetchone()
        if not row or now - float(row[1]) > CACHE_TTL:
            return None
        try:
            return validate_analysis(json.loads(row[0]))
        except (ValueError, json.JSONDecodeError):
            return None

    def record_failure(self, key: str, stage: str, error_type: str, now: float) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO failures VALUES(?,?,?,?)", (key, stage[:32], error_type[:64], now))

    def failure_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM failures").fetchone()[0])

    def prune(self, now: float) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM analysis_cache WHERE cached_at<?", (now - CACHE_TTL,))
            db.execute("DELETE FROM deliveries WHERE delivered_at<?", (now - METADATA_TTL,))
            db.execute("DELETE FROM analyses WHERE analyzed_at<?", (now - METADATA_TTL,))
            db.execute("DELETE FROM failures WHERE failed_at<?", (now - METADATA_TTL,))
