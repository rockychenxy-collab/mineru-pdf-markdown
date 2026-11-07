from pathlib import Path

from mineru_converter.document_roles import document_stem, inspect_pdf
from mineru_converter.models import SourceRecord
from mineru_converter.pipeline import output_markdown_name, output_assets_name
from mineru_converter.obsidian import create_import_plan
from mineru_converter.literature import source_frontmatter
from mineru_converter.cli import build_parser
from mineru_converter.pipeline import _match_record
from mineru_converter.models import TaskResult
from mineru_converter.models import BatchSubmission
from mineru_converter.pipeline import convert_records


def test_same_paper_attachments_never_share_document_or_asset_names():
    main = SourceRecord("main", Path("same.pdf"), "valid", sha256="1" * 64, citation_key="author2026", zotero_attachment_key="MAIN0001", document_role="main")
    supplement = SourceRecord("supp", Path("same.pdf"), "valid", sha256="2" * 64, citation_key="author2026", zotero_attachment_key="SUPP0001", document_role="supplement")
    assert output_markdown_name(main) == "author2026.md"
    assert output_markdown_name(supplement) == "author2026__补充_SUPP0001.md"
    assert output_assets_name(main) != output_assets_name(supplement)
    assert document_stem("author2026", "supplement", "SUPP0001") != document_stem("author2026", "supplement", "SUPP0002")


def test_main_and_supplement_import_to_separate_files_with_one_parent_card(tmp_path):
    vault = tmp_path / "vault"; (vault / "10_Literature").mkdir(parents=True)
    (vault / "10_Literature/author2026.md").write_text("# Parent card")
    plans = []
    for role, key in [("main", "MAIN0001"), ("supplement", "SUPP0001")]:
        output = tmp_path / role; output.mkdir()
        (output / "source.md").write_text(source_frontmatter("# Extracted text", {"citation_key": "author2026", "document_role": role, "zotero_attachment_key": key}))
        plans.append(create_import_plan(output, vault, "10_Literature/全文"))
    assert plans[0].note_destination.name == "author2026__全文.md"
    assert plans[1].note_destination.name == "author2026__补充_SUPP0001__全文.md"
    assert plans[0].attachment_dir != plans[1].attachment_dir
    assert all("[[10_Literature/author2026|" in p.rewritten_markdown for p in plans)


def test_role_inspection_handles_supplement_and_publisher_cover(monkeypatch, tmp_path):
    import pypdf
    class Page:
        def __init__(self, text): self.text = text
        def extract_text(self): return self.text
    class Reader:
        def __init__(self, path, strict=False):
            self.pages = [Page("Supplementary information\nPaper title")] if path.name == "supp.pdf" else [Page("Publisher cover\nPaper title"), Page("Abstract\nResearch description")]
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    assert inspect_pdf(tmp_path / "supp.pdf")["document_role"] == "supplement"
    assert inspect_pdf(tmp_path / "main.pdf")["document_role"] == "main"


def test_queue_cli_accepts_batch_with_per_attachment_metadata():
    args = build_parser().parse_args(["gui", "--queue-file", "queue.json", "--vault", "vault", "--target", "10_Literature/全文"])
    assert args.queue_file == "queue.json" and not args.paths


def test_out_of_order_same_filename_results_require_identity():
    records = [SourceRecord("a", Path("a/same.pdf"), "valid", data_id="main"), SourceRecord("b", Path("b/same.pdf"), "valid", data_id="supp")]
    assert _match_record(TaskResult("same.pdf", "done", data_id="supp"), records, set())[0] == 1
    assert _match_record(TaskResult("same.pdf", "done"), records, set()) is None
    assert _match_record(TaskResult("same.pdf", "done"), records[:1], set())[0] == 0


def test_ambiguous_cloud_results_are_reported_without_download(tmp_path):
    records = [SourceRecord("a", Path("a/same.pdf"), "valid", sha256="a" * 64, data_id="main"), SourceRecord("b", Path("b/same.pdf"), "valid", sha256="b" * 64, data_id="supp")]
    class Client:
        def submit_batch(self, chunk, **kwargs): return BatchSubmission("batch", list(chunk))
        def poll_batch(self, batch): return [TaskResult("same.pdf", "done", full_zip_url="https://example.invalid/fixture.zip")]
        def download_result(self, *args): raise AssertionError("Ambiguous files must never be downloaded or relabelled")
    results = convert_records(Client(), records, output_root=tmp_path / "out")
    assert len(results) == 2 and all(r["status"] == "failed" for r in results)
    assert all("逐个转换" in r["error"] for r in results)
