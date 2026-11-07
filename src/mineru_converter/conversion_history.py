"""Read-only detection of previous MinerU conversions.

Successful conversions intentionally keep a small final result instead of the
full MinerU manifest.  New results persist identity in Markdown frontmatter;
older results stay discoverable through their ``name-<hash>`` directory.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .literature import scalar_property
from .models import SourceRecord


HISTORY_LABELS = {
    "unchanged": "已转换（附件 ID 与 PDF 哈希一致）",
    "changed": "附件已变化（同一附件 ID，PDF 哈希不同）",
    "legacy-same": "已转换（旧输出哈希一致）",
    "existing": "既有转换（缺少哈希，无法判定是否变化）",
}

_LEGACY_HASH_RE = re.compile(
    r"-(?P<sha256>[0-9a-f]{8})(?:-run-\d{8}-\d{6}(?:-\d+)?)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ConversionIdentity:
    attachment_key: str | None = None
    sha256: str | None = None
    citation_key: str | None = None
    output_dir: Path | None = None
    modified_ns: int = 0


@dataclass(frozen=True)
class ExistingConversion:
    status: str
    identity: ConversionIdentity


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _normalise_hash(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    digest = text.strip().lower()
    return digest if re.fullmatch(r"[0-9a-f]{8,64}", digest) else None


def identity_from_markdown(markdown: str, *, output_dir: Path | None = None) -> ConversionIdentity:
    """Extract the conservative source identity persisted with a result."""

    return ConversionIdentity(
        attachment_key=scalar_property(markdown, "zotero_attachment_key")
        or scalar_property(markdown, "attachment_id"),
        sha256=_normalise_hash(
            scalar_property(markdown, "source_sha256")
            or scalar_property(markdown, "pdf_sha256")
        ),
        citation_key=scalar_property(markdown, "citekey")
        or scalar_property(markdown, "citation_key"),
        output_dir=output_dir,
    )


def load_markdown_identity(path: Path, *, output_dir: Path | None = None) -> ConversionIdentity:
    """Read only the beginning of a Markdown file to avoid loading full text."""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        markdown = handle.read(64 * 1024)
    return identity_from_markdown(markdown, output_dir=output_dir)


def compare_identities(
    current: ConversionIdentity,
    previous: ConversionIdentity,
) -> str | None:
    """Return ``unchanged``, ``changed``, ``legacy-same``, or no relation."""

    current_hash = _normalise_hash(current.sha256)
    previous_hash = _normalise_hash(previous.sha256)

    if current.attachment_key and previous.attachment_key:
        if current.attachment_key != previous.attachment_key:
            return None
        if current_hash and previous_hash:
            return "unchanged" if current_hash == previous_hash else "changed"
        return "existing"

    if current_hash and previous_hash:
        if len(current_hash) <= len(previous_hash):
            same_hash = previous_hash.startswith(current_hash)
        else:
            same_hash = current_hash.startswith(previous_hash)
        if same_hash:
            return "legacy-same"
    return None


def _identity_from_manifest(
    manifest_path: Path,
    output_dir: Path,
) -> ConversionIdentity | None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(manifest, Mapping) or manifest.get("status") != "done":
        return None
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        return None
    return identity_from_source(source, output_dir=output_dir)


def identity_from_source(
    source: Mapping[str, Any],
    *,
    output_dir: Path | None = None,
) -> ConversionIdentity:
    return ConversionIdentity(
        attachment_key=_text(source.get("zotero_attachment_key")),
        sha256=_normalise_hash(source.get("sha256")),
        citation_key=_text(source.get("citation_key")),
        output_dir=output_dir,
    )


def _legacy_identity(output_dir: Path) -> ConversionIdentity | None:
    match = _LEGACY_HASH_RE.search(output_dir.name)
    if not match:
        return None
    return ConversionIdentity(
        sha256=match.group("sha256").lower(),
        output_dir=output_dir,
    )


def _identity_from_output(output_dir: Path) -> ConversionIdentity | None:
    markdown_files = sorted(
        path
        for path in output_dir.glob("*.md")
        if path.is_file() and not path.is_symlink()
    )
    if not markdown_files:
        return None

    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        identity = _identity_from_manifest(manifest_path, output_dir)
        if identity is not None:
            try:
                return replace(identity, modified_ns=output_dir.stat().st_mtime_ns)
            except OSError:
                return identity

    for markdown_path in markdown_files:
        try:
            identity = load_markdown_identity(markdown_path, output_dir=output_dir)
        except OSError:
            continue
        if identity.attachment_key or identity.sha256:
            try:
                return replace(identity, modified_ns=output_dir.stat().st_mtime_ns)
            except OSError:
                return identity

    identity = _legacy_identity(output_dir)
    if identity is not None:
        try:
            return replace(identity, modified_ns=output_dir.stat().st_mtime_ns)
        except OSError:
            pass
    return identity


def scan_conversion_history(output_root: Path) -> list[ConversionIdentity]:
    """Inspect final result directories without following links or failed runs."""

    raw_root = Path(str(output_root)).expanduser().absolute()
    # Reject every symlink component before resolving or entering any directory.
    if any(part.is_symlink() for part in (*reversed(raw_root.parents), raw_root)):
        return []
    if not raw_root.is_dir():
        return []

    root = raw_root

    identities: list[ConversionIdentity] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        identity = _identity_from_output(child)
        if identity is not None:
            identities.append(identity)
    return identities


def find_existing_conversion(
    record: SourceRecord,
    history: Iterable[ConversionIdentity],
) -> ExistingConversion | None:
    current = ConversionIdentity(
        attachment_key=record.zotero_attachment_key,
        sha256=record.sha256,
        citation_key=record.citation_key,
    )
    ranked: list[tuple[int, ExistingConversion]] = []
    priority = {"unchanged": 0, "legacy-same": 1, "changed": 2, "existing": 3}
    for previous in history:
        status = compare_identities(current, previous)
        if status is None:
            continue
        ranked.append((priority[status], ExistingConversion(status=status, identity=previous)))
    if not ranked:
        return None
    ranked.sort(
        key=lambda item: (
            item[0],
            -item[1].identity.modified_ns,
            str(item[1].identity.output_dir or ""),
        )
    )
    return ranked[0][1]


def annotate_conversion_history(
    records: Iterable[SourceRecord],
    output_root: Path | None,
) -> list[SourceRecord]:
    """Return records annotated with a previous conversion when one is known."""

    history = scan_conversion_history(output_root) if output_root is not None else []
    annotated: list[SourceRecord] = []
    for record in records:
        match = find_existing_conversion(record, history)
        annotated.append(
            replace(
                record,
                history_status=match.status if match else None,
                history_output_dir=(
                    str(match.identity.output_dir)
                    if match and match.identity.output_dir
                    else None
                ),
                history_sha256=(
                    match.identity.sha256 if match else None
                ),
            )
        )
    return annotated


def history_label(status: str | None) -> str | None:
    return HISTORY_LABELS.get(status or "")
