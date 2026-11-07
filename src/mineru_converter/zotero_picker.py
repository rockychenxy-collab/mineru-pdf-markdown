from __future__ import annotations

"""Small Tk picker for local Zotero PDF attachments."""

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import messagebox, ttk

from .zotero import ZoteroPDFAttachment, ZoteroPDFItem, ZoteroPDFSelection


class ZoteroPicker:
    """Show read-only Zotero items and return explicitly selected PDFs."""

    def __init__(
        self,
        parent: tk.Misc,
        items: Sequence[ZoteroPDFItem],
        on_confirm: Callable[[list[ZoteroPDFSelection]], None],
        on_locate: Callable[[ZoteroPDFItem], None],
        scope_label: str = "当前 Zotero 文件夹",
    ) -> None:
        self.parent = parent
        self.items = list(items)
        self.on_confirm = on_confirm
        self.on_locate = on_locate
        self.scope_label = scope_label
        self._rows: dict[str, ZoteroPDFSelection] = {}

        self.window = tk.Toplevel(parent)
        self.window.title("从 Zotero 选择 PDF")
        self.window.geometry("1250x680")
        self.window.minsize(850, 480)
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

        outer = ttk.Frame(self.window, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text=(
                "这里读取 Zotero 本地数据库中的 PDF 附件（只读）。"
                "勾选要转换的 PDF；需要查看原条目时可用“在 Zotero 中定位”。"
            ),
        ).pack(anchor="w")

        search_row = ttk.Frame(outer)
        search_row.pack(fill="x", pady=(8, 6))
        ttk.Label(search_row, text="搜索：").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var, width=45)
        self.search_entry.pack(side="left", padx=(4, 8), fill="x", expand=True)
        self.search_var.trace_add("write", lambda *_args: self._refresh_rows())
        self.refresh_button = ttk.Button(search_row, text="刷新", command=self._refresh_rows)
        self.refresh_button.pack(side="left")
        self.select_all_button = ttk.Button(search_row, text="全选当前", command=self._select_all)
        self.select_all_button.pack(side="left", padx=5)
        self.clear_selection_button = ttk.Button(search_row, text="清空选择", command=self._clear_selection)
        self.clear_selection_button.pack(side="left")
        self.locate_button = ttk.Button(search_row, text="在 Zotero 中定位", command=self._locate_selected)
        self.locate_button.pack(side="left", padx=(12, 0))

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        columns = ("title", "creators", "year", "citation", "attachment", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "title": "条目标题",
            "creators": "作者",
            "year": "年份",
            "citation": "引用关键词",
            "attachment": "PDF 附件",
            "status": "本地状态",
        }
        widths = {"title": 330, "creators": 230, "year": 65, "citation": 190, "attachment": 300, "status": 85}
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
        self.tree.tag_configure("missing", foreground="#a33a3a")
        self.tree.bind("<Double-1>", self._double_click)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.status_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="取消", command=self.window.destroy).pack(side="right")
        ttk.Button(bottom, text="加入转换队列", command=self._confirm).pack(side="right", padx=(0, 6))

        self._refresh_rows()
        self.search_entry.focus_set()

    def _filtered_rows(self) -> list[tuple[ZoteroPDFItem, ZoteroPDFAttachment]]:
        query = self.search_var.get().strip().casefold()
        rows: list[tuple[ZoteroPDFItem, ZoteroPDFAttachment]] = []
        for item in self.items:
            if query and query not in item.search_text:
                continue
            rows.extend((item, attachment) for attachment in item.attachments)
        return rows

    def _refresh_rows(self) -> None:
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)
        self._rows.clear()
        rows = self._filtered_rows()
        for index, (item, attachment) in enumerate(rows):
            row_id = f"row-{index}"
            selection = ZoteroPDFSelection(item=item, attachment=attachment)
            self._rows[row_id] = selection
            self.tree.insert(
                "",
                "end",
                iid=row_id,
                values=(
                    item.title,
                    item.creators or "-",
                    item.year or "-",
                    item.citation_key or "-",
                    attachment.file_name,
                    attachment.status,
                ),
                tags=("missing",) if not attachment.exists else (),
            )
        self.status_var.set(f"{self.scope_label}：显示 {len(rows)} 个 PDF 附件；共 {len(self.items)} 个条目")

    def _select_all(self) -> None:
        self.tree.selection_set(self.tree.get_children())

    def _clear_selection(self) -> None:
        self.tree.selection_remove(self.tree.selection())

    def _selected(self) -> list[ZoteroPDFSelection]:
        return [self._rows[row_id] for row_id in self.tree.selection() if row_id in self._rows]

    def _locate_selected(self) -> None:
        selected = self._selected()
        if not selected:
            messagebox.showwarning("未选择条目", "请先选择一个 PDF 附件。", parent=self.window)
            return
        self.on_locate(selected[0].item)

    def _double_click(self, _event: tk.Event[tk.Misc]) -> None:
        self._locate_selected()

    def _confirm(self) -> None:
        selected = self._selected()
        if not selected:
            messagebox.showwarning("未选择 PDF", "请先勾选一个或多个 PDF 附件。", parent=self.window)
            return
        missing = sum(not selection.attachment.exists for selection in selected)
        if missing:
            messagebox.showwarning(
                "存在缺失附件",
                f"已选择 {len(selected)} 个附件，其中 {missing} 个本地文件缺失。"
                "缺失项会加入队列但不会上传 MinerU。",
                parent=self.window,
            )
        self.on_confirm(selected)
        self.window.destroy()
