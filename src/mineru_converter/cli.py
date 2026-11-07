from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from .api import MineruAPIError, MineruClient
from .config import (
    MissingAPIKeyError,
    describe_environment,
    get_api_key,
    get_base_url,
    resolve_env_file,
    write_environment,
)
from .conversion_history import annotate_conversion_history
from .models import ParseOptions
from .obsidian import ObsidianImportError, create_import_plan, execute_import
from .paths import PathListParseError, build_source_records, scan_pdf_files, tokenize_pasted_paths
from .pipeline import convert_records, convert_url


def _options(args: argparse.Namespace) -> ParseOptions:
    return ParseOptions(
        model_version=args.model,
        is_ocr=args.ocr,
        enable_formula=not args.no_formula,
        enable_table=not args.no_table,
        language=args.language,
        page_ranges=args.page_ranges,
    )


def _client(args: argparse.Namespace) -> MineruClient:
    env_file = Path(args.env_file).expanduser() if args.env_file else None
    return MineruClient(get_api_key(env_file), base_url=get_base_url(env_file))


def _progress(message: str) -> None:
    print(f"[mineru] {message}", file=sys.stderr)


def _print_records(records: list[Any]) -> None:
    for index, record in enumerate(records, start=1):
        path = str(record.path) if record.path else "-"
        details = []
        if record.pages is not None:
            details.append(f"{record.pages} pages")
        if record.size is not None:
            details.append(f"{record.size / 1024 / 1024:.2f} MiB")
        if record.duplicate_of:
            details.append(f"duplicate of {record.duplicate_of}")
        if record.error:
            details.append(record.error)
        print(f"{index:>3}  {record.status:<9}  {path}  {'; '.join(details)}")


