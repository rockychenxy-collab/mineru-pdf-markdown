from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import shutil
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
from pypdf import PdfWriter

from mineru_converter.api import MineruClient, progress_percent
from mineru_converter.bundle import BundleError, normalize_bundle, safe_extract_zip
from mineru_converter.cli import build_parser
from mineru_converter.config import describe_environment, write_environment
from mineru_converter.gui import clone_source_records, merge_source_records, remove_record_indices
from mineru_converter.models import BatchSubmission, SourceRecord, TaskResult
from mineru_converter.obsidian import ObsidianImportError, create_import_plan, execute_import
from mineru_converter.paths import PathListParseError, build_source_records, tokenize_pasted_paths
from mineru_converter.pipeline import convert_records
from mineru_converter.settings import load_gui_settings, save_gui_settings
from mineru_converter.pipeline import output_assets_name, output_markdown_name
from mineru_converter.zotero import (
    ZoteroPDFSelection,
    get_current_collection,
    list_pdf_items,
    records_from_zotero_selections,
    resolve_attachment_path,
)


def _write_pdf(path: Path, pages: int = 1, marker: str = "") -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)
    if marker:
        # Keep the fixture a valid PDF while making duplicate tests distinct.
        path.write_bytes(path.read_bytes() + marker.encode())


def _make_zotero_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create the smallest read-only Zotero-like database for unit tests."""

    database = tmp_path / "zotero.sqlite"
    storage = tmp_path / "storage"
    pdf = storage / "ATTACH01" / "Paper with spaces.pdf"
    pdf.parent.mkdir(parents=True)
    _write_pdf(pdf, pages=2)
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT NOT NULL);
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT NOT NULL);
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY, itemTypeID INTEGER NOT NULL,
            dateAdded TEXT NOT NULL, dateModified TEXT NOT NULL,
            clientDateModified TEXT NOT NULL, libraryID INTEGER NOT NULL,
            key TEXT NOT NULL, version INTEGER NOT NULL, synced INTEGER NOT NULL
        );
        CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY, parentItemID INTEGER, linkMode INTEGER,
            contentType TEXT, charsetID INTEGER, path TEXT, syncState INTEGER,
            storageModTime INTEGER, storageHash TEXT, lastProcessedModificationTime INTEGER,
            lastRead INTEGER
        );
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT, fieldFormatID INTEGER);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER, PRIMARY KEY(itemID, fieldID));
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INTEGER);
        CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER,
                                   PRIMARY KEY(itemID, creatorID, creatorTypeID));
        CREATE TABLE collections (
            collectionID INTEGER PRIMARY KEY, collectionName TEXT NOT NULL,
            parentCollectionID INTEGER, libraryID INTEGER NOT NULL, key TEXT NOT NULL
        );
        CREATE TABLE collectionItems (collectionID INTEGER NOT NULL, itemID INTEGER NOT NULL,
                                      orderIndex INTEGER NOT NULL, PRIMARY KEY(collectionID, itemID));
        """
    )
    connection.executemany("INSERT INTO libraries VALUES (?, ?)", [(1, "user")])
    connection.executemany("INSERT INTO itemTypes VALUES (?, ?)", [(1, "journalArticle"), (2, "attachment")])
    connection.execute(
        "INSERT INTO items VALUES (1, 1, '2025', '2025', '2025', 1, 'PARENT01', 0, 0)"
    )
    connection.execute(
        "INSERT INTO items VALUES (2, 2, '2025', '2025', '2025', 1, 'ATTACH01', 0, 0)"
    )
    connection.execute(
        "INSERT INTO itemAttachments VALUES (2, 1, 0, 'application/pdf', NULL, 'storage:Paper with spaces.pdf', 0, NULL, NULL, NULL, NULL)"
    )
    fields = [(1, "title"), (2, "date"), (3, "DOI"), (4, "citationKey"), (5, "publicationTitle")]
    connection.executemany("INSERT INTO fields(fieldID, fieldName) VALUES (?, ?)", fields)
    values = [
        (1, "A Zotero Fixture"),
        (2, "2025-01-01"),
        (3, "10.1234/fixture"),
        (4, "fixture.etal2025paper"),
        (5, "Fixture Journal"),
    ]
    connection.executemany("INSERT INTO itemDataValues VALUES (?, ?)", values)
    connection.executemany(
        "INSERT INTO itemData(itemID, fieldID, valueID) VALUES (1, ?, ?)",
        [(field_id, value_id) for field_id, value_id in zip(range(1, 6), range(1, 6))],
    )
    connection.execute("INSERT INTO creatorTypes VALUES (10, 'author')")
    connection.execute("INSERT INTO creators VALUES (1, 'Ada', 'Lovelace', 0)")
    connection.execute("INSERT INTO itemCreators VALUES (1, 1, 10, 0)")
    connection.execute("INSERT INTO collections VALUES (1, 'Fixture folder', NULL, 1, 'COLL01')")
    connection.execute("INSERT INTO collections VALUES (2, 'Other folder', NULL, 1, 'COLL02')")
    connection.execute("INSERT INTO collectionItems VALUES (1, 1, 0)")
    connection.commit()
    connection.close()
    return database, storage, pdf


