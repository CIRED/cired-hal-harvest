"""Tests for hal_harvest: no network, HAL is simulated."""

import json
import shutil
from pathlib import Path

import pytest
import requests

import hal_harvest as hh


class FakeResponse:
    def __init__(self, payload=None, status=200, body=b"", headers=None):
        self.payload, self.status_code, self.body = payload, status, body
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def records(n, start=0):
    return [
        {"docid": i, "halId_s": f"hal-{i:08d}", "fileMain_s": f"https://x/{i}.pdf"} for i in range(start, n)
    ]


class FakeHAL:
    """Serve `docs` in cursor pages of `page` records; `fail_on` lists page indices that error."""

    def __init__(self, docs, page=2, num_found=None, fail_on=()):
        self.docs, self.page, self.fail_on = docs, page, set(fail_on)
        self.num_found = len(docs) if num_found is None else num_found
        self.headers = {}
        self.calls = 0

    def get(self, url, params, timeout):
        self.calls += 1
        cursor = params["cursorMark"]
        idx = 0 if cursor == "*" else int(cursor)
        if idx // self.page in self.fail_on:
            raise requests.ConnectionError("connection reset")
        chunk = self.docs[idx : idx + self.page]
        nxt = str(idx + len(chunk)) if chunk else cursor
        return FakeResponse({"response": {"numFound": self.num_found, "docs": chunk}, "nextCursorMark": nxt})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(hh.time, "sleep", lambda s: None)


def test_complete_harvest_returns_every_record():
    result = hh.harvest(FakeHAL(records(5)))
    assert result["total_records"] == 5
    assert result["hal_num_found"] == 5


def test_error_mid_pagination_raises_instead_of_truncating():
    hal = FakeHAL(records(6), fail_on={1})
    with pytest.raises(hh.IncompleteHarvestError):
        hh.harvest(hal)
    assert hal.calls == 1 + hh.API_RETRIES  # first page, then retries exhausted


def test_transient_error_is_retried(monkeypatch):
    hal = FakeHAL(records(4))
    real_get, failed = hal.get, []

    def flaky(url, params, timeout):
        if params["cursorMark"] == "2" and not failed:
            failed.append(1)
            raise requests.Timeout("slow")
        return real_get(url, params, timeout)

    hal.get = flaky
    assert hh.harvest(hal)["total_records"] == 4


def test_count_below_hal_total_raises():
    with pytest.raises(hh.IncompleteHarvestError, match="received 3 records, HAL reports 4"):
        hh.harvest(FakeHAL(records(3), num_found=4))


def test_incomplete_harvest_saves_nothing(tmp_path, monkeypatch):
    real_harvest = hh.harvest
    monkeypatch.setattr(hh, "harvest", lambda: real_harvest(FakeHAL(records(6), fail_on={2})))
    assert hh.main(["query", "--data", str(tmp_path)]) == 1
    assert not list(tmp_path.rglob("hal_response_*.json"))


def doc(docid, title, doc_type, doi=None):
    d = {
        "docid": docid,
        "halId_s": f"hal-{docid}",
        "title_s": [title],
        "label_s": title,
        "docType_s": doc_type,
    }
    if doi:
        d["doiId_s"] = doi
    return d


def test_prepare_drops_working_paper_when_published_version_exists():
    raw = {
        "response": {
            "docs": [
                doc(1, "Same Title!", "REPORT"),
                doc(2, "same title", "ART"),
                doc(3, "Alone", "UNDEFINED"),
            ]
        }
    }
    ids = [p["halId_s"] for p in hh.prepare(raw, "x")["publications"]]
    assert ids == ["hal-2", "hal-3"]


def test_prepare_keeps_best_record_per_doi_but_all_of_book_and_chapters():
    raw = {
        "response": {
            "docs": [
                doc(1, "a", "COMM", "10.1/x"),
                doc(2, "b", "ART", "10.1/x"),
                doc(3, "c", "ART", "10.1/x"),
                doc(4, "book", "OUV", "10.1/y"),
                doc(5, "chapter", "COUV", "10.1/y"),
            ]
        }
    }
    cat = hh.prepare(raw, "x")
    assert [p["halId_s"] for p in cat["publications"]] == ["hal-3", "hal-4", "hal-5"]
    assert cat["filtering_statistics"]["doi_duplicates_excluded"] == 2


