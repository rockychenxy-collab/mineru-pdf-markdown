from __future__ import annotations

import inspect
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .api import MineruAPIError, MineruClient, progress_percent
from .bundle import BundleError, safe_document_stem, write_manifest
from .models import ParseOptions, SourceRecord, TaskResult
from .literature import source_frontmatter
from .document_roles import document_stem


ProgressCallback = Callable[[str], None]
DetailProgressCallback = Callable[[dict[str, Any]], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def output_slug(record: SourceRecord) -> str:
    name = output_name_stem(record)
    stem = re.sub(
        r"[^\w.-]+",
        "-",
        safe_document_stem(name),
        flags=re.UNICODE,
    ).strip("-._") or "document"
    digest = (record.sha256 or "unknown")[:8]
    return f"{stem[:100]}-{digest}"


def output_name_stem(record: SourceRecord) -> str:
    """Return the user-facing document stem.

    Zotero-imported records carry Better BibTeX's internal citation key.  It
    is more stable and more useful for later citation than the attachment's
    often-truncated storage filename.  Pasted paths keep their old behavior.
    """

    if record.citation_key:
        return safe_document_stem(document_stem(record.citation_key, record.document_role, record.zotero_attachment_key, record.sha256))
    return safe_document_stem(record.path.stem)


def output_markdown_name(record: SourceRecord) -> str:
    return f"{output_name_stem(record)}.md"


def output_assets_name(record: SourceRecord) -> str:
    return f"{output_name_stem(record)}_assets"


def unique_output_dir(root: Path, record: SourceRecord) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = root / output_slug(record)
    if not base.exists():
        return base
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = root / f"{base.name}-run-{stamp}"
    counter = 2
    while candidate.exists():
        candidate = root / f"{base.name}-run-{stamp}-{counter}"
        counter += 1
    return candidate


def _base_manifest(record: SourceRecord, options: ParseOptions, output_dir: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": _now(),
        "source": record.to_dict(),
        "options": options.to_payload(),
        "output_dir": str(output_dir),
        "status": "submitted",
        "batch_id": None,
        "task_id": None,
        "error": None,
    }


def _match_record(result: TaskResult, records: list[SourceRecord], used: set[int]) -> tuple[int, SourceRecord] | None:
    for index, record in enumerate(records):
        if index not in used and result.data_id and result.data_id == record.data_id:
            return index, record
    if result.data_id:
        return None
    candidates = [(i, r) for i, r in enumerate(records) if i not in used and r.path.name == result.file_name]
    if len(candidates) == 1:
        return candidates[0]
    remaining = [(i, r) for i, r in enumerate(records) if i not in used]
    if not result.file_name and len(remaining) == 1:
        return remaining[0]
    return None


def _accepts_keyword(method: Callable[..., Any], keyword: str) -> bool:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return True
    return keyword in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _poll_batch(
    client: MineruClient,
    batch_id: str,
    progress_callback: Callable[[list[TaskResult]], None] | None = None,
) -> list[TaskResult]:
    poll_method = client.poll_batch
    if progress_callback and _accepts_keyword(poll_method, "progress_callback"):
        return poll_method(batch_id, progress_callback=progress_callback)
    return poll_method(batch_id)


def _normalize_bundle(
    client: MineruClient,
    zip_path: Path,
    output_dir: Path,
    markdown_name: str,
    assets_name: str,
) -> dict[str, object]:
    """Use renamed-output support while remaining compatible with old fakes."""

    normalize_method = client.normalize_bundle
    if _accepts_keyword(normalize_method, "markdown_name"):
        return normalize_method(
            zip_path,
            output_dir,
            markdown_name=markdown_name,
            assets_name=assets_name,
            retain_raw=False,
        )

    details = normalize_method(zip_path, output_dir)
    legacy_markdown = output_dir / "full.md"
    target_markdown = output_dir / markdown_name
    if legacy_markdown != target_markdown and legacy_markdown.is_file():
        legacy_markdown.replace(target_markdown)
    legacy_assets = output_dir / "assets"
    target_assets = output_dir / assets_name
    if legacy_assets != target_assets and legacy_assets.is_dir():
        legacy_assets.replace(target_assets)
        if target_markdown.is_file():
            text = target_markdown.read_text(encoding="utf-8", errors="replace")
            target_markdown.write_text(text.replace("assets/", f"{assets_name}/"), encoding="utf-8")
    details["markdown_path"] = str(target_markdown)
    details["assets_dir"] = str(target_assets)
    return details


def _remove_success_intermediates(output_dir: Path) -> None:
    """Leave only the final renamed Markdown and assets directory."""

    for child in output_dir.iterdir():
        if child.name == "manifest.json" or child.name == "raw" or child.name.startswith(".mineru-full.zip"):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)


