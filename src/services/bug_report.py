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
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.models.bug_report import (
    VALID_STATUSES,
    Attachment,
    BugReport,
    BugReportRequest,
)

logger = logging.getLogger(__name__)

# Default storage location: data/bug_reports/reports.jsonl at the project root.
_DEFAULT_STORAGE = (
    Path(__file__).resolve().parent.parent.parent / "data" / "bug_reports" / "reports.jsonl"
)

# Magic-byte signatures used to sniff real file content (defeats extension /
# declared-MIME spoofing). Maps a leading byte-prefix to a canonical MIME type.
# Only a representative prefix is checked; text formats (txt/log/csv) have no
# reliable signature and are validated by extension only.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)

# Extensions that carry no reliable magic bytes; accepted on extension alone.
_TEXT_EXTENSIONS = frozenset({"txt", "log", "csv"})


class AttachmentValidationError(ValueError):
    """Raised when an uploaded attachment violates count/size/type limits."""


def _sanitize_filename(name: str) -> str:
    """Strip directories and unsafe characters from an uploaded filename.

    Prevents path traversal (``../``) and control characters. Falls back to a
    generic name if nothing usable remains.
    """
    # Keep only the final path component, drop any directory parts.
    base = Path(name or "").name
    # Allow letters, digits, dot, dash, underscore, space; replace the rest.
    base = re.sub(r"[^A-Za-z0-9._\- ]", "_", base).strip()
    return base or "attachment"


