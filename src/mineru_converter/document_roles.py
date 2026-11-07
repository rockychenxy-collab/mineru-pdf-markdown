"""Local-only attachment inspection and collision-free literature document identities."""
from __future__ import annotations

import re
from pathlib import Path

ROLES = {"main": "正文", "supplement": "补充材料", "other": "其他附件", "unknown": "待确认附件"}


def inspect_pdf(path: Path) -> dict:
    from pypdf import PdfReader
    path = path.resolve()
    result = {"path": str(path), "pages": None, "document_role": "unknown", "role_source": "unconfirmed"}
    try:
        reader = PdfReader(path, strict=False)
        result["pages"] = len(reader.pages)
        text = (reader.pages[0].extract_text() or "") if reader.pages else ""
        header = "\n".join(text.splitlines()[:18])[:1000]
        if re.search(r"^\s*(?:supplementary|supporting)[\s-]*(?:information|materials?|data)\b", header, re.I | re.M):
            result.update(document_role="supplement", role_source="first-page-hint")
        elif re.search(r"\babstract\b|\b1\.?\s+introduction\b", "\n".join((p.extract_text() or "") for p in reader.pages[:2])[:12000], re.I):
            result.update(document_role="main", role_source="first-page-hint")
    except Exception:
        result["inspection_warning"] = "无法可靠读取首页；请手动指定文档类型"
    return result


def document_stem(citekey: str, role: str | None, attachment_key: str | None, digest: str | None = None) -> str:
    if role in {None, "main"}:
        return citekey
    if role not in ROLES:
        raise ValueError("unknown document role")
    key = attachment_key or (digest or "")[:12]
    if not key or not re.fullmatch(r"[\w.-]+", key):
        raise ValueError("附件文档需要 Zotero 附件ID或文件哈希，不能使用相同文件名区分")
    label = "补充" if role == "supplement" else "附件"
    return f"{citekey}__{label}_{key}"
