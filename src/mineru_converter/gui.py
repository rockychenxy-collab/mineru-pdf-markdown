from __future__ import annotations

import queue
import os
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .api import MineruClient
from .config import get_api_key, get_base_url
from .conversion_history import annotate_conversion_history, history_label
from .models import ParseOptions, SourceRecord
from .obsidian import ObsidianImportError, ImportPlan, create_import_plan, execute_import
from .paths import build_source_records, tokenize_pasted_paths
from .pipeline import convert_records
from .settings import load_gui_settings, save_gui_settings
from .literature import literature_target
from .document_roles import ROLES
from .pipeline import output_name_stem
from .zotero import (
    ZoteroPDFItem,
    ZoteroPDFSelection,
    ZoteroReadError,
    default_zotero_db_path,
    default_zotero_storage_path,
    get_current_collection,
    list_pdf_items,
    records_from_zotero_selections,
)
from .zotero_picker import ZoteroPicker


def open_desktop_uri(uri: str) -> None:
    if sys.platform == "win32":
        os.startfile(uri)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", uri],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _record_path_key(record: SourceRecord) -> str | None:
    """Return a stable absolute-path key for queue-level de-duplication."""

    if record.path is None:
        return None
    return str(record.path.expanduser().resolve(strict=False))


def clone_source_records(records: Iterable[SourceRecord]) -> list[SourceRecord]:
    """Copy records before handing them to the background conversion thread."""

    return [replace(record, aliases=list(record.aliases)) for record in records]


def reconcile_records(records: Iterable[SourceRecord]) -> list[SourceRecord]:
    """Rebuild path and SHA-256 relationships without mutating the input list."""

    result: list[SourceRecord] = []
    seen_paths: set[str] = set()
    first_by_digest: dict[str, SourceRecord] = {}

    for original in records:
        record = replace(original, aliases=[])
        path_key = _record_path_key(record)
        if path_key is not None and path_key in seen_paths:
            continue
        if path_key is not None:
            seen_paths.add(path_key)

        if record.status in {"valid", "duplicate"} and record.sha256 and record.path is not None:
            first = first_by_digest.get(record.sha256)
            if first is None:
                record.status = "valid"
                record.duplicate_of = None
                first_by_digest[record.sha256] = record
            else:
                record.status = "duplicate"
                record.duplicate_of = str(first.path)
                first.aliases.append(str(record.path))
        elif record.status != "duplicate":
            record.duplicate_of = None
        result.append(record)
    return result


def merge_source_records(
    existing: Sequence[SourceRecord], incoming: Iterable[SourceRecord]
) -> list[SourceRecord]:
    """Append newly parsed records while retaining the existing queue."""

    merged = list(existing)
    seen_paths = {
        path_key
        for record in merged
        if (path_key := _record_path_key(record)) is not None
    }
    for record in incoming:
        path_key = _record_path_key(record)
        if path_key is not None and path_key in seen_paths:
            continue
        if path_key is not None:
            seen_paths.add(path_key)
        merged.append(record)
    return reconcile_records(merged)


def remove_record_indices(
    records: Sequence[SourceRecord], indices: Iterable[int]
) -> list[SourceRecord]:
    """Remove selected queue rows and repair duplicate relationships."""

    selected = set(indices)
    return reconcile_records(record for index, record in enumerate(records) if index not in selected)


