"""Bounded SQLite metadata, cooldown, budget and sanitized analysis cache."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from schema import ANALYSIS_FIELDS, validate_analysis
from security import redact_text

HOUR = 3600
DAY = 86400
CACHE_TTL = DAY
METADATA_TTL = 7 * DAY
_CACHE_FIELD_LIMIT = 512
_CACHE_SENSITIVE = re.compile(r"(?i)\b(?:credential|token|private|internal|prompt)\b")
_CURRENT_DELIVERIES = (
    ("incident_key", "TEXT", 1, 0), ("severity", "TEXT", 1, 0),
    ("status", "TEXT", 1, 0), ("delivered_at", "REAL", 1, 0),
)
_LEGACY_DELIVERIES = (
    ("dedupe_key", "TEXT", 0, 1), ("content_hash", "TEXT", 1, 0),
    ("last_success", "REAL", 1, 0),
)
_LEGACY_DEAD_LETTERS = (
    ("id", "INTEGER", 0, 1), ("dedupe_key", "TEXT", 1, 0),
    ("content_hash", "TEXT", 1, 0), ("failed_at", "REAL", 1, 0),
    ("stage", "TEXT", 1, 0), ("error_type", "TEXT", 1, 0),
)


def legacy_delivery_identity(payload: dict) -> tuple[str, str]:
    """Return the exact key and full-payload hash used by the legacy receiver."""
    fingerprints = sorted(
        str(alert.get("fingerprint", ""))
        for alert in payload.get("alerts", [])
        if isinstance(alert, dict)
    )
    key_data = {
        "groupKey": str(payload.get("groupKey", "")),
        "status": str(payload.get("status", "")),
        "fingerprints": fingerprints,
    }
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical(key_data)).hexdigest(), hashlib.sha256(canonical(payload)).hexdigest()


def _schema(db: sqlite3.Connection, table: str):
    return tuple((row[1], row[2].upper(), row[3], row[5]) for row in db.execute(f"PRAGMA table_info({table})"))


def _exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _sanitize_cached_analysis(analysis: dict) -> dict[str, str]:
    validated = validate_analysis(analysis)
    clean = {}
    for field in ANALYSIS_FIELDS:
        value = redact_text(validated[field], _CACHE_FIELD_LIMIT)
        value = "".join(character for character in value if character.isprintable()).strip()
        clean[field] = "[REDACTED]" if _CACHE_SENSITIVE.search(value) else value
    return validate_analysis(clean)


class DeliveryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                if _exists(db, "deliveries"):
                    schema = _schema(db, "deliveries")
                    if schema == _LEGACY_DELIVERIES:
                        if _exists(db, "legacy_deliveries"):
                            raise RuntimeError("conflicting legacy deliveries schema")
                        db.execute("ALTER TABLE deliveries RENAME TO legacy_deliveries")
                        db.execute("CREATE TABLE deliveries (incident_key TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, delivered_at REAL NOT NULL)")
                    elif schema != _CURRENT_DELIVERIES:
                        raise RuntimeError("unrecognized deliveries schema")
                else:
                    if _exists(db, "legacy_deliveries"):
                        raise RuntimeError("partial deliveries migration")
                    db.execute("CREATE TABLE deliveries (incident_key TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, delivered_at REAL NOT NULL)")
                if _exists(db, "legacy_deliveries") and _schema(db, "legacy_deliveries") != _LEGACY_DELIVERIES:
                    raise RuntimeError("unrecognized legacy deliveries schema")
                db.execute("CREATE TABLE IF NOT EXISTS legacy_deliveries (dedupe_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, last_success REAL NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS deliveries_time ON deliveries(delivered_at)")
                db.execute("CREATE TABLE IF NOT EXISTS analyses (incident_key TEXT NOT NULL, severity TEXT NOT NULL, analyzed_at REAL NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS analyses_time ON analyses(analyzed_at)")
                db.execute("CREATE TABLE IF NOT EXISTS analysis_cache (incident_key TEXT PRIMARY KEY, sanitized_analysis TEXT NOT NULL, cached_at REAL NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS failures (incident_key TEXT NOT NULL, stage TEXT NOT NULL, error_type TEXT NOT NULL, failed_at REAL NOT NULL)")
                if _exists(db, "dead_letters"):
                    if _schema(db, "dead_letters") != _LEGACY_DEAD_LETTERS:
                        raise RuntimeError("unrecognized dead_letters schema")
                    db.execute(
                        "INSERT INTO failures(incident_key,stage,error_type,failed_at) "
                        "SELECT dedupe_key,stage,error_type,failed_at FROM dead_letters"
                    )
                    db.execute("DROP TABLE dead_letters")
                db.commit()
            except Exception as exc:
                db.rollback()
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError("delivery store migration failed") from None

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def should_deliver(self, key: str, severity: str, status: str, now: float,
                       legacy_key: str | None = None, content_hash: str | None = None) -> bool:
        if status == "resolved":
            return True
        cooldown = HOUR if severity == "critical" else 6 * HOUR
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(delivered_at) FROM deliveries WHERE incident_key=? AND severity=? AND status=?",
                (key, severity, status),
            ).fetchone()
            if row and row[0] is not None:
                return now - float(row[0]) >= cooldown
            legacy = None
            if legacy_key is not None and content_hash is not None:
                legacy = db.execute(
                    "SELECT content_hash,last_success FROM legacy_deliveries WHERE dedupe_key=?",
                    (legacy_key,),
                ).fetchone()
        if legacy is None:
            return True
        return legacy[0] != content_hash or now - float(legacy[1]) > 300

    def record_delivery(self, key: str, severity: str, status: str, now: float,
                        legacy_key: str | None = None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO deliveries VALUES(?,?,?,?)", (key, severity, status, now))
            if legacy_key is not None:
                db.execute("DELETE FROM legacy_deliveries WHERE dedupe_key=?", (legacy_key,))

    def analysis_budget_available(self, severity: str, now: float) -> bool:
        with self._connect() as db:
            hourly, daily = db.execute("SELECT SUM(analyzed_at>?), COUNT(*) FROM analyses WHERE analyzed_at>?", (now - HOUR, now - DAY)).fetchone()
            warning_hourly, warning_daily = db.execute("SELECT SUM(analyzed_at>?), COUNT(*) FROM analyses WHERE severity='warning' AND analyzed_at>?", (now - HOUR, now - DAY)).fetchone()
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
            hourly, daily = db.execute("SELECT SUM(analyzed_at>?), COUNT(*) FROM analyses WHERE analyzed_at>?", (now - HOUR, now - DAY)).fetchone()
            warning_hourly, warning_daily = db.execute("SELECT SUM(analyzed_at>?), COUNT(*) FROM analyses WHERE severity='warning' AND analyzed_at>?", (now - HOUR, now - DAY)).fetchone()
            allowed = int(hourly or 0) < 6 and int(daily or 0) < 30
            if severity == "warning":
                allowed = allowed and int(warning_hourly or 0) < 2 and int(warning_daily or 0) < 10
            if allowed:
                db.execute("INSERT INTO analyses VALUES(?,?,?)", (key, severity, now))
            return allowed

    def cache_analysis(self, key: str, analysis: dict, now: float) -> None:
        clean = _sanitize_cached_analysis(analysis)
        with self._connect() as db:
            db.execute("INSERT INTO analysis_cache VALUES(?,?,?) ON CONFLICT(incident_key) DO UPDATE SET sanitized_analysis=excluded.sanitized_analysis,cached_at=excluded.cached_at", (key, json.dumps(clean, ensure_ascii=False, separators=(",", ":")), now))

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
            db.execute("DELETE FROM legacy_deliveries WHERE last_success<?", (now - METADATA_TTL,))
            db.execute("DELETE FROM analyses WHERE analyzed_at<?", (now - METADATA_TTL,))
            db.execute("DELETE FROM failures WHERE failed_at<?", (now - METADATA_TTL,))
            rows = db.execute(
                """
                SELECT table_name, row_id FROM (
                    SELECT 'deliveries' AS table_name, rowid AS row_id, delivered_at AS event_time FROM deliveries
                    UNION ALL SELECT 'legacy_deliveries', rowid, last_success FROM legacy_deliveries
                    UNION ALL SELECT 'analyses', rowid, analyzed_at FROM analyses
                    UNION ALL SELECT 'analysis_cache', rowid, cached_at FROM analysis_cache
                    UNION ALL SELECT 'failures', rowid, failed_at FROM failures
                )
                ORDER BY event_time ASC, table_name ASC, row_id ASC
                LIMIT MAX(0, (
                    SELECT (SELECT COUNT(*) FROM deliveries)
                      + (SELECT COUNT(*) FROM legacy_deliveries)
                      + (SELECT COUNT(*) FROM analyses)
                      + (SELECT COUNT(*) FROM analysis_cache)
                      + (SELECT COUNT(*) FROM failures) - 1000
                ))
                """
            ).fetchall()
            for table in ("deliveries", "legacy_deliveries", "analyses", "analysis_cache", "failures"):
                row_ids = [(row_id,) for table_name, row_id in rows if table_name == table]
                if row_ids:
                    db.executemany(f"DELETE FROM {table} WHERE rowid=?", row_ids)
