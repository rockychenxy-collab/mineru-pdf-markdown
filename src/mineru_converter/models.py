from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ParseOptions:
    """Options accepted by MinerU's v4 extraction endpoints."""

    model_version: str = "vlm"
    is_ocr: bool = False
    enable_formula: bool = True
    enable_table: bool = True
    language: str = "ch"
    page_ranges: str | None = None
    extra_formats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.model_version not in {"pipeline", "vlm", "MinerU-HTML"}:
            raise ValueError("model_version must be pipeline, vlm, or MinerU-HTML")
        if not self.language.strip():
            raise ValueError("language cannot be empty")
        invalid = set(self.extra_formats) - {"docx", "html", "latex"}
        if invalid:
            raise ValueError(f"unsupported extra formats: {sorted(invalid)}")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_version": self.model_version,
            "is_ocr": self.is_ocr,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
        }
        if self.page_ranges:
            payload["page_ranges"] = self.page_ranges
        if self.extra_formats:
            payload["extra_formats"] = list(self.extra_formats)
        return payload


@dataclass
class SourceRecord:
    token: str
    path: Path | None
    status: str
    size: int | None = None
    sha256: str | None = None
    pages: int | None = None
    duplicate_of: str | None = None
    data_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    error: str | None = None
    # Zotero metadata is optional so ordinary pasted paths keep the original
    # filename-based output behavior.
    citation_key: str | None = None
    zotero_item_key: str | None = None
    zotero_attachment_key: str | None = None
    zotero_uri: str | None = None
    obsidian_note: str | None = None
    document_role: str | None = None
    role_source: str | None = None
    attachment_title: str | None = None
    history_status: str | None = None
    history_output_dir: str | None = None
    history_sha256: str | None = None

    @property
    def valid(self) -> bool:
        return self.status == "valid" and self.path is not None and self.sha256 is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "path": str(self.path) if self.path else None,
            "status": self.status,
            "size": self.size,
            "sha256": self.sha256,
            "pages": self.pages,
            "duplicate_of": self.duplicate_of,
            "data_id": self.data_id,
            "aliases": list(self.aliases),
            "error": self.error,
            "citation_key": self.citation_key,
            "zotero_item_key": self.zotero_item_key,
            "zotero_attachment_key": self.zotero_attachment_key,
            "zotero_uri": self.zotero_uri,
            "obsidian_note": self.obsidian_note,
            "document_role": self.document_role,
            "role_source": self.role_source,
            "attachment_title": self.attachment_title,
            "history_status": self.history_status,
            "history_output_dir": self.history_output_dir,
            "history_sha256": self.history_sha256,
        }


@dataclass
class BatchSubmission:
    batch_id: str
    records: list[SourceRecord]
    upload_urls: list[str] = field(default_factory=list)


@dataclass
class TaskResult:
    file_name: str
    state: str
    full_zip_url: str | None = None
    data_id: str | None = None
    task_id: str | None = None
    error: str | None = None
    progress: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "state": self.state,
            # Signed object-storage URLs are intentionally not serialised.
            "has_full_zip_url": bool(self.full_zip_url),
            "data_id": self.data_id,
            "task_id": self.task_id,
            "error": self.error,
            "progress": self.progress,
        }