def _process_result(
    client: MineruClient,
    result: TaskResult,
    record: SourceRecord,
    options: ParseOptions,
    output_root: Path,
    progress: ProgressCallback | None = None,
    detail_progress: DetailProgressCallback | None = None,
    progress_start: float = 0.0,
    progress_done: float = 100.0,
) -> dict[str, object]:
    output_dir = unique_output_dir(output_root, record)
    manifest = _base_manifest(record, options, output_dir)
    manifest["state"] = result.state
    manifest["batch_id"] = None
    manifest["task_id"] = result.task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    if result.state != "done" or not result.full_zip_url:
        manifest["status"] = "failed"
        manifest["error"] = result.error or "MinerU did not return a completed result"
        return manifest

    zip_path = output_dir / ".mineru-full.zip"
    try:
        if progress:
            progress(f"downloading {record.path.name}")
        if detail_progress:
            detail_progress(
                {
                    "stage": "downloading",
                    "percent": progress_start,
                    "message": f"正在下载 {record.path.name} 的 MinerU 结果",
                }
            )
        client.download_result(result, zip_path)
        if progress:
            progress(f"normalizing {record.path.name}")
        if detail_progress:
            detail_progress(
                {
                    "stage": "normalizing",
                    "percent": progress_start + (progress_done - progress_start) * 0.5,
                    "message": f"正在整理 {record.path.name} 的 Markdown 和图片",
                }
            )
        details = _normalize_bundle(
            client,
            zip_path,
            output_dir,
            markdown_name=output_markdown_name(record),
            assets_name=output_assets_name(record),
        )
        manifest.update(details)
        if record.citation_key or record.zotero_attachment_key:
            markdown_path = output_dir / output_markdown_name(record)
            markdown_path.write_text(
                source_frontmatter(markdown_path.read_text(encoding="utf-8"), record.to_dict()),
                encoding="utf-8",
            )
        manifest["status"] = "done"
        manifest["completed_at"] = _now()
        _remove_success_intermediates(output_dir)
        if detail_progress:
            detail_progress(
                {
                    "stage": "done",
                    "percent": progress_done,
                    "message": f"已完成 {record.path.name}",
                }
            )
    except (MineruAPIError, BundleError, OSError) as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
    finally:
        zip_path.unlink(missing_ok=True)
    return manifest


