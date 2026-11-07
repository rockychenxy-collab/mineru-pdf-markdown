from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from .bundle import safe_document_stem
from .conversion_history import (
    ConversionIdentity,
    compare_identities,
    identity_from_markdown,
    identity_from_source,
    load_markdown_identity,
)
from .literature import citation_key, fulltext_navigation, source_frontmatter, scalar_property
from .document_roles import document_stem


class ObsidianImportError(RuntimeError):
    pass


@dataclass
class ImportPlan:
    vault: Path
    target_dir: Path
    source_dir: Path
    note_source: Path
    note_destination: Path
    attachment_dir: Path
    assets: list[tuple[Path, Path]] = field(default_factory=list)
    conflicts: list[Path] = field(default_factory=list)
    external_images: list[str] = field(default_factory=list)
    missing_images: list[str] = field(default_factory=list)
    rewritten_markdown: str = ""
    identity_status: str | None = None
    current_attachment_key: str | None = None
    current_source_sha256: str | None = None
    previous_attachment_key: str | None = None
    previous_source_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "vault": str(self.vault),
            "target_dir": str(self.target_dir),
            "source_dir": str(self.source_dir),
            "note_source": str(self.note_source),
            "note_destination": str(self.note_destination),
            "attachment_dir": str(self.attachment_dir),
            "assets": [{"source": str(source), "destination": str(destination)} for source, destination in self.assets],
            "conflicts": [str(path) for path in self.conflicts],
            "external_images": list(self.external_images),
            "missing_images": list(self.missing_images),
            "identity_status": self.identity_status,
            "current_attachment_key": self.current_attachment_key,
            "current_source_sha256": self.current_source_sha256,
            "previous_attachment_key": self.previous_attachment_key,
            "previous_source_sha256": self.previous_source_sha256,
            "will_write": len(self.assets) + 1,
        }


def _inside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_target(vault: Path, target_dir: str | Path) -> Path:
    target = Path(target_dir).expanduser()
    if not target.is_absolute():
        target = vault / target
    target = target.resolve()
    if not _inside(vault, target):
        raise ObsidianImportError("Obsidian target directory must be inside the selected vault")
    return target


def _find_note_source(result_dir: Path) -> Path:
    preferred = result_dir / "full.md"
    if preferred.is_file():
        return preferred
    candidates = sorted(path for path in result_dir.rglob("*.md") if path.is_file() and path.name != "manifest.md")
    if not candidates:
        raise ObsidianImportError(f"missing Markdown result in: {result_dir}")
    return candidates[0]


def _find_assets_dir(result_dir: Path, note_source: Path) -> Path | None:
    preferred = result_dir / f"{note_source.stem}_assets"
    if preferred.is_dir():
        return preferred
    legacy = result_dir / "assets"
    if legacy.is_dir():
        return legacy
    candidates = sorted(path for path in result_dir.iterdir() if path.is_dir() and path.name.endswith("_assets"))
    return candidates[0] if candidates else None


def _reference_variants(reference: str) -> list[str]:
    raw = unquote(reference.strip().strip("<>"))
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        raw = unquote(parsed.path)
    raw = raw.split("#", 1)[0].split("?", 1)[0]
    normalized = raw.replace("\\", "/")
    variants = [normalized, normalized.lstrip("./")]
    if Path(raw).is_absolute():
        variants.append(str(Path(raw).resolve()))
    return list(dict.fromkeys(variants))


def _rewrite_for_obsidian(
    markdown: str,
    assets: dict[str, str],
) -> tuple[str, list[str], list[str]]:
    external: list[str] = []
    missing: list[str] = []
    pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

    def resolve_asset(destination: str) -> str | None:
        variants = _reference_variants(destination)
        for key in variants:
            if key in assets:
                return assets[key]
        basename = Path(variants[0]).name
        matches = sorted({value for key, value in assets.items() if Path(key).name == basename})
        return matches[0] if len(matches) == 1 else None

    def replace(match: re.Match[str]) -> str:
        alt, contents = match.groups()
        raw = contents.strip()
        if raw.startswith("<") and ">" in raw:
            destination = raw[1 : raw.find(">")]
        else:
            parts = raw.split(None, 1)
            destination = parts[0].strip("<>") if parts else raw
        scheme = urlparse(unquote(destination)).scheme
        if scheme in {"http", "https", "data"}:
            external.append(destination)
            return match.group(0)
        target = resolve_asset(destination)
        if target is None:
            missing.append(destination)
            return match.group(0)
        return f"![[{target}]]"

    return pattern.sub(replace, markdown), sorted(set(external)), sorted(set(missing))


