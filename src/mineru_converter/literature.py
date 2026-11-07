"""Portable identity and note naming for literature imports (no network calls)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from datetime import datetime
from typing import Any
from .document_roles import document_stem


def source_frontmatter(markdown: str, source: dict[str, Any]) -> str:
    """Persist enough attachment identity to recognise later PDF revisions."""
    if not source.get("citation_key") and not source.get("zotero_attachment_key"):
        return markdown
    citation = source.get("citation_key")
    attachment_key = source.get("zotero_attachment_key")
    digest = source.get("sha256")
    if citation:
        document_id = document_stem(
            citation,
            source.get("document_role"),
            attachment_key,
            digest,
        )
    else:
        document_id = f"zotero_{attachment_key}_{str(digest or '')[:12]}"
    fields = {
        "type": "literature_fulltext" if citation else "attachment_fulltext",
        "citekey": citation,
        "document_role": source.get("document_role") or "main",
        "document_id": document_id,
        "role_source": source.get("role_source") or "legacy-default",
        "attachment_title": source.get("attachment_title"),
        "zotero_item_key": source.get("zotero_item_key"),
        "zotero_attachment_key": attachment_key,
        "zotero_link": source.get("zotero_uri"),
        "source_sha256": digest,
        "source_note": source.get("obsidian_note"),
        "source_basis": "fulltext_extraction",
        "reviewed": False,
        "created": datetime.now().isoformat(timespec="seconds"),
        "tags": ["literature", "全文抽取"],
    }
    match = re.match(r"\A---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|\Z)", markdown)
    existing = match.group(1) if match else ""
    # Preserve an extractor's existing frontmatter, only adding absent keys.
    lines = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items()
             if v is not None and not re.search(rf"^{re.escape(k)}\s*:", existing, re.M)]
    header = "\n".join(filter(None, [existing, *lines]))
    body = markdown[match.end():] if match else markdown
    return f"---\n{header}\n---\n\n{body.lstrip()}"


def citation_key(markdown: str) -> str | None:
    match = re.match(r"\A---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|\Z)", markdown)
    if not match:
        return None
    value = re.search(r'^citekey:\s*(.+?)\s*$', match.group(1), re.M)
    if not value:
        return None
    raw = value.group(1)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = raw.strip("'\"")
    return result if isinstance(result, str) and re.fullmatch(r"[\w.\-]+", result) else None


def scalar_property(markdown: str, name: str) -> str | None:
    header = re.match(r"\A---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|\Z)", markdown)
    if not header:
        return None
    m = re.search(rf"^{re.escape(name)}:\s*(.+?)\s*$", header.group(1), re.M)
    if not m:
        return None
    try:
        result = json.loads(m.group(1))
    except ValueError:
        result = m.group(1).strip("'\"")
    return result if isinstance(result, str) else None


def fulltext_navigation(markdown: str, note: str | None) -> str:
    if not note or "%% literature navigation %%" in markdown:
        return markdown
    if any(c in note for c in ["[", "]", "|", "\n", "\r"]):
        raise ValueError("source note contains unsupported wikilink characters")
    link = note.removesuffix(".md")
    block = f"%% literature navigation %%\n> [!info] 文献关联\n> 文献笔记：[[{link}|返回标注与个人总结]]\n> 本页是全文抽取；AI 精读结论请核对原文后写入文献笔记。\n\n"
    match = re.match(r"\A---\r?\n[\s\S]*?\r?\n---(?:\r?\n|\Z)", markdown)
    if match:
        return markdown[:match.end()] + "\n" + block + markdown[match.end():].lstrip()
    return block + markdown


def literature_target(vault: Path, keys: list[str]) -> str:
    """Follow the same canonical cards as Obsidian, keeping generic imports separate."""
    folder, child = "10_Literature", "全文"
    settings_file = vault / ".obsidian/plugins/local-literature-workflow/data.json"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        folder, child = settings.get("folder", folder), settings.get("fulltextFolder", child)
    except (OSError, ValueError, AttributeError):
        pass
    parents = set()
    for key in keys:
        if not re.fullmatch(r"[\w.\-]+", key):
            continue
        cards = [p for p in vault.rglob(f"{key}.md") if not p.is_symlink() and p.is_file()
                 and not any(x in {"backups", ".obsidian", ".git", "90_Templates"} for x in p.relative_to(vault).parts)]
        if len(cards) == 1:
            parents.add(cards[0].parent.relative_to(vault).as_posix())
    if len(parents) == 1:
        folder = parents.pop()
    result = Path(folder) / child
    if result.is_absolute() or ".." in result.parts or ":" in str(result):
        raise ValueError("literature folder must be a vault-relative directory")
    return result.as_posix()