class ConverterGUI:
    def __init__(self, root: tk.Tk, *, paths: Sequence[str] = (), vault: str | None = None,
                 target: str | None = None, source_metadata: dict[str, str | None] | None = None,
                 queue_items: Sequence[dict] = ()) -> None:
        self.root = root
        self.root.title("MinerU PDF → Markdown")
        self.root.geometry("1220x850")
        self.settings = load_gui_settings()
        if vault:
            self.settings["obsidian_vault"] = vault
        if target:
            self.settings["obsidian_target"] = target
        self.initial_source_metadata = source_metadata or {}
        self.records: list[SourceRecord] = []
        self._last_results: list[dict[str, object]] = []
        self._conversion_active = False
        self._busy_widgets: list[tk.Misc] = []
        self._import_buttons: list[ttk.Button] = []
        self._progress_indeterminate = False
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._build()
        for item in queue_items:
            incoming = build_source_records([item["path"]])
            for record in incoming:
                for name in ["citation_key", "zotero_item_key", "zotero_attachment_key", "zotero_uri", "obsidian_note", "document_role", "role_source", "attachment_title"]:
                    if isinstance(item.get(name), str):
                        setattr(record, name, item[name])
                if record.document_role not in {*ROLES, None}:
                    raise ValueError("invalid document role in queue")
            self.records = merge_source_records(self.records, incoming)
        if paths:
            incoming = build_source_records(paths)
            if len(incoming) == 1:
                for key, value in self.initial_source_metadata.items():
                    if value and hasattr(incoming[0], key):
                        setattr(incoming[0], key, value)
            self.records = merge_source_records(self.records, incoming)
        self._refresh_conversion_history()
        if queue_items or paths:
            self._refresh_tree()
        self._set_import_buttons(False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._drain_events)

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="both", expand=True)

        ttk.Label(top, text="粘贴一个或多个 PDF 路径（可多次追加）：").pack(anchor="w")
        self.input = tk.Text(top, height=6, wrap="none")
        self.input.pack(fill="x", pady=(4, 8))

        controls = ttk.Frame(top)
        controls.pack(fill="x", pady=(0, 8))
        self.add_button = ttk.Button(controls, text="添加文件", command=self.add_files)
        self.add_button.pack(side="left")
        self.parse_button = ttk.Button(controls, text="追加解析", command=self.parse_paths)
        self.parse_button.pack(side="left", padx=5)
        self.open_zotero_button = ttk.Button(controls, text="打开 Zotero", command=self.open_zotero)
        self.open_zotero_button.pack(side="left", padx=(0, 5))
        self.zotero_button = ttk.Button(controls, text="读取当前 Zotero 文件夹", command=self.choose_from_zotero)
        self.zotero_button.pack(side="left", padx=(0, 5))
        self.choose_output_button = ttk.Button(controls, text="选择输出目录", command=self.choose_output)
        self.choose_output_button.pack(side="left")
        self.batch_button = ttk.Button(controls, text="批量转换", command=lambda: self.convert(False))
        self.batch_button.pack(side="left", padx=5)
        self.serial_button = ttk.Button(controls, text="逐个转换", command=lambda: self.convert(True))
        self.serial_button.pack(side="left")

        self.output_var = tk.StringVar(value=self.settings.get("output_root", "outputs"))
        ttk.Label(controls, text="输出:").pack(side="left", padx=(20, 4))
        self.output_entry = ttk.Entry(controls, textvariable=self.output_var, width=38)
        self.output_entry.pack(side="left")

        options = ttk.Frame(top)
        options.pack(fill="x", pady=(0, 6))
        self.model_var = tk.StringVar(value="vlm")
        self.language_var = tk.StringVar(value="ch")
        self.ocr_var = tk.BooleanVar(value=False)
        ttk.Label(options, text="模型").pack(side="left")
        self.model_combo = ttk.Combobox(
            options,
            textvariable=self.model_var,
            values=["vlm", "pipeline"],
            width=10,
            state="readonly",
        )
        self.model_combo.pack(side="left", padx=4)
        ttk.Label(options, text="语言").pack(side="left", padx=(12, 0))
        self.language_entry = ttk.Entry(options, textvariable=self.language_var, width=8)
        self.language_entry.pack(side="left", padx=4)
        self.ocr_check = ttk.Checkbutton(options, text="OCR", variable=self.ocr_var)
        self.ocr_check.pack(side="left", padx=12)

        progress_frame = ttk.Frame(top)
        progress_frame.pack(fill="x", pady=(0, 8))
        self.progressbar = ttk.Progressbar(progress_frame, maximum=100, mode="determinate")
        self.progressbar.pack(side="left", fill="x", expand=True)
        self.progress_state_var = tk.StringVar(value="就绪")
        ttk.Label(progress_frame, textvariable=self.progress_state_var, width=38).pack(side="left", padx=(10, 0))

        columns = ("status", "name", "path", "pages", "size", "details")
        table_frame = ttk.Frame(top)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=15,
            selectmode="extended",
        )
        headings = {"status": "状态", "name": "文件名", "path": "路径", "pages": "页数", "size": "大小", "details": "说明"}
        widths = {"status": 90, "name": 230, "path": 430, "pages": 65, "size": 90, "details": 220}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.tag_configure("duplicate", foreground="#936c00")
        self.tree.tag_configure("history-changed", foreground="#b35c00")
        self.tree.tag_configure("history-same", foreground="#2f6f4e")
        self.tree.tag_configure("invalid", foreground="#a33a3a")
        self.tree.bind("<Delete>", self._delete_selected_event)
        self.tree.bind("<BackSpace>", self._delete_selected_event)

        list_controls = ttk.Frame(top)
        list_controls.pack(fill="x", pady=(8, 0))
        self.delete_button = ttk.Button(list_controls, text="移除选中（不删除源文件）", command=self.delete_selected)
        self.delete_button.pack(side="left")
        self.clear_button = ttk.Button(list_controls, text="清空列表", command=self.clear_records)
        self.clear_button.pack(side="left", padx=5)
        for role in ["main", "supplement", "other"]:
            button = ttk.Button(list_controls, text="设为" + ROLES[role], command=lambda value=role: self.set_selected_role(value))
            button.pack(side="left", padx=2)
            self._busy_widgets.append(button)
        ttk.Label(list_controls, text="可按住 ⌘/Shift/Ctrl 多选，Delete 或 Backspace 移除队列项。").pack(side="left", padx=8)

        obsidian = ttk.LabelFrame(top, text="导入 Obsidian（转换完成后）", padding=6)
        obsidian.pack(fill="x", pady=(8, 0))
        vault_row = ttk.Frame(obsidian)
        vault_row.pack(fill="x", pady=(0, 4))
        ttk.Label(vault_row, text="Vault:", width=9).pack(side="left")
        default_vault = self.settings.get("obsidian_vault", str(Path.home()))
        self.obsidian_vault_var = tk.StringVar(value=default_vault)
        self.obsidian_vault_entry = ttk.Entry(vault_row, textvariable=self.obsidian_vault_var)
        self.obsidian_vault_entry.pack(side="left", fill="x", expand=True)
        self.choose_vault_button = ttk.Button(vault_row, text="选择库", command=self.choose_obsidian_vault)
        self.choose_vault_button.pack(side="left", padx=(5, 0))

        target_row = ttk.Frame(obsidian)
        target_row.pack(fill="x", pady=(0, 4))
        ttk.Label(target_row, text="笔记目录:", width=9).pack(side="left")
        self.obsidian_target_var = tk.StringVar(value=self.settings.get("obsidian_target", "10_Literature/全文"))
        self.obsidian_target_entry = ttk.Entry(target_row, textvariable=self.obsidian_target_var)
        self.obsidian_target_entry.pack(side="left", fill="x", expand=True)
        self.choose_target_button = ttk.Button(target_row, text="选择目录", command=self.choose_obsidian_target)
        self.choose_target_button.pack(side="left", padx=(5, 0))

        import_row = ttk.Frame(obsidian)
        import_row.pack(fill="x")
        self.import_selected_button = ttk.Button(import_row, text="导入选中结果", command=self.import_selected_to_obsidian)
        self.import_selected_button.pack(side="left")
        self.import_all_button = ttk.Button(import_row, text="导入全部成功结果", command=self.import_all_to_obsidian)
        self.import_all_button.pack(side="left", padx=5)
        ttk.Label(import_row, text="导入前预览；Zotero 全文使用“__全文”文件名，保留文献笔记。").pack(side="left", padx=8)
        self._import_buttons = [self.import_selected_button, self.import_all_button]

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(top, textvariable=self.status_var).pack(anchor="w", pady=(8, 0))
        self._busy_widgets += [
            self.input,
            self.add_button,
            self.parse_button,
            self.open_zotero_button,
            self.zotero_button,
            self.choose_output_button,
            self.batch_button,
            self.serial_button,
            self.output_entry,
            self.model_combo,
            self.language_entry,
            self.ocr_check,
            self.tree,
            self.delete_button,
            self.clear_button,
            self.obsidian_vault_entry,
            self.choose_vault_button,
            self.obsidian_target_entry,
            self.choose_target_button,
        ]

    def _settings_values(self) -> dict[str, str]:
        return {
            "output_root": self.output_var.get().strip(),
            "obsidian_vault": self.obsidian_vault_var.get().strip(),
            "obsidian_target": self.obsidian_target_var.get().strip(),
        }

    def _save_settings(self) -> None:
        try:
            save_gui_settings(self._settings_values())
        except OSError:
            pass

    def _on_close(self) -> None:
        self._save_settings()
        self.root.destroy()

    def add_files(self) -> None:
        if self._conversion_active:
            return
        paths = filedialog.askopenfilenames(title="选择 PDF", filetypes=[("PDF", "*.pdf"), ("All files", "*")])
        if paths:
            current = self.input.get("1.0", "end-1c")
            addition = "\n".join(f'"{path}"' for path in paths)
            if current.strip():
                self.input.insert("end", "\n")
            self.input.insert("end", addition)
            self.parse_paths()

    def choose_output(self) -> None:
        if self._conversion_active:
            return
        directory = filedialog.askdirectory(title="选择输出目录")
        if directory:
            self.output_var.set(directory)
            self._refresh_conversion_history()
            self._refresh_tree()
            self._save_settings()

    def _refresh_conversion_history(self) -> None:
        root_text = self.output_var.get().strip()
        output_root = Path(root_text).expanduser() if root_text else None
        self.records = annotate_conversion_history(self.records, output_root)

    def _history_summary(self) -> str:
        same = sum(
            record.history_status in {"unchanged", "legacy-same", "existing"}
            for record in self.records
        )
        changed = sum(record.history_status == "changed" for record in self.records)
        parts = []
        if same:
            parts.append(f"已转换 {same}")
        if changed:
            parts.append(f"附件已变化 {changed}")
        return "；历史：" + "，".join(parts) if parts else ""

    def parse_paths(self) -> bool:
        if self._conversion_active:
            return False
        text = self.input.get("1.0", "end-1c")
        if not text.strip():
            self.status_var.set(f"没有待解析路径；当前列表有 {len(self.records)} 个文件")
            return True
        try:
            tokens = tokenize_pasted_paths(text)
            incoming = build_source_records(tokens)
        except Exception as exc:
            messagebox.showerror("路径解析失败", str(exc))
            return False

        before = len(self.records)
        self.records = merge_source_records(self.records, incoming)
        self._refresh_conversion_history()
        self.input.delete("1.0", "end")
        self._refresh_tree()
        added = len(self.records) - before
        skipped = len(incoming) - added
        if added:
            self._last_results = []
            self._set_import_buttons(False)
        suffix = f"，跳过 {skipped} 个已在列表中的路径" if skipped else ""
        self.status_var.set(
            f"追加解析 {len(incoming)} 个路径，新增 {added} 个；"
            f"列表共 {len(self.records)} 个{suffix}{self._history_summary()}"
        )
        return True

    def _open_zotero(self) -> None:
        """Bring Zotero to the foreground without changing its data."""

        try:
            if sys.platform == "darwin":
                subprocess.run(["open", "-a", "Zotero"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                open_desktop_uri("zotero://select")
        except OSError as exc:
            messagebox.showerror("无法打开 Zotero", str(exc))

    def open_zotero(self) -> None:
        if self._conversion_active:
            return
        self._open_zotero()
        self.status_var.set("请在 Zotero 左侧选中一个 collection/文件夹，然后点击“读取当前 Zotero 文件夹”")

    def _locate_zotero_item(self, item: ZoteroPDFItem) -> None:
        self._open_zotero()
        try:
            open_desktop_uri(item.zotero_uri)
        except OSError as exc:
            messagebox.showerror("无法定位 Zotero 条目", str(exc))

    def choose_from_zotero(self) -> None:
        if self._conversion_active:
            return
        database = default_zotero_db_path()
        try:
            current = get_current_collection()
        except ZoteroReadError as exc:
            messagebox.showerror("读取 Zotero 当前文件夹失败", str(exc))
            return
        if current.is_library:
            messagebox.showwarning(
                "请先选择 Zotero 文件夹",
                f"当前选中的是“{current.name}”，不是具体文件夹。\n\n"
                "请先点击左侧的 collection/文件夹，再回到本程序点击“读取当前 Zotero 文件夹”。",
            )
            return
        try:
            items = list_pdf_items(
                database,
                default_zotero_storage_path(database),
                collection_id=current.collection_id,
            )
        except ZoteroReadError as exc:
            messagebox.showerror("读取 Zotero 失败", str(exc))
            return
        if not items:
            messagebox.showinfo("当前文件夹没有 PDF", f"Zotero 文件夹“{current.name}”中没有找到 PDF 附件。")
            return
        ZoteroPicker(
            self.root,
            items,
            self._add_zotero_selections,
            self._locate_zotero_item,
            scope_label=f"Zotero 文件夹“{current.name}”",
        )

    def _add_zotero_selections(self, selections: list[ZoteroPDFSelection]) -> None:
        incoming = records_from_zotero_selections(selections)
        before = len(self.records)
        self.records = merge_source_records(self.records, incoming)
        self._refresh_conversion_history()
        self._refresh_tree()
        added = len(self.records) - before
        skipped = len(incoming) - added
        self._last_results = []
        self._set_import_buttons(False)
        citation_keys = sorted(
            {
                selection.item.citation_key
                for selection in selections
                if selection.item.citation_key
            }
        )
        citation_hint = f"；引用关键词：{', '.join(citation_keys[:3])}" if citation_keys else ""
        if citation_keys:
            vault = Path(self.obsidian_vault_var.get()).expanduser()
            self.obsidian_target_var.set(literature_target(vault, citation_keys))
        suffix = f"；跳过 {skipped} 个重复路径" if skipped else ""
        self.status_var.set(
            f"从 Zotero 追加 {added} 个 PDF，列表共 {len(self.records)} 个"
            f"{suffix}{citation_hint}{self._history_summary()}"
        )

    def _refresh_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for record in self.records:
            details: list[str] = []
            if record.error:
                details.append(record.error)
            if record.duplicate_of:
                details.append(f"重复：{record.duplicate_of}")
            if record.citation_key:
                details.append(f"引用关键词：{record.citation_key}")
            if record.zotero_item_key:
                details.append(f"Zotero：{record.zotero_item_key}")
            if record.citation_key:
                details.append("类型：" + ROLES.get(record.document_role, "正文（旧格式）"))
                details.append("输出：" + output_name_stem(record))
            label = history_label(record.history_status)
            if label:
                details.append("历史：" + label)
                if record.history_output_dir:
                    details.append("旧结果：" + record.history_output_dir)
            tags: tuple[str, ...] = ()
            if record.status in {"duplicate", "invalid"}:
                tags = (record.status,)
            elif record.history_status == "changed":
                tags = ("history-changed",)
            elif record.history_status:
                tags = ("history-same",)
            display_status = record.status
            if record.status == "valid" and record.history_status == "changed":
                display_status = "已变化"
            elif record.status == "valid" and record.history_status:
                display_status = "已转换"
            self.tree.insert(
                "",
                "end",
                values=(
                    display_status,
                    record.path.name if record.path else "-",
                    str(record.path or "-"),
                    record.pages or "-",
                    f"{record.size / 1024 / 1024:.2f} MiB" if record.size is not None else "-",
                    "; ".join(details),
                ),
                tags=tags,
            )

    def _delete_selected_event(self, _event: tk.Event[tk.Misc]) -> str:
        self.delete_selected()
        return "break"

    def delete_selected(self) -> None:
        if self._conversion_active:
            return
        children = self.tree.get_children()
        selected = set(self.tree.selection())
        indices = [index for index, item in enumerate(children) if item in selected]
        if not indices:
            self.status_var.set("请先在列表中选择要移除的文件")
            return
        self.records = remove_record_indices(self.records, indices)
        self._last_results = []
        self._refresh_tree()
        self.status_var.set(f"已移除 {len(indices)} 个队列项；当前列表有 {len(self.records)} 个文件（源文件未删除）")

    def clear_records(self) -> None:
        if self._conversion_active:
            return
        removed = len(self.records)
        self.records = []
        self._last_results = []
        self._refresh_tree()
        self._set_import_buttons(False)
        self.status_var.set(f"已清空列表中的 {removed} 个队列项（源文件未删除）")

    def set_selected_role(self, role: str) -> None:
        if self._conversion_active:
            return
        if self._last_results:
            messagebox.showwarning("转换已完成", "请在转换前设置文档类型；已有全文结果不会被改名或覆盖。")
            return
        chosen = set(self.tree.selection())
        for index, row in enumerate(self.tree.get_children()):
            if row in chosen and self.records[index].citation_key:
                self.records[index].document_role = role
                self.records[index].role_source = "user-selected"
        self._refresh_conversion_history()
        self._refresh_tree()

    def convert(self, serial: bool) -> None:
        if self._conversion_active:
            return
        if self.input.get("1.0", "end-1c").strip() and not self.parse_paths():
            return
        self._refresh_conversion_history()
        self._refresh_tree()
        if not any(record.valid for record in self.records):
            messagebox.showwarning("没有有效 PDF", "请先粘贴或选择有效 PDF 文件。")
            return
        names = [output_name_stem(r) for r in self.records if r.valid]
        if len(names) != len(set(names)):
            messagebox.showerror("文档名称冲突", "同篇论文有多份附件被设为正文。请将补充材料或其他附件设为对应类型后再转换。")
            return
        try:
            key = get_api_key()
        except Exception as exc:
            messagebox.showerror("API Key 未配置", str(exc))
            return
        options = ParseOptions(model_version=self.model_var.get(), language=self.language_var.get(), is_ocr=self.ocr_var.get())
        output_root = Path(self.output_var.get()).expanduser()
        records_snapshot = clone_source_records(self.records)
        self._last_results = []
        self._save_settings()
        self._set_import_buttons(False)
        self._set_busy(True)
        self._start_progress()
        self.status_var.set("转换中，请等待任务完成……")
        threading.Thread(
            target=self._convert_worker,
            args=(key, options, output_root, serial, records_snapshot),
            daemon=True,
            name="mineru-conversion",
        ).start()

    def _set_busy(self, busy: bool) -> None:
        self._conversion_active = busy
        for widget in self._busy_widgets:
            if isinstance(widget, tk.Text):
                widget.configure(state="disabled" if busy else "normal")
            elif busy:
                widget.state(["disabled"])
            else:
                widget.state(["!disabled"])
        self._set_import_buttons(bool(self._successful_results()) and not busy)

    def _set_import_buttons(self, enabled: bool) -> None:
        if not self._import_buttons:
            return
        state = "normal" if enabled and not self._conversion_active else "disabled"
        for button in self._import_buttons:
            button.state(["!disabled"] if state == "normal" else ["disabled"])

    def _start_progress(self) -> None:
        self.progressbar.stop()
        self.progressbar.configure(mode="indeterminate", value=0)
        self.progressbar.start(12)
        self._progress_indeterminate = True
        self.progress_state_var.set("等待 MinerU 返回任务进度……")

    def _update_progress(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        percent = payload.get("percent")
        message = str(payload.get("message") or "处理中……")
        if isinstance(percent, (int, float)):
            if self._progress_indeterminate:
                self.progressbar.stop()
                self.progressbar.configure(mode="determinate")
                self._progress_indeterminate = False
            self.progressbar["value"] = max(0.0, min(100.0, float(percent)))
        elif not self._progress_indeterminate:
            self.progressbar.configure(mode="indeterminate")
            self.progressbar.start(12)
            self._progress_indeterminate = True
        self.progress_state_var.set(message)

    def _finish_progress(self, success: bool) -> None:
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate", value=100 if success else self.progressbar["value"])
        self._progress_indeterminate = False
        self.progress_state_var.set("转换完成" if success else "转换失败")

    def _convert_worker(
        self,
        key: str,
        options: ParseOptions,
        output_root: Path,
        serial: bool,
        records_snapshot: list[SourceRecord],
    ) -> None:
        try:
            client = MineruClient(key, base_url=get_base_url())
            result = convert_records(
                client,
                records_snapshot,
                options=options,
                output_root=output_root,
                serial=serial,
                progress=lambda message: self.events.put(("status", message)),
                progress_detail=lambda detail: self.events.put(("progress", detail)),
            )
            self.events.put(("done", result))
        except Exception as exc:  # surfaced on the UI thread
            self.events.put(("error", exc))

    def _source_key(self, value: object) -> str | None:
        if not value:
            return None
        return str(Path(str(value)).expanduser().resolve(strict=False))

    def _successful_results(self) -> list[dict[str, object]]:
        return [
            result
            for result in self._last_results
            if result.get("status") == "done" and result.get("output_dir")
        ]

    def _selected_results(self, all_results: bool = False) -> list[dict[str, object]]:
        successful = self._successful_results()
        if all_results:
            return successful
        children = self.tree.get_children()
        selected = set(self.tree.selection())
        indices = [index for index, item in enumerate(children) if item in selected]
        keys = {
            _record_path_key(self.records[index])
            for index in indices
            if 0 <= index < len(self.records)
        }
        return [
            result
            for result in successful
            if self._source_key((result.get("source") or {}).get("path") if isinstance(result.get("source"), dict) else None) in keys
        ]

    def choose_obsidian_vault(self) -> None:
        if self._conversion_active:
            return
        current = Path(self.obsidian_vault_var.get()).expanduser()
        initial = current if current.is_dir() else Path.home()
        directory = filedialog.askdirectory(title="选择 Obsidian Vault", initialdir=str(initial))
        if directory:
            self.obsidian_vault_var.set(directory)
            self._save_settings()

    def choose_obsidian_target(self) -> None:
        if self._conversion_active:
            return
        vault = Path(self.obsidian_vault_var.get()).expanduser().resolve()
        if not vault.is_dir():
            messagebox.showerror("Vault 不存在", "请先选择一个有效的 Obsidian Vault。")
            return
        directory = filedialog.askdirectory(title="选择 Vault 内的笔记目录", initialdir=str(vault))
        if not directory:
            return
        selected = Path(directory).expanduser().resolve()
        try:
            relative = selected.relative_to(vault)
        except ValueError:
            messagebox.showerror("目录无效", "笔记目录必须位于所选 Obsidian Vault 内。")
            return
        self.obsidian_target_var.set(relative.as_posix() if relative.parts else ".")
        self._save_settings()

    def _make_import_plans(self, results: Sequence[dict[str, object]]) -> list[ImportPlan] | None:
        vault = Path(self.obsidian_vault_var.get()).expanduser()
        target = self.obsidian_target_var.get().strip() or "."
        if not vault.is_dir():
            messagebox.showerror("Vault 不存在", f"请先选择有效的 Obsidian Vault：\n{vault}")
            return None
        plans: list[ImportPlan] = []
        errors: list[str] = []
        for result in results:
            try:
                source = result.get("source") if isinstance(result.get("source"), dict) else {}
                plans.append(create_import_plan(Path(str(result["output_dir"])), vault, target,
                                                literature=bool(source.get("citation_key")), source_metadata=source))
            except (ObsidianImportError, OSError) as exc:
                errors.append(str(exc))
        if errors:
            messagebox.showerror("导入计划失败", "\n".join(errors[:5]))
            return None
        return plans

    def _import_results(self, results: Sequence[dict[str, object]]) -> None:
        plans = self._make_import_plans(results)
        if not plans:
            return
        conflicts = sum(len(plan.conflicts) for plan in plans)
        same_identity = sum(
            plan.identity_status in {"unchanged", "legacy-same", "existing"}
            for plan in plans
        )
        changed_identity = sum(plan.identity_status == "changed" for plan in plans)
        destinations = [p.note_destination for p in plans]
        if len(destinations) != len(set(destinations)):
            messagebox.showerror("导入目标冲突", "多份结果指向同一文件。请核对正文与补充材料类型。")
            return
        assets = sum(len(plan.assets) for plan in plans)
        external = sorted({image for plan in plans for image in plan.external_images})
        missing = sorted({image for plan in plans for image in plan.missing_images})
        lines = [
            "这是 dry-run 导入计划：",
            f"笔记：{len(plans)} 篇",
            f"图片：{assets} 个",
            f"目标：{plans[0].target_dir}",
            f"图片目录：{plans[0].attachment_dir.parent}",
            f"同名冲突：{conflicts} 个",
            f"外链图片：{len(external)} 个",
            f"无法匹配的本地图片：{len(missing)} 个",
            f"既有全文：相同 {same_identity} 篇，PDF 已变化 {changed_identity} 篇",
        ]
        changed_plans = [plan for plan in plans if plan.identity_status == "changed"]
        for plan in changed_plans[:3]:
            lines.append(
                f"变化：{plan.note_destination.name}"
                f"（{str(plan.previous_source_sha256 or '未知哈希')[:12]} → "
                f"{str(plan.current_source_sha256 or '未知哈希')[:12]}）"
            )
        if missing:
            lines.append("未匹配示例：" + ", ".join(missing[:3]))
        if not messagebox.askyesno("确认导入 Obsidian", "\n".join(lines)):
            return
        overwrite = False
        if conflicts:
            change_warning = (
                f"\n其中 {changed_identity} 个目标属于同一附件的 PDF 变化版本。"
                if changed_identity
                else ""
            )
            overwrite = messagebox.askyesno(
                "确认覆盖并备份",
                f"发现 {conflicts} 个已存在文件。{change_warning}"
                "继续后会先备份再覆盖；取消则写入零个文件。是否继续？",
            )
            if not overwrite:
                return
        try:
            backups = []
            for plan in plans:
                backup = execute_import(plan, overwrite=overwrite, backup_root=Path.cwd() / "backups")
                if backup:
                    backups.append(str(backup))
        except (ObsidianImportError, OSError) as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        self._save_settings()
        self.status_var.set(f"已导入 {len(plans)} 篇笔记和 {assets} 个图片资源")
        suffix = f"\n备份：{backups[0]}" if backups else ""
        messagebox.showinfo("导入完成", f"Obsidian 导入完成。{suffix}")

    def import_selected_to_obsidian(self) -> None:
        results = self._selected_results()
        if not results:
            messagebox.showwarning("没有可导入结果", "请先选中已成功转换的 PDF 行。")
            return
        self._import_results(results)

    def import_all_to_obsidian(self) -> None:
        results = self._selected_results(all_results=True)
        if not results:
            messagebox.showwarning("没有可导入结果", "当前没有成功转换的结果。")
            return
        self._import_results(results)

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    self._update_progress(payload)
                elif kind == "done":
                    self._last_results = list(payload) if isinstance(payload, list) else []
                    self._set_busy(False)
                    self._finish_progress(True)
                    done_count = sum(item.get("status") == "done" for item in self._last_results)
                    failed = [item for item in self._last_results if item.get("status") == "failed"]
                    self.status_var.set(f"完成：{done_count} 个成功，{len(failed)} 个失败；可选择成功结果导入 Obsidian")
                    detail = f"成功 {done_count} 个，失败 {len(failed)} 个。"
                    if failed:
                        detail += "\n" + str(failed[0].get("error", "未知错误"))
                    messagebox.showinfo("转换完成", detail + "\n成功结果已整理到输出目录。")
                elif kind == "error":
                    self._set_busy(False)
                    self._finish_progress(False)
                    self.status_var.set("失败")
                    messagebox.showerror("转换失败", str(payload))
        except queue.Empty:
            pass
        self.root.after(150, self._drain_events)


def run_gui(**kwargs: Any) -> None:
    root = tk.Tk()
    ConverterGUI(root, **kwargs)
    root.mainloop()
