"""
Bug Report Service — business logic for capturing and managing bug reports.

Responsibilities:
    - Assign a sequential ``BUG-{n}`` id and UTC timestamp to each report
    - Persist reports to an append-only JSONL file (one JSON object per line)
    - List / filter stored reports for the admin page
    - Update a report's lifecycle status (open → in_progress → resolved / wont_fix)
    - Verify the admin password and mint/validate a signed admin unlock cookie

All persistence is isolated here, so swapping the JSONL sink for a database or
for filing real Athena tickets later is a single-class change.
"""

import hashlib
import hmac
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src.models.bug_report import (
    VALID_STATUSES,
    BugReport,
    BugReportRequest,
)

logger = logging.getLogger(__name__)

# Default storage location: data/bug_reports/reports.jsonl at the project root.
_DEFAULT_STORAGE = (
    Path(__file__).resolve().parent.parent.parent / "data" / "bug_reports" / "reports.jsonl"
)


class BugReportService:
    """Captures, persists, and manages user-submitted bug reports."""

    def __init__(
        self,
        storage_path: Path | str | None = None,
        admin_password: str = "",
        admin_secret: str = "",
        admin_unlock_hours: float = 8.0,
    ) -> None:
        self._storage_path = Path(storage_path) if storage_path else _DEFAULT_STORAGE
        self._admin_password = admin_password
        self._admin_secret = admin_secret or "bug-report-admin-secret"
        self._admin_unlock_seconds = admin_unlock_hours * 3600
        # Guards concurrent reads/writes to the JSONL file (multi-user app).
        self._lock = threading.Lock()
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Submission ────────────────────────────────────────────────────

    def submit(self, request: BugReportRequest, reported_by: str) -> BugReport:
        """Persist a new bug report and return the stored record."""
        with self._lock:
            next_num = self._next_id_number_unlocked()
            report = BugReport(
                id=f"BUG-{next_num}",
                reported_by=reported_by or "unknown",
                reported_at=datetime.now(timezone.utc),
                status="open",
                **request.model_dump(),
            )
            with self._storage_path.open("a", encoding="utf-8") as fh:
                fh.write(report.model_dump_json() + "\n")
        logger.info("Bug report %s submitted by %s", report.id, report.reported_by)
        return report

    # ── Retrieval ─────────────────────────────────────────────────────

    def list_reports(self, status: str | None = None) -> list[BugReport]:
        """
        Return all stored reports, newest first.

        Optionally filter by ``status``. Malformed lines are skipped so one bad
        record never breaks the admin page.
        """
        reports = self._read_all()
        if status:
            reports = [r for r in reports if r.status == status]
        reports.sort(key=lambda r: r.reported_at, reverse=True)
        return reports

    def get_report(self, report_id: str) -> BugReport | None:
        """Return a single report by id, or None if not found."""
        for report in self._read_all():
            if report.id == report_id:
                return report
        return None


    # ── Management ────────────────────────────────────────────────────

    def update_status(self, report_id: str, status: str) -> BugReport | None:
        """
        Update a report's status and rewrite the JSONL file.

        Returns the updated report, or None if the id was not found.
        Raises ValueError for an invalid status.
        """
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {', '.join(VALID_STATUSES)}."
            )
        with self._lock:
            reports = self._read_all_unlocked()
            updated: BugReport | None = None
            for report in reports:
                if report.id == report_id:
                    report.status = status  # type: ignore[assignment]
                    updated = report
                    break
            if updated is None:
                return None
            self._rewrite_unlocked(reports)
        logger.info("Bug report %s status updated to %s", report_id, status)
        return updated

    def delete_report(self, report_id: str) -> bool:
        """Remove a report by id. Returns True if a report was deleted."""
        with self._lock:
            reports = self._read_all_unlocked()
            remaining = [r for r in reports if r.id != report_id]
            if len(remaining) == len(reports):
                return False
            self._rewrite_unlocked(remaining)
        logger.info("Bug report %s deleted", report_id)
        return True

    # ── Admin Authentication ──────────────────────────────────────────

    def verify_admin_password(self, password: str) -> bool:
        """Constant-time comparison of the supplied admin password."""
        if not self._admin_password:
            # No password configured → admin area is locked entirely.
            return False
        return hmac.compare_digest(password or "", self._admin_password)

    def create_admin_token(self) -> str:
        """Create a signed, time-limited admin unlock token."""
        issued = str(int(time.time()))
        signature = self._sign(issued)
        return f"{issued}|{signature}"

    def validate_admin_token(self, token: str | None) -> bool:
        """Validate an admin unlock token (signature + expiry)."""
        if not token or "|" not in token:
            return False
        try:
            issued_str, signature = token.rsplit("|", 1)
            if not hmac.compare_digest(signature, self._sign(issued_str)):
                return False
            issued = int(issued_str)
        except (ValueError, TypeError):
            return False
        return (time.time() - issued) <= self._admin_unlock_seconds

    def _sign(self, data: str) -> str:
        """Create an HMAC-SHA256 signature for the admin token."""
        return hmac.HMAC(
            self._admin_secret.encode(),
            data.encode(),
            hashlib.sha256,
        ).hexdigest()

    # ── Internal Persistence Helpers ──────────────────────────────────

    def _read_all(self) -> list[BugReport]:
        """Thread-safe read of all reports."""
        with self._lock:
            return self._read_all_unlocked()

    def _read_all_unlocked(self) -> list[BugReport]:
        """Read all reports from the JSONL file (caller holds the lock)."""
        if not self._storage_path.exists():
            return []
        reports: list[BugReport] = []
        with self._storage_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    reports.append(BugReport.model_validate_json(line))
                except Exception:  # noqa: BLE001 — skip a single corrupt line
                    logger.warning("Skipping malformed bug report line")
                    continue
        return reports

    def _rewrite_unlocked(self, reports: list[BugReport]) -> None:
        """Atomically rewrite the JSONL file (caller holds the lock)."""
        tmp_path = self._storage_path.with_suffix(".jsonl.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for report in reports:
                fh.write(report.model_dump_json() + "\n")
        tmp_path.replace(self._storage_path)

    def _next_id_number_unlocked(self) -> int:
        """Compute the next sequential id number (caller holds the lock)."""
        existing = self._read_all_unlocked()
        max_num = 0
        for report in existing:
            try:
                num = int(report.id.split("-", 1)[1])
                max_num = max(max_num, num)
            except (IndexError, ValueError):
                continue
        return max_num + 1