class FakeDownloads:
    def __init__(self, bodies):
        self.bodies, self.headers = bodies, {}

    def get(self, url, stream, timeout):
        body = self.bodies[url]
        if isinstance(body, int):
            return FakeResponse(status=body)
        return FakeResponse(body=body, headers={"Content-Type": "application/octet-stream"})


def test_download_names_by_content_skips_present_and_reports_failures(tmp_path, monkeypatch):
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "hal-00000000.pdf").write_bytes(b"%PDF old")
    (tmp_path / "catalog").mkdir()
    bodies = {"https://x/1.pdf": b"%PDF-1.7 new", "https://x/2.pdf": b"PK\x03\x04zip", "https://x/3.pdf": 403}
    monkeypatch.setattr(hh.requests, "Session", lambda: FakeDownloads(bodies))
    stats = hh.download(tmp_path, {"publications": records(4)})
    assert stats == {"present": 1, "downloaded": 2, "failed": 1, "no_file": 0}
    assert sorted(p.name for p in docs.iterdir()) == [
        "hal-00000000.pdf",
        "hal-00000001.pdf",
        "hal-00000002.zip",
    ]
    failures = json.loads(next((tmp_path / "catalog").glob("download_failures_*.json")).read_text())
    assert failures[0]["halId_s"] == "hal-00000003"


def test_ris_record_maps_hal_fields_and_attaches_local_file(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "hal-03127371.pdf").write_bytes(b"%PDF")
    pub = {
        "halId_s": "hal-03127371",
        "docType_s": "ART",
        "title_s": ["Technology costs for the first wave of wind farms in Vietnam"],
        "authLastName_s": ["Ha-Duong"],
        "authFirstName_s": ["Minh"],
        "producedDate_tdate": "2023-06-01T00:00:00Z",
        "journalTitle_s": "Energy for Sustainable Development",
        "volume_s": "74",
        "page_s": "309-313",
        "doiId_s": "10.1016/j.esd.2023.04.010",
        "keyword_s": ["Vietnam", "Wind power"],
        "abstract_s": ["Line one.\nLine two."],
    }
    other = {"halId_s": "hal-2", "docType_s": "LECTURE", "label_s": "X &amp; Y", "authFullName_s": ["A B"]}
    hh.write_ris(tmp_path, {"publications": [pub, other]})
    text = (tmp_path / "cired_hal.ris").read_text()
    first, second = text.split("ER  - \n")[:2]
    for line in [
        "TY  - JOUR",
        "AU  - Ha-Duong, Minh",
        "PY  - 2023",
        "DA  - 2023/06/01",
        "T2  - Energy for Sustainable Development",
        "SP  - 309",
        "EP  - 313",
        "AB  - Line one. Line two.",
        "KW  - Wind power",
        "UR  - https://hal.science/hal-03127371",
        "L1  - documents/hal-03127371.pdf",
    ]:
        assert line in first.splitlines()
    assert "TY  - GEN" in second and "TI  - X & Y" in second and "L1" not in second


def test_ris_new_since_keeps_only_records_absent_from_earlier_catalogue(tmp_path):
    (tmp_path / "documents").mkdir()
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"publications": [{"halId_s": "hal-1"}]}))
    pubs = [{"halId_s": "hal-1", "title_s": ["Old"]}, {"halId_s": "hal-2", "title_s": ["New"]}]
    out = hh.write_ris(tmp_path, {"publications": pubs}, new_since=old)
    assert out.name == "cired_hal_new.ris"
    assert out.read_text().count("ER  - ") == 1 and "TI  - New" in out.read_text()


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("pdftotext"), reason="poppler-utils not installed")
def test_extract_converts_pdf_once(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir()
    # Minimal one-page PDF with the text "Hello CIRED".
    content = b"BT /F1 24 Tf 72 720 Td (Hello CIRED) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    pdf += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    (docs / "hal-1.pdf").write_bytes(pdf)
    assert hh.extract_text(tmp_path)["converted"] == 1
    assert "Hello CIRED" in (tmp_path / "text" / "hal-1.txt").read_text()
    assert hh.extract_text(tmp_path)["present"] == 1


@pytest.mark.adherence
def test_ruff_clean():
    import subprocess

    ruff = shutil.which("ruff")
    assert ruff, "ruff missing from the environment: uv sync --group dev"
    root = Path(hh.__file__).parent
    assert subprocess.run([ruff, "check", str(root)]).returncode == 0