def convert_records(
    client: MineruClient,
    records: Iterable[SourceRecord],
    options: ParseOptions | None = None,
    output_root: Path = Path("outputs"),
    serial: bool = False,
    progress: ProgressCallback | None = None,
    progress_detail: DetailProgressCallback | None = None,
) -> list[dict[str, object]]:
    """Submit valid unique records in one-file or up-to-50-file batches."""

    options = options or ParseOptions()
    all_records = list(records)
    valid = [record for record in all_records if record.valid]
    if not valid:
        raise ValueError("no valid PDFs available for conversion")

    results: list[dict[str, object]] = []
    chunk_size = 1 if serial else 50
    total = len(valid)
    completed = 0
    for start in range(0, len(valid), chunk_size):
        chunk = valid[start : start + chunk_size]
        used: set[int] = set()
        if progress:
            progress(f"submitting {len(chunk)} PDF(s)")
        if progress_detail:
            progress_detail(
                {
                    "stage": "uploading",
                    "percent": completed / total * 100,
                    "completed": completed,
                    "total": total,
                    "message": f"正在提交第 {completed + 1}-{completed + len(chunk)} 个 PDF",
                }
            )
        submission = client.submit_batch(chunk, options=options, upload_concurrency=1 if serial else 2)

        def on_task_progress(task_results: list[TaskResult]) -> None:
            if not progress_detail or not task_results:
                return
            values: list[float] = []
            mineru_available = False
            for task_result in task_results:
                value = progress_percent(task_result.progress)
                if value is not None:
                    mineru_available = True
                if task_result.state in {"done", "failed"}:
                    value = 100.0
                if value is not None:
                    values.append(value / 100)
            if not values:
                progress_detail(
                    {
                        "stage": "extracting",
                        "completed": completed,
                        "total": total,
                        "mineru_progress": False,
                        "message": f"MinerU 正在解析（已完成 {completed}/{total}）",
                    }
                )
                return
            overall = (completed + sum(values)) / total * 100
            finished = sum(task.state in {"done", "failed"} for task in task_results)
            progress_detail(
                {
                    "stage": "extracting",
                    "percent": max(0.0, min(100.0, overall)),
                    "completed": completed + finished,
                    "total": total,
                    "mineru_progress": mineru_available,
                    "message": f"MinerU 正在解析（当前批次完成 {finished}/{len(chunk)}）",
                }
            )

        task_results = _poll_batch(client, submission.batch_id, on_task_progress if progress_detail else None)
        processed_in_chunk = 0
        for task_result in task_results:
            match = _match_record(task_result, chunk, used)
            if match is None:
                continue
            index, record = match
            used.add(index)
            item = _process_result(
                client,
                task_result,
                record,
                options,
                output_root,
                progress,
                progress_detail,
                progress_start=completed / total * 100,
                progress_done=(completed + 1) / total * 100,
            )
            item["batch_id"] = submission.batch_id
            output_dir = Path(str(item["output_dir"]))
            if item.get("status") != "done":
                write_manifest(output_dir, item)
            results.append(item)
            processed_in_chunk += 1
            completed += 1

        if processed_in_chunk < len(task_results):
            completed += len(task_results) - processed_in_chunk
        for index, record in enumerate(chunk):
            if index not in used:
                results.append({"schema_version": 1, "status": "failed", "source": record.to_dict(),
                                "error": "MinerU 未返回可唯一对应的结果ID；同名附件请使用逐个转换，不能猜测正文与补充材料的对应关系"})

    for record in all_records:
        if record.status == "duplicate":
            results.append(
                {
                    "schema_version": 1,
                    "status": "duplicate",
                    "source": record.to_dict(),
                    "error": f"duplicate of {record.duplicate_of}",
                }
            )
        elif not record.valid:
            results.append(
                {
                    "schema_version": 1,
                    "status": "invalid",
                    "source": record.to_dict(),
                    "error": record.error or record.status,
                }
            )
    if progress_detail:
        progress_detail(
            {
                "stage": "done",
                "percent": 100.0,
                "completed": total,
                "total": total,
                "mineru_progress": True,
                "message": f"全部完成：{total} 个 PDF",
            }
        )
    return results


def convert_url(
    client: MineruClient,
    url: str,
    options: ParseOptions | None = None,
    output_root: Path = Path("outputs"),
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    options = options or ParseOptions()
    if progress:
        progress(f"submitting URL {url}")
    task_id = client.submit_url(url, options=options)
    result = client.poll_task(task_id)
    fake_path = Path(url.rsplit("/", 1)[-1] or "remote-document.pdf")
    record = SourceRecord(
        token=url,
        path=fake_path,
        status="valid",
        data_id=fake_path.stem,
    )
    return _process_result(client, result, record, options, output_root, progress)
