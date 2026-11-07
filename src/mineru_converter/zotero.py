from __future__ import annotations

"""Read-only access to local Zotero PDF attachments.

The module intentionally uses SQLite's immutable read-only URI.  It never
updates the Zotero database and never calls a remote Zotero API.  The GUI uses
this module to turn selected attachments into the same ``SourceRecord`` type
used by pasted paths and file pickers.
"""

from dataclasses import dataclass, replace
import json
import sqlite3
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from .models import SourceRecord
from .paths import build_source_records
from .document_roles import inspect_pdf


class ZoteroReadError(RuntimeError):
    """Raised when the local Zotero database cannot be read safely."""


@dataclass(frozen=True)
class ZoteroCurrentCollection:
    """The collection currently selected in Zotero's left pane."""

    target_id: str | None
    collection_id: int | None
    name: str
    library_id: int
    library_name: str

    @property
    def is_library(self) -> bool:
        return self.collection_id is None


def default_zotero_db_path() -> Path:
    return Path.home() / "Zotero" / "zotero.sqlite"


def default_zotero_storage_path(db_path: Path | None = None) -> Path:
    database = (db_path or default_zotero_db_path()).expanduser()
    return database.parent / "storage"


@dataclass(frozen=True)
class ZoteroPDFAttachment:
    item_id: int
    item_key: str
    parent_item_id: int | None
    path: str
    link_mode: int | None
    content_type: str | None
    local_path: Path

    @property
    def file_name(self) -> str:
        return self.local_path.name

    @property
    def exists(self) -> bool:
        return self.local_path.is_file()

    @property
    def status(self) -> str:
        return "可用" if self.exists else "缺失"


@dataclass(frozen=True)
class ZoteroPDFItem:
    item_id: int
    library_id: int
    library_type: str
    item_key: str
    item_type: str
    title: str
    creators: str
    date: str
    doi: str
    citation_key: str | None
    publication: str
    attachments: tuple[ZoteroPDFAttachment, ...]

    @property
    def year(self) -> str:
        return self.date[:4] if self.date else ""

    @property
    def zotero_uri(self) -> str:
        if self.library_type == "group":
            return f"zotero://select/groups/{self.library_id}/items/{self.item_key}"
        return f"zotero://select/library/items/{self.item_key}"

    @property
    def search_text(self) -> str:
        values = [
            self.title,
            self.creators,
            self.date,
            self.doi,
            self.citation_key or "",
            self.publication,
            *(attachment.file_name for attachment in self.attachments),
        ]
        return " ".join(values).casefold()


@dataclass(frozen=True)
class ZoteroPDFSelection:
    """One explicitly selected parent item + PDF attachment pair."""

    item: ZoteroPDFItem
    attachment: ZoteroPDFAttachment


def get_current_collection(
    connector_url: str = "http://127.0.0.1:23119",
    *,
    timeout: float = 3.0,
) -> ZoteroCurrentCollection:
    """Read Zotero's current left-pane collection through its local Connector.

    Zotero 9 exposes the current collection through a read-only POST endpoint.
    The empty JSON body is intentional; no save/update operation is performed.
    """

    endpoint = connector_url.rstrip("/") + "/connector/getSelectedCollection"
    request = Request(
        endpoint,
        data=b"{}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost Connector only
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ZoteroReadError(
            "无法读取 Zotero 当前文件夹。请确认 Zotero 已打开，并检查本地 Connector。"
        ) from exc
    if not isinstance(payload, dict):
        raise ZoteroReadError("Zotero Connector 返回了无法识别的文件夹信息。")

    raw_id = payload.get("id")
    target_id = str(raw_id) if raw_id else None
    collection_id: int | None = None
    if target_id and target_id.startswith("C") and target_id[1:].isdigit():
        collection_id = int(target_id[1:])
    elif target_id and target_id.isdigit():
        # Zotero 9 may return the numeric collection ID after a
        # zotero://select navigation, while other Connector calls return C13.
        collection_id = int(target_id)
    try:
        library_id = int(payload.get("libraryID", 1))
    except (TypeError, ValueError):
        library_id = 1
    return ZoteroCurrentCollection(
        target_id=target_id,
        collection_id=collection_id,
        name=str(payload.get("name") or "当前 Zotero 位置"),
        library_id=library_id,
        library_name=str(payload.get("libraryName") or "Zotero 文库"),
    )


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    path = db_path.expanduser().resolve(strict=False)
    if not path.is_file():
        raise ZoteroReadError(f"Zotero 数据库不存在：{path}")
    # immutable avoids touching the active database's journal/WAL and makes
    # accidental writes impossible through this connection.
    uri = f"file:{path}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as exc:
        raise ZoteroReadError(f"无法只读打开 Zotero 数据库：{exc}") from exc


def _path_candidates(raw_path: str, attachment_key: str, storage_root: Path) -> list[Path]:
    raw = (raw_path or "").strip()
    if not raw:
        return [storage_root / attachment_key / "missing.pdf"]

    candidates: list[Path] = []
    if raw.startswith("storage:"):
        name = raw[len("storage:") :].lstrip("/")
        candidates.extend(
            [
                storage_root / attachment_key / name,
                storage_root / attachment_key / unquote(name),
            ]
        )
    elif raw.startswith("file://"):
        parsed = urlparse(raw)
        candidates.extend([Path(parsed.path), Path(unquote(parsed.path))])
    else:
        candidates.extend([Path(raw).expanduser(), Path(unquote(raw)).expanduser()])

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique or [storage_root / attachment_key / "missing.pdf"]


def resolve_attachment_path(
    attachment_path: str,
    attachment_key: str,
    storage_root: Path | None = None,
) -> Path:
    """Resolve Zotero ``storage:`` and linked-file attachment paths.

    If an exact path is missing, the first candidate is returned so the GUI
    can display the expected path and report it without submitting anything.
    Percent-decoded candidates are only preferred when the raw candidate does
    not exist, matching the pasted-path behavior.
    """

    root = (storage_root or default_zotero_storage_path()).expanduser()
    candidates = _path_candidates(attachment_path, attachment_key, root)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].expanduser().resolve(strict=False)


