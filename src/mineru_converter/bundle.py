from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".tif", ".tiff"}
MARKDOWN_IMAGE_RE = re.compile(r"(!\[[^\]]*\]\()([^\n]*?)(\))")
WIKILINK_IMAGE_RE = re.compile(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
HTML_IMAGE_RE = re.compile(r"(<img\b[^>]*?\bsrc=[\"'])(.*?)([\"'])", re.IGNORECASE)


class BundleError(RuntimeError):
    pass


def safe_document_stem(name: str) -> str:
    """Keep a source filename readable while making it safe as a folder name."""

    cleaned = re.sub(r'[\x00-\x1f/:\\*?"<>|]+', "_", name).strip(" ._")
    return (cleaned[:120] or "document").rstrip(" ._") or "document"


def _inside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def safe_extract_zip(zip_path: Path, destination: Path) -> list[Path]:
    """Extract a MinerU ZIP while rejecting absolute and traversal members."""

    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise BundleError(f"unsafe ZIP member: {info.filename}")
                target = (destination / member).resolve()
                if not _inside(destination, target):
                    raise BundleError(f"unsafe ZIP member: {info.filename}")
                # Reject Unix symlink entries instead of materializing them.
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise BundleError(f"symlink ZIP member is not allowed: {info.filename}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                extracted.append(target)
    except zipfile.BadZipFile as exc:
        raise BundleError(f"invalid result ZIP: {zip_path}") from exc
    return extracted


def find_full_markdown(root: Path) -> Path:
    candidates = sorted(root.rglob("full.md"), key=lambda path: (len(path.parts), str(path)))
    if not candidates:
        candidates = sorted(root.rglob("*.md"), key=lambda path: (len(path.parts), str(path)))
    if not candidates:
        raise BundleError("MinerU result ZIP contains no Markdown file")
    return candidates[0]


def _safe_asset_name(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    name = "".join(char for char in name if char.isprintable()).strip()
    return name or "image"


def _asset_destination(assets_dir: Path, source: Path, used: set[str]) -> Path:
    base = _safe_asset_name(source.name)
    candidate = assets_dir / base
    if candidate.name not in used:
        used.add(candidate.name)
        return candidate
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:10]
    candidate = assets_dir / f"{candidate.stem}-{digest}{candidate.suffix}"
    used.add(candidate.name)
    return candidate


def _collect_images(root: Path, assets_dir: Path) -> dict[Path, Path]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[Path, Path] = {}
    used: set[str] = set()
    for source in sorted(root.rglob("*")):
        if source.is_file() and source.suffix.lower() in IMAGE_EXTENSIONS:
            target = _asset_destination(assets_dir, source, used)
            shutil.copy2(source, target)
            mapping[source.resolve()] = target
    return mapping


def _reference_candidates(reference: str, markdown_path: Path, root: Path) -> list[Path]:
    clean = unquote(reference.strip().strip("<>"))
    parsed = urlparse(clean)
    if parsed.scheme in {"http", "https", "data"}:
        return []
    if parsed.scheme == "file":
        clean = unquote(parsed.path)
    clean = clean.split("#", 1)[0].split("?", 1)[0]
    relative = Path(clean)
    candidates = [markdown_path.parent / relative, root / relative]
    if relative.parts and relative.parts[0] in {"images", "assets"}:
        candidates.append(root / Path(*relative.parts[1:]))
    return [candidate.resolve() for candidate in candidates]


def _lookup_asset(
    reference: str,
    markdown_path: Path,
    root: Path,
    mapping: dict[Path, Path],
) -> Path | None:
    for candidate in _reference_candidates(reference, markdown_path, root):
        if candidate in mapping:
            return mapping[candidate]
    basename = Path(unquote(reference.strip().strip("<>").split("?", 1)[0])).name
    matches = [target for source, target in mapping.items() if source.name == basename]
    return matches[0] if len(matches) == 1 else None


def _is_external(reference: str) -> bool:
    return urlparse(reference.strip().strip("<>")).scheme in {"http", "https"}


def rewrite_standard_markdown(
    markdown: str,
    markdown_path: Path,
    root: Path,
    mapping: dict[Path, Path],
    assets_dir_name: str = "assets",
) -> tuple[str, list[str], list[str]]:
    missing: list[str] = []
    external: list[str] = []

    def output_reference(target: Path) -> str:
        reference = f"{assets_dir_name}/{target.name}"
        return f"<{reference}>" if any(char.isspace() for char in reference) else reference

    def replace_destination(reference: str, original: str) -> str:
        reference = reference.strip()
        if _is_external(reference):
            external.append(reference)
            return original
        target = _lookup_asset(reference, markdown_path, root, mapping)
        if target is None:
            missing.append(reference)
            return original
        return output_reference(target)

    def markdown_replacement(match: re.Match[str]) -> str:
        prefix, contents, suffix = match.groups()
        raw = contents.strip()
        if raw.startswith("<") and ">" in raw:
            close = raw.find(">")
            destination = raw[1:close]
            tail = raw[close + 1 :]
            rewritten = replace_destination(destination, contents)
            return f"{prefix}{rewritten}{tail}{suffix}"
        pieces = raw.split(None, 1)
        destination = pieces[0] if pieces else raw
        tail = f" {pieces[1]}" if len(pieces) == 2 else ""
        rewritten = replace_destination(destination, destination)
        return f"{prefix}{rewritten}{tail}{suffix}"

    result = MARKDOWN_IMAGE_RE.sub(markdown_replacement, markdown)

    def wikilink_replacement(match: re.Match[str]) -> str:
        reference = match.group(1).strip()
        target = _lookup_asset(reference, markdown_path, root, mapping)
        if target is None:
            missing.append(reference)
            return match.group(0)
        return f"![]({output_reference(target)})"

    result = WIKILINK_IMAGE_RE.sub(wikilink_replacement, result)

    def html_replacement(match: re.Match[str]) -> str:
        prefix, reference, suffix = match.groups()
        rewritten = replace_destination(reference, reference)
        return f"{prefix}{rewritten}{suffix}"

    result = HTML_IMAGE_RE.sub(html_replacement, result)
    return result, sorted(set(missing)), sorted(set(external))


def normalize_bundle(
    zip_path: Path,
    output_dir: Path,
    *,
    markdown_name: str = "full.md",
    assets_name: str = "assets",
    retain_raw: bool = True,
) -> dict[str, object]:
    """Extract a MinerU result and produce portable Markdown plus assets.

    ``retain_raw=True`` keeps the old direct-API behavior.  The normal
    conversion pipeline passes ``False`` and removes all intermediates after a
    successful task, leaving only the renamed Markdown and asset directory.
    """

    if Path(markdown_name).name != markdown_name or not markdown_name.lower().endswith(".md"):
        raise BundleError("markdown_name must be a single .md filename")
    if Path(assets_name).name != assets_name or not assets_name.strip():
        raise BundleError("assets_name must be a single directory name")

    output_dir.mkdir(parents=True, exist_ok=True)
    stored_zip: Path | None = None
    source_zip = zip_path
    if retain_raw:
        raw_dir = output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        stored_zip = raw_dir / "full.zip"
        if zip_path.resolve() != stored_zip.resolve():
            shutil.copy2(zip_path, stored_zip)
        source_zip = stored_zip

    with tempfile.TemporaryDirectory(prefix="mineru-bundle-") as temporary:
        extracted_root = Path(temporary)
        safe_extract_zip(source_zip, extracted_root)
        source_markdown = find_full_markdown(extracted_root)
        markdown = source_markdown.read_text(encoding="utf-8", errors="replace")
        assets_dir = output_dir / assets_name
        mapping = _collect_images(extracted_root, assets_dir)
        rewritten, missing, external = rewrite_standard_markdown(
            markdown,
            source_markdown,
            extracted_root,
            mapping,
            assets_dir_name=assets_name,
        )

    markdown_path = output_dir / markdown_name
    markdown_path.write_text(rewritten, encoding="utf-8")
    details: dict[str, object] = {
        "markdown_path": str(markdown_path),
        "assets_dir": str(assets_dir),
        "image_count": len(mapping),
        "missing_assets": missing,
        "external_images": external,
    }
    if stored_zip is not None:
        details["raw_zip"] = str(stored_zip)
    return details


def write_manifest(output_dir: Path, manifest: dict[str, object]) -> Path:
    path = output_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