class BugReportService:
    """Captures, persists, and manages user-submitted bug reports."""

    def __init__(
        self,
        storage_path: Path | str | None = None,
        admin_password: str = "",
        admin_secret: str = "",
        admin_unlock_hours: float = 8.0,
        max_files: int = 5,
        max_file_mb: float = 10.0,
        max_total_mb: float = 25.0,
        allowed_extensions: list[str] | None = None,
    ) -> None:
        self._storage_path = Path(storage_path) if storage_path else _DEFAULT_STORAGE
        self._admin_password = admin_password
        self._admin_secret = admin_secret or "bug-report-admin-secret"
        self._admin_unlock_seconds = admin_unlock_hours * 3600
        # Attachment limits.
        self._max_files = max_files
        self._max_file_bytes = int(max_file_mb * 1024 * 1024)
        self._max_total_bytes = int(max_total_mb * 1024 * 1024)
        self._allowed_extensions = frozenset(
            (allowed_extensions or ["png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "log", "csv", "mp4", "webm"])
        )
        # Attachments live alongside the JSONL, one directory per report.
        self._attachments_dir = self._storage_path.parent / "attachments"
        # Guards concurrent reads/writes to the JSONL file (multi-user app).
        self._lock = threading.Lock()
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Submission ────────────────────────────────────────────────────

    def submit(
        self,
        request: BugReportRequest,
        reported_by: str,
        uploads: list[tuple[str, str, bytes]] | None = None,
    ) -> BugReport:
        """Persist a new bug report and return the stored record.

        ``uploads`` is an optional list of ``(filename, content_type, data)``
        tuples. When present, they are validated and written to disk under the
        new report's id before the record is appended, so the JSONL row is
        written exactly once (preserving append-only semantics).

        Raises ``AttachmentValidationError`` if any upload violates the limits.
        """
        with self._lock:
            next_num = self._next_id_number_unlocked()
            report_id = f"BUG-{next_num}"
            attachments = self._save_attachments_unlocked(report_id, uploads or [])
            report = BugReport(
                id=report_id,
                reported_by=reported_by or "unknown",
                reported_at=datetime.now(timezone.utc),
                status="open",
                attachments=attachments,
                **request.model_dump(),
            )
            with self._storage_path.open("a", encoding="utf-8") as fh:
                fh.write(report.model_dump_json() + "\n")
        logger.info(
            "Bug report %s submitted by %s (%d attachment(s))",
            report.id,
            report.reported_by,
            len(report.attachments),
        )
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
        """Remove a report by id (and its attachments). Returns True if deleted."""
        with self._lock:
            reports = self._read_all_unlocked()
            remaining = [r for r in reports if r.id != report_id]
            if len(remaining) == len(reports):
                return False
            self._rewrite_unlocked(remaining)
            # Remove the report's attachment directory, if any.
            report_dir = self._attachments_dir / report_id
            if report_dir.exists():
                shutil.rmtree(report_dir, ignore_errors=True)
        logger.info("Bug report %s deleted", report_id)
        return True

    # ── Attachments ───────────────────────────────────────────────────

    def get_attachment_path(self, report_id: str, attachment_id: str) -> Path | None:
        """Return the on-disk path for an attachment, or None if not found.

        Looks the attachment up in the report's metadata (so only files this
        service wrote are ever served) and verifies the resolved path stays
        inside the report's attachment directory (defense-in-depth).
        """
        report = self.get_report(report_id)
        if report is None:
            return None
        for att in report.attachments:
            if att.id == attachment_id:
                report_dir = (self._attachments_dir / report_id).resolve()
                candidate = (report_dir / att.stored_filename).resolve()
                if report_dir not in candidate.parents:
                    return None
                return candidate if candidate.exists() else None
        return None

    def _save_attachments_unlocked(
        self,
        report_id: str,
        uploads: list[tuple[str, str, bytes]],
    ) -> list[Attachment]:
        """Validate and persist uploads for ``report_id`` (caller holds lock).

        Enforces max-file-count, per-file size, total size, and an extension +
        magic-byte allow-list. Writes files under ``attachments/{report_id}/``
        with uuid-prefixed names. Returns the attachment metadata list.
        """
        if not uploads:
            return []
        if len(uploads) > self._max_files:
            raise AttachmentValidationError(
                f"Too many files: {len(uploads)}. Maximum is {self._max_files}."
            )

        total = 0
        prepared: list[tuple[str, str, bytes, str]] = []
        for filename, declared_type, data in uploads:
            safe_name = _sanitize_filename(filename)
            ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
            size = len(data)
            total += size

            if not ext or ext not in self._allowed_extensions:
                raise AttachmentValidationError(
                    f"File type '.{ext or '?'}' is not allowed for '{safe_name}'. "
                    f"Allowed: {', '.join(sorted(self._allowed_extensions))}."
                )
            if size == 0:
                raise AttachmentValidationError(f"File '{safe_name}' is empty.")
            if size > self._max_file_bytes:
                mb = self._max_file_bytes / (1024 * 1024)
                raise AttachmentValidationError(
                    f"File '{safe_name}' is too large ({size / (1024 * 1024):.1f} MB). "
                    f"Maximum per file is {mb:.0f} MB."
                )

            content_type = self._verify_content(safe_name, ext, declared_type, data)
            prepared.append((safe_name, content_type, data, ext))

        if total > self._max_total_bytes:
            mb = self._max_total_bytes / (1024 * 1024)
            raise AttachmentValidationError(
                f"Attachments total too large ({total / (1024 * 1024):.1f} MB). "
                f"Maximum is {mb:.0f} MB per report."
            )

        report_dir = self._attachments_dir / report_id
        report_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Attachment] = []
        for safe_name, content_type, data, _ext in prepared:
            att_id = uuid.uuid4().hex
            stored_filename = f"{att_id}__{safe_name}"
            (report_dir / stored_filename).write_bytes(data)
            saved.append(
                Attachment(
                    id=att_id,
                    original_filename=safe_name,
                    stored_filename=stored_filename,
                    content_type=content_type,
                    size_bytes=len(data),
                )
            )
        return saved

    def _verify_content(
        self,
        filename: str,
        ext: str,
        declared_type: str,
        data: bytes,
    ) -> str:
        """Confirm the bytes match the extension via magic-byte sniffing.

        Text formats (txt/log/csv) and formats without a checked signature
        (webp/mp4/webm) fall back to the declared MIME type. Raises if a
        signature is present but disagrees with the extension.
        """
        header = data[:16]
        detected: str | None = None
        for prefix, mime in _MAGIC_SIGNATURES:
            if header.startswith(prefix):
                detected = mime
                break

        if detected is not None:
            # A recognized signature must be consistent with the extension.
            ext_to_mime = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "pdf": "application/pdf",
            }
            expected = ext_to_mime.get(ext)
            if expected is not None and detected != expected:
                raise AttachmentValidationError(
                    f"File '{filename}' content does not match its .{ext} extension."
                )
            return detected

        if ext in _TEXT_EXTENSIONS:
            return declared_type or "text/plain"
        return declared_type or "application/octet-stream"

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