def _creator_text(connection: sqlite3.Connection, item_ids: list[int]) -> dict[int, str]:
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = connection.execute(
        f"""
        SELECT ic.itemID, c.firstName, c.lastName, c.fieldMode, ic.orderIndex
        FROM itemCreators ic
        JOIN creators c ON c.creatorID = ic.creatorID
        WHERE ic.itemID IN ({placeholders})
        ORDER BY ic.itemID, ic.orderIndex
        """,
        item_ids,
    )
    values: dict[int, list[str]] = {}
    for row in rows:
        if row["fieldMode"]:
            name = (row["lastName"] or row["firstName"] or "").strip()
        else:
            first = (row["firstName"] or "").strip()
            last = (row["lastName"] or "").strip()
            name = " ".join(part for part in (first, last) if part)
        if name:
            values.setdefault(int(row["itemID"]), []).append(name)
    return {item_id: "; ".join(names) for item_id, names in values.items()}


def list_pdf_items(
    db_path: Path | None = None,
    storage_root: Path | None = None,
    *,
    query: str = "",
    limit: int = 500,
    include_standalone: bool = False,
    collection_id: int | None = None,
    collection_key: str | None = None,
) -> list[ZoteroPDFItem]:
    """List Zotero parent items that have one or more PDF attachments."""

    if limit < 1:
        return []
    if collection_id is not None and collection_key:
        raise ValueError("collection_id and collection_key cannot be used together")
    database = (db_path or default_zotero_db_path()).expanduser()
    root = (storage_root or default_zotero_storage_path(database)).expanduser()
    connection = _connect_read_only(database)
    try:
        parent_filter = "" if include_standalone else "AND a.parentItemID IS NOT NULL"
        collection_filter = ""
        collection_params: list[object] = []
        if collection_id is not None:
            collection_filter = """
              AND EXISTS (
                    SELECT 1
                    FROM collectionItems selected_collection_item
                    JOIN collections selected_collection
                      ON selected_collection.collectionID = selected_collection_item.collectionID
                    WHERE selected_collection_item.itemID = COALESCE(p.itemID, ai.itemID)
                      AND selected_collection.collectionID = ?
              )
            """
            collection_params.append(collection_id)
        elif collection_key:
            collection_filter = """
              AND EXISTS (
                    SELECT 1
                    FROM collectionItems selected_collection_item
                    JOIN collections selected_collection
                      ON selected_collection.collectionID = selected_collection_item.collectionID
                    WHERE selected_collection_item.itemID = COALESCE(p.itemID, ai.itemID)
                      AND selected_collection.key = ?
              )
            """
            collection_params.append(collection_key)
        rows = connection.execute(
            f"""
            SELECT
                COALESCE(p.itemID, a.itemID) AS source_item_id,
                COALESCE(p.libraryID, ai.libraryID) AS library_id,
                COALESCE(pl.type, al.type, 'user') AS library_type,
                COALESCE(p.key, ai.key) AS item_key,
                COALESCE(pt.typeName, at.typeName, 'attachment') AS item_type,
                COALESCE((
                    SELECT value.value
                    FROM itemData data
                    JOIN fields field ON field.fieldID = data.fieldID
                    JOIN itemDataValues value ON value.valueID = data.valueID
                    WHERE data.itemID = p.itemID AND field.fieldName = 'title'
                    LIMIT 1
                ), '') AS title,
                COALESCE((
                    SELECT value.value
                    FROM itemData data
                    JOIN fields field ON field.fieldID = data.fieldID
                    JOIN itemDataValues value ON value.valueID = data.valueID
                    WHERE data.itemID = p.itemID AND field.fieldName = 'date'
                    LIMIT 1
                ), '') AS date,
                COALESCE((
                    SELECT value.value
                    FROM itemData data
                    JOIN fields field ON field.fieldID = data.fieldID
                    JOIN itemDataValues value ON value.valueID = data.valueID
                    WHERE data.itemID = p.itemID AND field.fieldName = 'DOI'
                    LIMIT 1
                ), '') AS doi,
                NULLIF(TRIM((
                    SELECT value.value
                    FROM itemData data
                    JOIN fields field ON field.fieldID = data.fieldID
                    JOIN itemDataValues value ON value.valueID = data.valueID
                    WHERE data.itemID = p.itemID AND field.fieldName = 'citationKey'
                    LIMIT 1
                )), '') AS citation_key,
                COALESCE((
                    SELECT value.value
                    FROM itemData data
                    JOIN fields field ON field.fieldID = data.fieldID
                    JOIN itemDataValues value ON value.valueID = data.valueID
                    WHERE data.itemID = p.itemID AND field.fieldName = 'publicationTitle'
                    LIMIT 1
                ), '') AS publication,
                a.itemID AS attachment_id,
                ai.key AS attachment_key,
                a.parentItemID AS parent_item_id,
                a.linkMode AS link_mode,
                a.contentType AS content_type,
                a.path AS attachment_path
            FROM itemAttachments a
            JOIN items ai ON ai.itemID = a.itemID
            LEFT JOIN items p ON p.itemID = a.parentItemID
            LEFT JOIN itemTypes pt ON pt.itemTypeID = p.itemTypeID
            LEFT JOIN itemTypes at ON at.itemTypeID = ai.itemTypeID
            LEFT JOIN libraries pl ON pl.libraryID = p.libraryID
            LEFT JOIN libraries al ON al.libraryID = ai.libraryID
            WHERE (lower(COALESCE(a.contentType, '')) = 'application/pdf'
                   OR lower(COALESCE(a.path, '')) LIKE '%.pdf')
              {parent_filter}
              {collection_filter}
            ORDER BY COALESCE(p.dateModified, ai.dateModified) DESC, a.itemID DESC
            """,
            collection_params,
        ).fetchall()

        parent_ids = [int(row["source_item_id"]) for row in rows if row["parent_item_id"] is not None]
        creators = _creator_text(connection, parent_ids)
        items_by_id: dict[int, ZoteroPDFItem] = {}
        for row in rows:
            source_id = int(row["source_item_id"])
            title = (row["title"] or "").strip()
            attachment_key = str(row["attachment_key"])
            local_path = resolve_attachment_path(str(row["attachment_path"] or ""), attachment_key, root)
            attachment = ZoteroPDFAttachment(
                item_id=int(row["attachment_id"]),
                item_key=attachment_key,
                parent_item_id=int(row["parent_item_id"]) if row["parent_item_id"] is not None else None,
                path=str(row["attachment_path"] or ""),
                link_mode=int(row["link_mode"]) if row["link_mode"] is not None else None,
                content_type=str(row["content_type"] or "") or None,
                local_path=local_path,
            )
            item = items_by_id.get(source_id)
            if item is None:
                item = ZoteroPDFItem(
                    item_id=source_id,
                    library_id=int(row["library_id"]),
                    library_type=str(row["library_type"] or "user"),
                    item_key=str(row["item_key"]),
                    item_type=str(row["item_type"]),
                    title=title or attachment.file_name,
                    creators=creators.get(source_id, ""),
                    date=str(row["date"] or ""),
                    doi=str(row["doi"] or ""),
                    citation_key=str(row["citation_key"]) if row["citation_key"] else None,
                    publication=str(row["publication"] or ""),
                    attachments=(attachment,),
                )
            else:
                item = replace(item, attachments=(*item.attachments, attachment))
            items_by_id[source_id] = item

        normalized_query = query.strip().casefold()
        result = list(items_by_id.values())
        if normalized_query:
            result = [item for item in result if normalized_query in item.search_text]
        return result[:limit]
    except sqlite3.Error as exc:
        raise ZoteroReadError(f"读取 Zotero PDF 条目失败：{exc}") from exc
    finally:
        connection.close()


def records_from_zotero_selections(
    selections: Iterable[ZoteroPDFSelection],
) -> list[SourceRecord]:
    """Build queue records while retaining citation-key and Zotero metadata."""

    records: list[SourceRecord] = []
    for selection in selections:
        item = selection.item
        attachment = selection.attachment
        built = build_source_records([str(attachment.local_path)], deduplicate=False)
        if not built:
            continue
        record = built[0]
        record.citation_key = item.citation_key
        record.zotero_item_key = item.item_key
        record.zotero_attachment_key = attachment.item_key
        record.zotero_uri = item.zotero_uri
        if record.valid:
            inspection = inspect_pdf(attachment.local_path)
            record.document_role = inspection["document_role"]
            record.role_source = inspection["role_source"]
            if record.document_role == "unknown" and len(item.attachments) == 1:
                record.document_role = None
                record.role_source = "legacy-single-attachment"
        record.attachment_title = attachment.file_name
        records.append(record)
    return records