def test_pasted_paths_support_quotes_unicode_commas_percent_encoding_and_file_uri(tmp_path: Path) -> None:
    first = tmp_path / "中文 论文 (一).pdf"
    second = tmp_path / "plain.pdf"
    third = tmp_path / "百分号 文件.pdf"
    fourth = tmp_path / "第四篇.pdf"
    for path in [first, second, third, fourth]:
        _write_pdf(path, marker=path.name)

    encoded_third = quote(str(third), safe="/")
    raw = f"'{first}' {second}\n{encoded_third}，{fourth.as_uri()}"
    tokens = tokenize_pasted_paths(raw)
    assert tokens == [str(first), str(second), encoded_third, fourth.as_uri()]

    records = build_source_records(tokens)
    assert [record.status for record in records] == ["valid"] * 4
    assert records[2].path == third.resolve()
    assert records[2].error == "resolved using percent/file URI decoding"

    with pytest.raises(PathListParseError):
        tokenize_pasted_paths("'unclosed.pdf")


def test_sha256_deduplicates_identical_pdfs(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _write_pdf(first)
    second.write_bytes(first.read_bytes())

    records = build_source_records([str(first), str(second)])
    assert records[0].status == "valid"
    assert records[1].status == "duplicate"
    assert records[1].duplicate_of == str(first.resolve())
    assert str(second.resolve()) in records[0].aliases


def test_gui_queue_appends_records_and_skips_repeated_paths(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _write_pdf(first, marker="first")
    _write_pdf(second, marker="second")

    existing = build_source_records([str(first)])
    merged = merge_source_records(existing, build_source_records([str(second)]))
    assert [record.path for record in merged] == [first.resolve(), second.resolve()]

    repeated = merge_source_records(merged, build_source_records([str(first), str(second)]))
    assert [record.path for record in repeated] == [first.resolve(), second.resolve()]


def test_gui_queue_removal_supports_single_multi_select_and_repairs_duplicates(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    duplicate = tmp_path / "duplicate.pdf"
    third = tmp_path / "third.pdf"
    _write_pdf(first, marker="same")
    duplicate.write_bytes(first.read_bytes())
    _write_pdf(third, marker="different")
    records = build_source_records([str(first), str(duplicate), str(third)])

    after_single = remove_record_indices(records, [2])
    assert [record.path for record in after_single] == [first.resolve(), duplicate.resolve()]
    assert [record.status for record in after_single] == ["valid", "duplicate"]

    after_multi = remove_record_indices(records, [0, 2])
    assert [record.path for record in after_multi] == [duplicate.resolve()]
    assert after_multi[0].status == "valid"
    assert after_multi[0].duplicate_of is None


def test_gui_conversion_snapshot_does_not_share_mutable_aliases(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    _write_pdf(pdf)
    records = build_source_records([str(pdf)])
    snapshot = clone_source_records(records)

    records[0].aliases.append("changed-after-start")
    assert snapshot[0].aliases == []
    assert snapshot[0] is not records[0]


def test_zotero_read_only_query_resolves_pdf_and_citation_key(tmp_path: Path) -> None:
    database, storage, pdf = _make_zotero_fixture(tmp_path)
    items = list_pdf_items(database, storage)

    assert len(items) == 1
    item = items[0]
    assert item.title == "A Zotero Fixture"
    assert item.creators == "Ada Lovelace"
    assert item.citation_key == "fixture.etal2025paper"
    assert item.doi == "10.1234/fixture"
    assert item.attachments[0].local_path == pdf.resolve()
    assert item.attachments[0].exists
    assert item.zotero_uri == "zotero://select/library/items/PARENT01"
    assert resolve_attachment_path("storage:Paper with spaces.pdf", "ATTACH01", storage) == pdf.resolve()

    record = records_from_zotero_selections([ZoteroPDFSelection(item, item.attachments[0])])[0]
    assert record.valid
    assert record.citation_key == "fixture.etal2025paper"
    assert record.zotero_item_key == "PARENT01"
    assert record.zotero_attachment_key == "ATTACH01"
    assert output_markdown_name(record) == "fixture.etal2025paper.md"
    assert output_assets_name(record) == "fixture.etal2025paper_assets"

    assert len(list_pdf_items(database, storage, collection_id=1)) == 1
    assert len(list_pdf_items(database, storage, collection_key="COLL01")) == 1
    assert list_pdf_items(database, storage, collection_id=2) == []


def test_zotero_current_collection_connector_response_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return '{"id":"C13","name":"LIG","libraryID":1,"libraryName":"我的文库"}'.encode()

    calls: list[tuple[object, float]] = []

    def fake_urlopen(request, timeout):  # noqa: ANN001
        calls.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr("mineru_converter.zotero.urlopen", fake_urlopen)
    current = get_current_collection(timeout=1.5)
    assert current.target_id == "C13"
    assert current.collection_id == 13
    assert current.name == "LIG"
    assert calls[0][1] == 1.5


def test_zotero_missing_pdf_becomes_non_uploadable_record(tmp_path: Path) -> None:
    database, storage, _ = _make_zotero_fixture(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO items VALUES (3, 2, '2025', '2025', '2025', 1, 'ATTACH02', 0, 0)")
    connection.execute(
        "INSERT INTO itemAttachments VALUES (3, 1, 0, 'application/pdf', NULL, 'storage:missing.pdf', 0, NULL, NULL, NULL, NULL)"
    )
    connection.commit()
    connection.close()

    items = list_pdf_items(database, storage)
    assert len(items) == 1
    assert len(items[0].attachments) == 2
    missing = next(attachment for attachment in items[0].attachments if not attachment.exists)
    record = records_from_zotero_selections([ZoteroPDFSelection(items[0], missing)])[0]
    assert record.status == "missing"
    assert not record.valid


def test_normalize_bundle_rewrites_assets_and_rejects_traversal(tmp_path: Path) -> None:
    zip_path = tmp_path / "full.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(
            "result/full.md",
            "# Sample\n\n![Figure](images/image_001.png)\n![Remote](https://example.com/a.png)\n![Missing](images/missing.png)\n",
        )
        archive.writestr("result/images/image_001.png", b"not-a-real-png-fixture")

    output = tmp_path / "output"
    details = normalize_bundle(zip_path, output)
    markdown = (output / "full.md").read_text(encoding="utf-8")
    assert details["image_count"] == 1
    assert "assets/image_001.png" in markdown
    assert details["missing_assets"] == ["images/missing.png"]
    assert details["external_images"] == ["https://example.com/a.png"]
    assert (output / "assets/image_001.png").is_file()
    assert (output / "raw/full.zip").is_file()

    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../outside.txt", "blocked")
    with pytest.raises(BundleError):
        safe_extract_zip(unsafe, tmp_path / "extract")


class _Response:
    def __init__(self, status_code: int, body: object | None = None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._body = body
        self.content = content

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def iter_content(self, chunk_size: int = 1):  # noqa: ARG002 - mirrors requests.Response
        yield self.content


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.request_calls: list[tuple[str, str, dict[str, object]]] = []
        self.put_calls: list[tuple[str, dict[str, object]]] = []
        self.get_calls: list[tuple[str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.request_calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def put(self, url: str, **kwargs: object) -> _Response:
        self.put_calls.append((url, kwargs))
        return _Response(200)

    def get(self, url: str, **kwargs: object) -> _Response:
        self.get_calls.append((url, kwargs))
        return _Response(200, content=b"zip-bytes")


def test_api_batch_upload_retries_and_does_not_send_bearer_to_signed_url(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    _write_pdf(pdf)
    record = build_source_records([str(pdf)])[0]
    session = _Session(
        [
            _Response(503, {"code": 0}),
            _Response(200, {"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://signed.example/upload"]}}),
        ]
    )
    client = MineruClient("secret-token", session=session, retry_attempts=2, sleep_fn=lambda _: None)
    submission = client.submit_file(record)

    assert submission.batch_id == "batch-1"
    assert len(session.request_calls) == 2
    assert session.request_calls[0][2]["headers"]["Authorization"] == "Bearer secret-token"
    assert session.put_calls[0][0] == "https://signed.example/upload"
    assert "headers" not in session.put_calls[0][1]


def test_api_poll_batch_forwards_mineru_extract_progress() -> None:
    class PollSession:
        def __init__(self) -> None:
            self.responses = [
                _Response(
                    200,
                    {
                        "code": 0,
                        "data": {
                            "extract_result": [
                                {"file_name": "paper.pdf", "state": "running", "extract_progress": {"progress": 42}}
                            ]
                        },
                    },
                ),
                _Response(
                    200,
                    {
                        "code": 0,
                        "data": {
                            "extract_result": [
                                {"file_name": "paper.pdf", "state": "done", "extract_progress": {"progress": 100}}
                            ]
                        },
                    },
                ),
            ]

        def request(self, method: str, url: str, **kwargs: object) -> _Response:  # noqa: ARG002
            return self.responses.pop(0)

    updates: list[list[TaskResult]] = []
    client = MineruClient("secret-token", session=PollSession(), sleep_fn=lambda _: None)
    results = client.poll_batch("batch-1", interval_seconds=0.1, progress_callback=updates.append)
    assert len(updates) == 2
    assert progress_percent(updates[0][0].progress) == 42
    assert results[0].state == "done"


def test_pipeline_resets_result_matching_between_50_file_chunks(tmp_path: Path) -> None:
    records: list[SourceRecord] = []
    for index in range(51):
        path = tmp_path / f"paper-{index}.pdf"
        path.write_bytes(f"pdf-{index}".encode())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append(
            SourceRecord(
                token=str(path),
                path=path,
                status="valid",
                size=path.stat().st_size,
                sha256=digest,
                data_id=f"data-{index}",
            )
        )

    class FakeClient:
        def __init__(self) -> None:
            self.submissions: dict[str, list[SourceRecord]] = {}
            self.calls: list[int] = []

        def submit_batch(self, chunk, options=None, upload_concurrency=2):  # noqa: ANN001, ARG002
            batch_id = f"batch-{len(self.calls) + 1}"
            chunk = list(chunk)
            self.calls.append(len(chunk))
            self.submissions[batch_id] = chunk
            return BatchSubmission(batch_id=batch_id, records=chunk)

        def poll_batch(self, batch_id):  # noqa: ANN001
            return [
                TaskResult(file_name=record.path.name, state="failed", data_id=record.data_id, error="fixture")
                for record in self.submissions[batch_id]
            ]

    client = FakeClient()
    results = convert_records(client, records, output_root=tmp_path / "outputs")
    assert client.calls == [50, 1]
    assert len(results) == 51
    assert sum(item["status"] == "failed" for item in results) == 51


def test_pipeline_success_writes_markdown_assets_and_secret_safe_manifest(tmp_path: Path) -> None:
    pdf = tmp_path / "success.pdf"
    _write_pdf(pdf)
    record = build_source_records([str(pdf)])[0]
    fixture_zip = tmp_path / "fixture.zip"
    with zipfile.ZipFile(fixture_zip, "w") as archive:
        archive.writestr("result/full.md", "# fixture\n\n![figure](images/figure.png)\n")
        archive.writestr("result/images/figure.png", b"image")

    class FixtureClient:
        def submit_batch(self, chunk, options=None, upload_concurrency=2):  # noqa: ANN001, ARG002
            return BatchSubmission(batch_id="fixture-batch", records=list(chunk))

        def poll_batch(self, batch_id):  # noqa: ANN001
            return [
                TaskResult(
                    file_name=record.path.name,
                    state="done",
                    full_zip_url="https://signed.example/result.zip?x=fixture-value",
                    data_id=record.data_id,
                    task_id="fixture-task",
                )
            ]

        def download_result(self, result, destination):  # noqa: ANN001
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fixture_zip, destination)
            return destination

        @staticmethod
        def normalize_bundle(zip_path, output_dir):  # noqa: ANN001
            return normalize_bundle(zip_path, output_dir)

    results = convert_records(FixtureClient(), [record], output_root=tmp_path / "outputs")
    assert results[0]["status"] == "done"
    output_dir = Path(results[0]["output_dir"])
    assert (output_dir / "success.md").is_file()
    assert (output_dir / "success_assets/figure.png").is_file()
    assert sorted(path.name for path in output_dir.iterdir()) == ["success.md", "success_assets"]
    serialized = json.dumps(results[0], ensure_ascii=False)
    assert "x=fixture-value" not in serialized


def test_obsidian_import_is_dry_run_by_default_and_uses_wikilinks(tmp_path: Path) -> None:
    result_dir = tmp_path / "paper-result"
    (result_dir / "assets").mkdir(parents=True)
    (result_dir / "assets/figure.png").write_bytes(b"image")
    absolute_image = (result_dir / "assets/figure.png").resolve()
    (result_dir / "full.md").write_text(
        f"# Paper\n\n![Figure](assets/figure.png)\n![Absolute]({absolute_image})\n"
        "![External](https://example.com/figure.png)\n",
        encoding="utf-8",
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    plan = create_import_plan(result_dir, vault, "Notes")

    assert plan.note_destination == vault / "Notes/paper-result.md"
    assert len(plan.assets) == 1
    assert plan.external_images == ["https://example.com/figure.png"]
    assert plan.missing_images == []
    assert not plan.note_destination.exists()

    backup = execute_import(plan, backup_root=tmp_path / "backups")
    assert backup is None
    assert plan.note_destination.read_text(encoding="utf-8") == (
        "# Paper\n\n![[99_Attachments/mineru/paper-result/figure.png]]\n"
        "![[99_Attachments/mineru/paper-result/figure.png]]\n"
        "![External](https://example.com/figure.png)\n"
    )
    assert (vault / "99_Attachments/mineru/paper-result/figure.png").is_file()

    conflicting = create_import_plan(result_dir, vault, "Notes")
    with pytest.raises(ObsidianImportError):
        execute_import(conflicting)
    backup = execute_import(conflicting, overwrite=True, backup_root=tmp_path / "backups")
    assert backup is not None and backup.is_dir()


def test_init_creates_private_env_file_without_overwriting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    created = write_environment("fresh-token", env_file)
    assert created == env_file
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    status = describe_environment(env_file)
    assert status["api_key_configured"] is True
    assert status["api_key_source"] == "env_file"
    assert "fresh-token" not in json.dumps(status)
    with pytest.raises(FileExistsError):
        write_environment("another-token", env_file)
    with pytest.raises(ValueError):
        write_environment("bad\nvalue", tmp_path / "other.env")


def test_gui_settings_remember_paths_atomically(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    saved = save_gui_settings(
        {
            "output_root": "/tmp/output",
            "obsidian_vault": "/tmp/vault",
            "obsidian_target": "Notes",
            "ignored": "not persisted",
        },
        settings_path,
    )
    assert saved == settings_path
    assert load_gui_settings(settings_path) == {
        "output_root": "/tmp/output",
        "obsidian_vault": "/tmp/vault",
        "obsidian_target": "Notes",
    }
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600


def test_paste_mode_flags_are_mutually_exclusive() -> None:
    parser = build_parser()
    serial = parser.parse_args(["paste", "--serial", "--dry-run"])
    batch = parser.parse_args(["paste", "--batch", "--dry-run"])
    default = parser.parse_args(["paste", "--dry-run"])
    assert serial.serial is True
    assert batch.serial is False
    assert default.serial is False


def test_obsidian_note_name_cannot_escape_target_directory(tmp_path: Path) -> None:
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / "full.md").write_text("# note", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ObsidianImportError):
        create_import_plan(result_dir, vault, "Notes", note_name="../../outside.md")


def test_wait_requires_exactly_one_job_identifier() -> None:
    client = MineruClient("secret", sleep_fn=lambda _: None)
    with pytest.raises(ValueError):
        client.wait()
    with pytest.raises(ValueError):
        client.wait(batch_id="b", task_id="t")
    result = TaskResult(file_name="paper.pdf", state="done", full_zip_url="https://signed.example/?secret=x")
    assert "secret=x" not in json.dumps(result.to_dict())
    assert result.to_dict()["has_full_zip_url"] is True