def create_import_plan(
    result_dir: Path,
    vault: Path,
    target_dir: str | Path,
    note_name: str | None = None,
    *,
    literature: bool = False,
    source_metadata: dict[str, object] | None = None,
) -> ImportPlan:
    result_dir = result_dir.expanduser().resolve()
    vault = vault.expanduser().resolve()
    note_source = _find_note_source(result_dir)
    assets_dir = _find_assets_dir(result_dir, note_source)
    if not vault.is_dir():
        raise ObsidianImportError(f"vault does not exist: {vault}")

    target = _resolve_target(vault, target_dir)
    markdown = note_source.read_text(encoding="utf-8", errors="replace")
    if source_metadata:
        markdown = source_frontmatter(markdown, source_metadata)
    key = citation_key(markdown)
    identity = None
    default_name = f"{result_dir.name}.md" if note_source.name == "full.md" else f"{note_source.stem}.md"
    if literature or key:
        identity = scalar_property(markdown, "document_id")
        if not identity and key:
            identity = document_stem(key, scalar_property(markdown, "document_role"), scalar_property(markdown, "zotero_attachment_key"), scalar_property(markdown, "source_sha256"))
        stem = safe_document_stem(identity or key or note_source.stem).removesuffix("__全文")
        default_name = f"{stem}__全文.md"
    name = note_name or default_name
    if not name.lower().endswith(".md"):
        name += ".md"
    if (literature or key) and not name.endswith("__全文.md"):
        raise ObsidianImportError("literature fulltext names must end with __全文.md to protect Zotero notes")
    note_destination = target / name
    if not _inside(target, note_destination.resolve()):
        raise ObsidianImportError("note name must remain inside the selected target directory")

    attachment_stem = identity or (result_dir.name if note_source.name == "full.md" else note_source.stem)
    attachment_dir = vault / "99_Attachments" / "mineru" / safe_document_stem(attachment_stem)
    if not _inside(vault, attachment_dir.resolve()):
        raise ObsidianImportError("attachment directory must remain inside the selected vault")

    asset_pairs: list[tuple[Path, Path]] = []
    asset_map: dict[str, str] = {}
    if assets_dir is not None:
        for source in sorted(path for path in assets_dir.rglob("*") if path.is_file()):
            relative = source.relative_to(assets_dir).as_posix()
            destination = attachment_dir / relative
            if not _inside(vault, destination.resolve()):
                raise ObsidianImportError("asset destination must remain inside the selected vault")
            asset_pairs.append((source, destination))
            target_reference = destination.relative_to(vault).as_posix()
            aliases = {
                relative,
                f"assets/{relative}",
                f"{assets_dir.name}/{relative}",
                str(source.resolve()),
                str((result_dir / relative).resolve()),
            }
            for alias in aliases:
                asset_map[alias.replace("\\", "/")] = target_reference

    if literature or key:
        source_note = str((source_metadata or {}).get("obsidian_note") or "")
        if not source_note and key:
            candidates = [p for p in vault.rglob(f"{key}.md") if not p.is_symlink() and p.is_file() and not any(part in {"backups", ".obsidian", ".git"} for part in p.relative_to(vault).parts)]
            if len(candidates) == 1:
                source_note = candidates[0].relative_to(vault).as_posix()
        if source_note and (Path(source_note).is_absolute() or ".." in Path(source_note).parts):
            raise ObsidianImportError("source note must remain inside the vault")
        if source_note and (vault / source_note).is_file():
            if key:
                markdown = source_frontmatter(markdown, {"citation_key": key, "obsidian_note": source_note})
            markdown = fulltext_navigation(markdown, source_note)
    rewritten, external, missing = _rewrite_for_obsidian(markdown, asset_map)
    conflicts = [path for path in [note_destination, *[dst for _, dst in asset_pairs]] if path.exists()]
    current_identity = identity_from_markdown(markdown, output_dir=result_dir)
    if source_metadata:
        metadata_identity = identity_from_source(source_metadata, output_dir=result_dir)
        current_identity = ConversionIdentity(
            attachment_key=metadata_identity.attachment_key or current_identity.attachment_key,
            sha256=metadata_identity.sha256 or current_identity.sha256,
            citation_key=metadata_identity.citation_key or current_identity.citation_key,
            output_dir=result_dir,
        )
    previous_identity = None
    if note_destination.is_file() and not note_destination.is_symlink():
        try:
            previous_identity = load_markdown_identity(note_destination)
        except OSError:
            previous_identity = None
    identity_status = (
        compare_identities(current_identity, previous_identity)
        if previous_identity is not None
        else None
    )
    return ImportPlan(
        vault=vault,
        target_dir=target,
        source_dir=result_dir,
        note_source=note_source,
        note_destination=note_destination,
        attachment_dir=attachment_dir,
        assets=asset_pairs,
        conflicts=conflicts,
        external_images=external,
        missing_images=missing,
        rewritten_markdown=rewritten,
        identity_status=identity_status,
        current_attachment_key=current_identity.attachment_key,
        current_source_sha256=current_identity.sha256,
        previous_attachment_key=(
            previous_identity.attachment_key if previous_identity else None
        ),
        previous_source_sha256=(
            previous_identity.sha256 if previous_identity else None
        ),
    )


def execute_import(
    plan: ImportPlan,
    overwrite: bool = False,
    backup_root: Path = Path("backups"),
) -> Path | None:
    destinations = [plan.note_destination, *[destination for _, destination in plan.assets]]
    # A file may appear after dry-run. Never trust the old conflict snapshot.
    conflicts = [p for p in destinations if p.exists()]
    if conflicts and not overwrite:
        raise ObsidianImportError(
            "import would overwrite existing files; review dry-run or pass overwrite=True"
        )
    if any(path.is_symlink() for path in destinations):
        raise ObsidianImportError("refusing to write through a symbolic-link destination")

    backup_dir: Path | None = None
    if conflicts and overwrite:
        backup_dir = backup_root / f"obsidian-import-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        for conflict in conflicts:
            if conflict.is_file():
                destination = backup_dir / conflict.relative_to(plan.vault)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(conflict, destination)

    plan.target_dir.mkdir(parents=True, exist_ok=True)
    plan.note_destination.write_text(plan.rewritten_markdown, encoding="utf-8")
    for source, destination in plan.assets:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return backup_dir