def _emit(value: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        if isinstance(value, list):
            for item in value:
                print(json.dumps(item, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(value, ensure_ascii=False, indent=2))


def _convert_records_command(args: argparse.Namespace, records: list[Any]) -> int:
    records = annotate_conversion_history(records, Path(args.output_root))
    if not args.json:
        _print_records(records)
    if args.dry_run:
        _emit([record.to_dict() for record in records], args.json)
        return 0
    client = _client(args)
    results = convert_records(
        client,
        records,
        options=_options(args),
        output_root=Path(args.output_root),
        serial=args.serial,
        progress=_progress,
    )
    _emit(results, args.json)
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    records = build_source_records([args.path])
    return _convert_records_command(args, records)


def cmd_batch(args: argparse.Namespace) -> int:
    directory = Path(args.directory).expanduser()
    if not directory.is_dir():
        raise ValueError(f"directory does not exist: {directory}")
    tokens = scan_pdf_files(directory, recursive=not args.no_recursive)
    records = build_source_records(tokens)
    return _convert_records_command(args, records)


def cmd_paste(args: argparse.Namespace) -> int:
    if args.input_file:
        raw = Path(args.input_file).expanduser().read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print("Paste one or more PDF paths, then press Ctrl-D:", file=sys.stderr)
        raw = sys.stdin.read()
    tokens = tokenize_pasted_paths(raw)
    if not tokens:
        raise ValueError("no paths were pasted")
    records = build_source_records(tokens)
    return _convert_records_command(args, records)


def cmd_url(args: argparse.Namespace) -> int:
    if args.dry_run:
        _emit({"url": args.url, "options": _options(args).to_payload()}, args.json)
        return 0
    result = convert_url(
        _client(args),
        args.url,
        options=_options(args),
        output_root=Path(args.output_root),
        progress=_progress,
    )
    _emit(result, args.json)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    plan = create_import_plan(
        Path(args.result_dir),
        Path(args.vault),
        args.target,
        note_name=args.note_name,
    )
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    if not args.apply:
        print("Dry-run only. Re-run with --apply after reviewing the plan.", file=sys.stderr)
        return 0
    backup = execute_import(plan, overwrite=args.overwrite, backup_root=Path(args.backup_root))
    if backup:
        print(f"Backup created at: {backup}", file=sys.stderr)
    print(f"Imported: {plan.note_destination}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui

    if args.citation_key and len(args.paths) != 1:
        raise ValueError("--citation-key requires exactly one PDF")
    queue = []
    if args.queue_file:
        queue = json.loads(Path(args.queue_file).read_text(encoding="utf-8"))
        if not isinstance(queue, list) or any(not isinstance(x, dict) or not isinstance(x.get("path"), str) for x in queue):
            raise ValueError("queue file must contain local PDF records")
    run_gui(
        paths=args.paths, vault=args.vault, target=args.target,
        queue_items=queue,
        source_metadata={"citation_key": args.citation_key, "zotero_item_key": args.zotero_item_key,
                         "zotero_attachment_key": args.zotero_attachment_key, "obsidian_note": args.source_note},
    )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from .document_roles import inspect_pdf
    print(json.dumps([inspect_pdf(Path(p)) for p in args.paths], ensure_ascii=False))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser() if args.env_file else None
    status = describe_environment(env_file)
    _emit(status, args.json)
    return 0 if status["api_key_configured"] else 1


def cmd_init(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser() if args.env_file else None
    print("请输入已在 MinerU 控制台轮换后的新 API Key（输入时不会回显）：", file=sys.stderr)
    entered_key = getpass.getpass("")
    path = write_environment(entered_key, env_file=env_file, base_url=args.base_url)
    print(f"已创建私密配置文件：{path}")
    print("文件权限已设为 600；请运行 doctor 检查，不要把 Key 写入命令行或提交到 Git。", file=sys.stderr)
    return 0


def _add_conversion_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", choices=["vlm", "pipeline"], default="vlm")
    parser.add_argument("--ocr", action="store_true", help="enable OCR")
    parser.add_argument("--no-formula", action="store_true")
    parser.add_argument("--no-table", action="store_true")
    parser.add_argument("--language", default="ch")
    parser.add_argument("--page-ranges")
    parser.add_argument("--output-root", default="outputs")
    parser.add_argument("--env-file")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert PDFs with MinerU and prepare Obsidian imports")
    parser.add_argument("--version", action="version", version="0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert = subparsers.add_parser("convert", help="convert one PDF")
    convert.add_argument("path")
    convert.add_argument("--serial", action="store_true")
    _add_conversion_options(convert)
    convert.set_defaults(func=cmd_convert)

    batch = subparsers.add_parser("batch", help="convert all PDFs in a directory")
    batch.add_argument("directory")
    batch.add_argument("--no-recursive", action="store_true")
    batch.add_argument("--serial", action="store_true")
    _add_conversion_options(batch)
    batch.set_defaults(func=cmd_batch)

    paste = subparsers.add_parser("paste", help="read one or more pasted PDF paths")
    paste.add_argument("--input-file")
    paste_mode = paste.add_mutually_exclusive_group()
    paste_mode.add_argument("--batch", dest="serial", action="store_false", help="submit valid paths in batches")
    paste_mode.add_argument("--serial", dest="serial", action="store_true", help="submit one path at a time")
    _add_conversion_options(paste)
    paste.set_defaults(func=cmd_paste, serial=False)

    url = subparsers.add_parser("url", help="convert a public document URL")
    url.add_argument("url")
    _add_conversion_options(url)
    url.set_defaults(func=cmd_url)

    importer = subparsers.add_parser("import", help="dry-run or apply an Obsidian import")
    importer.add_argument("result_dir")
    importer.add_argument("--vault", required=True)
    importer.add_argument("--target", required=True, help="directory inside the vault")
    importer.add_argument("--note-name")
    importer.add_argument("--apply", action="store_true")
    importer.add_argument("--overwrite", action="store_true")
    importer.add_argument("--backup-root", default="backups")
    importer.set_defaults(func=cmd_import)

    gui = subparsers.add_parser("gui", help="open the file and pasted-path GUI")
    gui.add_argument("paths", nargs="*", help="queue local PDFs without uploading")
    gui.add_argument("--vault")
    gui.add_argument("--target")
    gui.add_argument("--citation-key")
    gui.add_argument("--zotero-item-key")
    gui.add_argument("--zotero-attachment-key")
    gui.add_argument("--source-note")
    gui.add_argument("--queue-file")
    gui.set_defaults(func=cmd_gui)

    inspector = subparsers.add_parser("inspect", help="inspect local attachment roles without uploading")
    inspector.add_argument("paths", nargs="+")
    inspector.set_defaults(func=cmd_inspect)

    doctor = subparsers.add_parser("doctor", help="show safe local configuration diagnostics")
    doctor.add_argument("--env-file")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    init = subparsers.add_parser("init", help="create a 0600 .env file using a hidden prompt")
    init.add_argument("--env-file")
    init.add_argument("--base-url", default="https://mineru.net")
    init.set_defaults(func=cmd_init)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(args.func(args))
    except (MineruAPIError, MissingAPIKeyError, PathListParseError, ObsidianImportError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
