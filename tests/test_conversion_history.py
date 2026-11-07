import json
from pathlib import Path

import pytest

from mineru_converter.conversion_history import (
    annotate_conversion_history,
    compare_identities,
    scan_conversion_history,
)
from mineru_converter.literature import source_frontmatter
from mineru_converter.models import SourceRecord
from mineru_converter.obsidian import ObsidianImportError, create_import_plan, execute_import


def _write_identified_result(
    root: Path,
    directory: str,
    *,
    attachment_key: str,
    sha256: str,
    citation_key: str = "author2026",
) -> Path:
    output = root / directory
    output.mkdir(parents=True)
    markdown = source_frontmatter(
        "# Converted paper\n",
        {
            "citation_key": citation_key,
            "zotero_attachment_key": attachment_key,
            "sha256": sha256,
            "document_role": "main",
        },
    )
    (output / f"{citation_key}.md").write_text(markdown, encoding="utf-8")
    return output


def _record(
    tmp_path: Path,
    *,
    attachment_key: str | None,
    sha256: str,
) -> SourceRecord:
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"fixture")
    return SourceRecord(
        token=str(path),
        path=path,
        status="valid",
        sha256=sha256,
        citation_key="author2026",
        zotero_attachment_key=attachment_key,
    )


def test_history_marks_same_attachment_and_pdf_without_mutating_record(tmp_path: Path) -> None:
    digest = "a" * 64
    output = _write_identified_result(
        tmp_path / "outputs",
        "author2026-" + digest[:8],
        attachment_key="ATTACH01",
        sha256=digest,
    )
    record = _record(tmp_path, attachment_key="ATTACH01", sha256=digest)

    annotated = annotate_conversion_history([record], tmp_path / "outputs")

    assert annotated[0].history_status == "unchanged"
    assert annotated[0].history_output_dir == str(output)
    assert annotated[0].history_sha256 == digest
    assert record.history_status is None


def test_history_marks_pdf_change_for_the_same_attachment(tmp_path: Path) -> None:
    previous_digest = "a" * 64
    current_digest = "b" * 64
    _write_identified_result(
        tmp_path / "outputs",
        "author2026-" + previous_digest[:8],
        attachment_key="ATTACH01",
        sha256=previous_digest,
    )
    record = _record(tmp_path, attachment_key="ATTACH01", sha256=current_digest)

    annotated = annotate_conversion_history([record], tmp_path / "outputs")

    assert annotated[0].history_status == "changed"
    assert annotated[0].history_sha256 == previous_digest


def test_same_pdf_on_a_different_attachment_is_not_an_existing_conversion(tmp_path: Path) -> None:
    digest = "c" * 64
    _write_identified_result(
        tmp_path / "outputs",
        "author2026-" + digest[:8],
        attachment_key="ATTACH01",
        sha256=digest,
    )
    record = _record(tmp_path, attachment_key="ATTACH02", sha256=digest)

    annotated = annotate_conversion_history([record], tmp_path / "outputs")

    assert annotated[0].history_status is None


def test_legacy_name_hash_remains_detectable_for_pasted_paths(tmp_path: Path) -> None:
    digest = "d" * 64
    output = tmp_path / "outputs" / ("paper-" + digest[:8])
    output.mkdir(parents=True)
    (output / "paper.md").write_text("# Legacy result\n", encoding="utf-8")
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"fixture")
    record = SourceRecord(
        token=str(path),
        path=path,
        status="valid",
        sha256=digest,
    )

    annotated = annotate_conversion_history([record], tmp_path / "outputs")

    assert annotated[0].history_status == "legacy-same"
    assert annotated[0].history_output_dir == str(output)


def test_failed_manifest_is_not_treated_as_a_completed_conversion(tmp_path: Path) -> None:
    digest = "e" * 64
    output = tmp_path / "outputs" / ("paper-" + digest[:8])
    output.mkdir(parents=True)
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "source": {
                    "sha256": digest,
                    "zotero_attachment_key": "ATTACH01",
                    "citation_key": "author2026",
                },
            }
        ),
        encoding="utf-8",
    )

    assert scan_conversion_history(tmp_path / "outputs") == []


