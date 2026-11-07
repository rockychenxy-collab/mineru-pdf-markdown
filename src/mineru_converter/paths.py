from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

from .models import SourceRecord


class PathListParseError(ValueError):
    pass


_SEPARATORS = {" ", "\t", "\r", "\n", ",", "，", ";", "；"}


def tokenize_pasted_paths(text: str) -> list[str]:
    """Split shell-like pasted paths while preserving quoted spaces.

    Separators are whitespace, ASCII/full-width commas, and semicolons outside
    quotes. Backslash escapes the next character outside single quotes.
    """

    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    windows_path = False

    def flush() -> None:
        nonlocal windows_path
        if buf:
            tokens.append("".join(buf))
            buf.clear()
        windows_path = False

    for index, char in enumerate(text):
        if escaped:
            buf.append(char)
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            else:
                buf.append(char)
            continue
        if char == "\\" and (windows_path or re.fullmatch(r"[A-Za-z]:", "".join(buf))
                              or (not buf and text[index:index + 2] == "\\\\")):
            windows_path = True
            buf.append(char)
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char == "\\":
                escaped = True
            else:
                buf.append(char)
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "\\":
            escaped = True
        elif char in _SEPARATORS:
            flush()
        else:
            buf.append(char)

    if escaped:
        buf.append("\\")
    if quote is not None:
        raise PathListParseError("unclosed quote in pasted path list")
    flush()
    return tokens


def _path_candidates(token: str) -> list[Path]:
    raw = token.strip()
    if len(raw) >= 2 and raw[0] == "<" and raw[-1] == ">":
        raw = raw[1:-1]

    candidates: list[str] = [raw]
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        decoded = unquote(parsed.path)
        candidates.insert(0, decoded)
    if "%" in raw:
        decoded = unquote(raw)
        if decoded not in candidates:
            candidates.append(decoded)

    paths: list[Path] = []
    for item in candidates:
        path = Path(item).expanduser()
        if path not in paths:
            paths.append(path)
    return paths


def resolve_pasted_path(token: str) -> tuple[Path, str]:
    """Return the first existing candidate and how it was resolved."""

    candidates = _path_candidates(token)
    for index, candidate in enumerate(candidates):
        if candidate.exists():
            return candidate.resolve(), "exact" if index == 0 else "decoded"
    return candidates[0].expanduser(), "missing"


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_data_id(path: Path, digest: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip("-._") or "document"
    return f"{stem[:80]}-{digest[:12]}"


def _page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path), strict=False).pages)
    except Exception:
        return None


def build_source_records(tokens: Iterable[str], deduplicate: bool = True) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    by_digest: dict[str, SourceRecord] = {}

    for token in tokens:
        path, resolution = resolve_pasted_path(token)
        if not path.exists():
            records.append(SourceRecord(token, path, "missing", error="file does not exist"))
            continue
        if not path.is_file():
            records.append(SourceRecord(token, path, "invalid", error="path is not a regular file"))
            continue
        if path.suffix.lower() != ".pdf":
            records.append(SourceRecord(token, path, "invalid", error="file extension is not .pdf"))
            continue
        if not path.stat().st_mode & 0o444:
            records.append(SourceRecord(token, path, "invalid", error="file is not readable"))
            continue

        digest = _sha256(path)
        record = SourceRecord(
            token=token,
            path=path,
            status="valid",
            size=path.stat().st_size,
            sha256=digest,
            pages=_page_count(path),
            data_id=_safe_data_id(path, digest),
        )
        if resolution == "decoded":
            record.error = "resolved using percent/file URI decoding"

        if deduplicate and digest in by_digest:
            original = by_digest[digest]
            record.status = "duplicate"
            record.duplicate_of = str(original.path)
            original.aliases.append(str(path))
        else:
            by_digest[digest] = record
        records.append(record)
    return records


def scan_pdf_files(directory: Path, recursive: bool = True) -> list[str]:
    pattern = "**/*.pdf" if recursive else "*.pdf"
    return [str(path) for path in sorted(directory.glob(pattern)) if path.is_file()]
