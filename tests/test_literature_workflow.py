from pathlib import Path

import pytest

from mineru_converter.cli import build_parser
from mineru_converter.literature import citation_key, source_frontmatter, literature_target
from mineru_converter.obsidian import ObsidianImportError, create_import_plan, execute_import
from mineru_converter.paths import tokenize_pasted_paths


def test_identity_survives_output_and_unicode_quotes() -> None:
    source = {"citation_key": "example.etal2025paper", "zotero_item_key": "ITEM1234", "sha256": "abcd", "obsidian_note": "10_Literature/example.etal2025paper.md"}
    text = source_frontmatter('# "原文"\n', source)
    assert citation_key(text) == source["citation_key"]
    assert 'zotero_item_key: "ITEM1234"' in text
    assert 'source_sha256: "abcd"' in text
    assert source_frontmatter(text, source) == text


def test_fulltext_does_not_collide_with_zotero_card_and_links_back(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    card = vault / "10_Literature/topic/a2026.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ntype: literature\n---\n# My notes", encoding="utf-8")
    result = tmp_path / "result"
    result.mkdir()
    (result / "a2026.md").write_text("# Full text\n", encoding="utf-8")
    plan = create_import_plan(result, vault, "10_Literature/topic/全文", literature=True,
                              source_metadata={"citation_key": "a2026", "obsidian_note": "10_Literature/topic/a2026.md", "zotero_item_key": "ITEM"})
    assert plan.note_destination.name == "a2026__全文.md"
    assert "[[10_Literature/topic/a2026|" in plan.rewritten_markdown
    execute_import(plan)
    assert card.read_text(encoding="utf-8").endswith("# My notes")


def test_new_file_between_preview_and_apply_is_not_overwritten(tmp_path: Path) -> None:
    result = tmp_path / "result"
    result.mkdir()
    (result / "a.md").write_text("# Imported", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    plan = create_import_plan(result, vault, "notes")
    plan.note_destination.parent.mkdir(parents=True)
    plan.note_destination.write_text("Important concurrent note", encoding="utf-8")
    with pytest.raises(ObsidianImportError):
        execute_import(plan)
    assert plan.note_destination.read_text() == "Important concurrent note"


def test_bridge_cli_queues_a_pdf_and_passes_identity_without_convert() -> None:
    args = build_parser().parse_args(["gui", "paper.pdf", "--vault", "vault", "--target", "10_Literature/全文", "--citation-key", "a2026", "--source-note", "10_Literature/a2026.md"])
    assert args.paths == ["paper.pdf"]
    assert args.citation_key == "a2026"
    assert args.command == "gui"


def test_zotero_picker_follows_card_directory_instead_of_old_math_target(tmp_path: Path) -> None:
    card = tmp_path / "10_Literature/纳米光学/a2026.md"
    card.parent.mkdir(parents=True)
    card.write_text("# Card", encoding="utf-8")
    assert literature_target(tmp_path, ["a2026"]) == "10_Literature/纳米光学/全文"


def test_cannot_override_fulltext_name_with_card_name(tmp_path: Path) -> None:
    result = tmp_path / "result"
    result.mkdir()
    (result / "a2026.md").write_text("# Fulltext", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ObsidianImportError):
        create_import_plan(result, vault, "10_Literature", note_name="a2026.md", literature=True)


def test_windows_drive_and_unc_paths_keep_backslashes() -> None:
    assert tokenize_pasted_paths(r'"C:\Users\duck\Paper name.pdf" D:\papers\one.pdf') == [r"C:\Users\duck\Paper name.pdf", r"D:\papers\one.pdf"]
    assert tokenize_pasted_paths(r'"\\server\share\Paper name.pdf"') == [r"\\server\share\Paper name.pdf"]


def test_windows_settings_use_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from mineru_converter import settings
    monkeypatch.setattr(settings.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert settings.default_settings_path() == tmp_path / "MinerU PDF to Markdown/settings.json"