def test_attachment_identity_is_persisted_without_a_citation_key() -> None:
    markdown = source_frontmatter(
        "# Paper\n",
        {
            "zotero_attachment_key": "ATTACH01",
            "sha256": "f" * 64,
            "zotero_item_key": "ITEM0001",
        },
    )

    assert 'type: "attachment_fulltext"' in markdown
    assert 'zotero_attachment_key: "ATTACH01"' in markdown
    assert f'source_sha256: "{"f" * 64}"' in markdown
    assert "citekey:" not in markdown


def test_import_prefers_current_source_metadata_over_stale_frontmatter(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target = vault / "10_Literature" / "全文"
    target.mkdir(parents=True)
    previous_digest = "3" * 64
    stale_digest = "4" * 64
    current_digest = "5" * 64
    previous = target / "author2026__全文.md"
    previous.write_text(
        source_frontmatter(
            "# Previous\n",
            {
                "citation_key": "author2026",
                "zotero_attachment_key": "ATTACH01",
                "sha256": previous_digest,
                "document_role": "main",
            },
        ),
        encoding="utf-8",
    )
    result = tmp_path / "result"
    result.mkdir()
    (result / "author2026.md").write_text(
        source_frontmatter(
            "# Current\n",
            {
                "citation_key": "author2026",
                "zotero_attachment_key": "ATTACH01",
                "sha256": stale_digest,
                "document_role": "main",
            },
        ),
        encoding="utf-8",
    )

    plan = create_import_plan(
        result,
        vault,
        "10_Literature/全文",
        literature=True,
        source_metadata={
            "citation_key": "author2026",
            "zotero_attachment_key": "ATTACH01",
            "sha256": current_digest,
            "document_role": "main",
        },
    )

    assert plan.current_source_sha256 == current_digest
    assert plan.previous_source_sha256 == previous_digest
    assert plan.identity_status == "changed"


def test_import_marks_changed_pdf_and_never_overwrites_by_default(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target = vault / "10_Literature" / "全文"
    target.mkdir(parents=True)
    previous_digest = "1" * 64
    current_digest = "2" * 64
    previous = target / "author2026__全文.md"
    previous.write_text(
        source_frontmatter(
            "# Previous fulltext\n",
            {
                "citation_key": "author2026",
                "zotero_attachment_key": "ATTACH01",
                "sha256": previous_digest,
                "document_role": "main",
            },
        ),
        encoding="utf-8",
    )

    result = tmp_path / "result"
    result.mkdir()
    (result / "author2026.md").write_text("# New fulltext\n", encoding="utf-8")
    plan = create_import_plan(
        result,
        vault,
        "10_Literature/全文",
        literature=True,
        source_metadata={
            "citation_key": "author2026",
            "zotero_attachment_key": "ATTACH01",
            "sha256": current_digest,
            "document_role": "main",
        },
    )

    assert plan.identity_status == "changed"
    assert plan.current_source_sha256 == current_digest
    assert plan.previous_source_sha256 == previous_digest
    original = previous.read_text(encoding="utf-8")
    with pytest.raises(ObsidianImportError):
        execute_import(plan)
    assert previous.read_text(encoding="utf-8") == original

    backup = execute_import(plan, overwrite=True, backup_root=tmp_path / "backups")
    assert backup is not None
    assert current_digest in previous.read_text(encoding="utf-8")
    assert previous_digest in (backup / previous.relative_to(vault)).read_text(encoding="utf-8")


def test_identity_comparison_uses_hash_prefix_for_legacy_outputs() -> None:
    from mineru_converter.conversion_history import ConversionIdentity

    full = ConversionIdentity(sha256="a" * 64)
    legacy = ConversionIdentity(sha256="a" * 8)
    assert compare_identities(full, legacy) == "legacy-same"
