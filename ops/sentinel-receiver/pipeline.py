"""Sentinel Phase 0 analysis-to-delivery vertical slice."""

from __future__ import annotations

import json
import threading
from typing import Any, cast
from diagnostics import collect_diagnostics
from render import render_message
from schema import sanitize_payload, validate_analysis
from store import legacy_delivery_identity
from triage import build_fallback, triage

VALID_MODES = {"off", "shadow", "gms", "hermes"}


def normalize_mode(value: str | None) -> str:
    mode = (value or "shadow").strip().lower()
    return mode if mode in VALID_MODES else "shadow"


class ProcessingGate:
    """One active analysis and at most eight queued requests."""
    def __init__(self):
        self._capacity = threading.BoundedSemaphore(9)
        self._serial = threading.Lock()

    def enter(self) -> bool:
        if not self._capacity.acquire(blocking=False):
            return False
        self._serial.acquire()
        return True

    def acquire(self, blocking: bool = False) -> bool:
        return self.enter()

    def leave(self) -> None:
        self._serial.release()
        self._capacity.release()

    def release(self) -> None:
        self.leave()


class AnalysisPipeline:
    def __init__(self, store, sender, gms=None, hermes=None, mode=None, diagnostic_query=None):
        self.store = store
        self.sender = sender
        self.gms = gms
        self.hermes = hermes
        self.mode = normalize_mode(mode)
        self.diagnostic_query = diagnostic_query
        self._gms_lock = threading.Lock()
        self._shadow_lock = threading.Lock()
        self._shadow_thread = None
        self._closed = False

    def _record_failure(self, key: str, stage: str, exc: Exception, now: float) -> None:
        try:
            self.store.record_failure(key, stage, type(exc).__name__, now)
        except Exception:
            pass

    def _analyze(self, provider, clean: dict, item: dict[str, str], now: float, name: str, budgeted: bool = True):
        if provider is None:
            return build_fallback(item)
        try:
            lock = self._gms_lock if provider is self.gms else threading.Lock()
            with lock:
                if budgeted:
                    if not self.store.reserve_analysis(item["incident_key"], item["severity"], now):
                        return build_fallback(item)
                result = validate_analysis(provider(clean))
                self.store.cache_analysis(item["incident_key"], result, now)
                return result
        except Exception as exc:
            self._record_failure(item["incident_key"], name, exc, now)
            return build_fallback(item)

    def _shadow_gms(self, clean: dict, item: dict[str, str], now: float) -> None:
        if self.gms is None:
            return

        def run():
            try:
                self._analyze(self.gms, clean, item, now, "gms-shadow")
            except Exception:
                pass

        with self._shadow_lock:
            if self._closed or (self._shadow_thread is not None and self._shadow_thread.is_alive()):
                return
            thread = threading.Thread(target=run, name="sentinel-gms-shadow", daemon=True)
            self._shadow_thread = thread
            thread.start()

    def close(self) -> None:
        """Stop accepting shadow work and wait for the bounded worker to finish."""
        with self._shadow_lock:
            self._closed = True
            thread = self._shadow_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def process(self, payload: dict, now: float) -> str:
        legacy_key, content_hash = legacy_delivery_identity(payload)
        raw_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        clean = sanitize_payload(payload, raw_size)
        item = triage(clean, now)
        key, severity, status = item["incident_key"], item["severity"], item["status"]
        try:
            if not self.store.should_deliver(
                key, severity, status, now, legacy_key, content_hash
            ):
                return "duplicate"
        except Exception:
            pass

        if status == "resolved" or self.mode == "off":
            analysis = build_fallback(item)
        elif self.mode == "gms":
            try:
                analysis = self.store.get_cached_analysis(key, now)
            except Exception:
                analysis = None
            analysis = analysis or self._analyze(self.gms, clean, item, now, "gms")
        else:  # shadow and explicit Hermes rollback keep Hermes authoritative
            analysis = self._analyze(self.hermes, clean, item, now, "hermes", budgeted=False)

        if status == "firing" and self.diagnostic_query is not None:
            try:
                cast(dict[str, Any], item)["diagnostics"] = collect_diagnostics(
                    clean, item, now, self.diagnostic_query
                )
            except Exception as exc:
                # Enrichment must never block the deterministic alert path.
                self._record_failure(key, "diagnostics", exc, now)
        message = render_message(analysis, item)
        try:
            try:
                self.sender(message)
            except Exception as exc:
                self._record_failure(key, "mattermost", exc, now)
                raise
            try:
                self.store.record_delivery(key, severity, status, now, legacy_key)
            except Exception:
                pass
        finally:
            try:
                self.store.prune(now)
            except Exception:
                pass
        if self.mode == "shadow" and status != "resolved":
            self._shadow_gms(clean, item, now)
        return "delivered"
