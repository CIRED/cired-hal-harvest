"""
Harvest the open-access CIRED publications deposited in HAL.

Standalone successor of cired.digital's src/intake/ pipeline (query.py,
prepare_catalog.py, download.py), kept for the lab once the chatbot was retired.
It has no dependency on the R2R stack.

Subcommands
-----------
query     Fetch the HAL records; refuse to save a harvest that is incomplete.
prepare   Deduplicate the latest raw harvest into a catalogue.
download  Fetch the full texts missing from data/documents/.
update    query + prepare + download + ris + extract, then log to data/harvests.tsv.
extract   Convert each PDF to plain text in data/text/ (needs pdftotext).
ris       Write data/cired_hal.ris for Zotero, full texts attached by local path.
status    Compare the latest catalogue with the files on disk.

The data directory defaults to data/ next to this file; override with
--data DIR.
"""

import argparse
import html
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

HAL_API_URL = "https://api.archives-ouvertes.fr/search/"
# The lab has had two structIds (135977 until 2019, 1042364 since 2020):
# querying the acronym covers both.
HAL_QUERY = "labStructAcronym_s:CIRED"
# Full text deposited in HAL itself (not mere links to external open access).
HAL_FILTER = "submitType_s:file"
HAL_FIELDS = (
    "docid,halId_s,doiId_s,label_s,producedDate_tdate,authFullName_s,title_s,"
    "abstract_s,submitType_s,docType_s,peerReviewing_t,labStructAcronym_s,fileMain_s,"
    # bibliographic fields for the RIS export
    "authLastName_s,authFirstName_s,journalTitle_s,journalIssn_s,volume_s,issue_s,page_s,"
    "publisher_s,bookTitle_s,conferenceTitle_s,keyword_s,language_s,uri_s"
)
HAL_BATCH_SIZE = 500
API_TIMEOUT = 60  # seconds
API_DELAY = 1  # seconds between API pages
API_RETRIES = 3
DOWNLOAD_TIMEOUT = 120  # seconds
DOWNLOAD_DELAY = 1  # seconds between downloads
USER_AGENT = "CIRED-HAL-harvester/1.0"

DEFAULT_DATA = Path(__file__).resolve().parent / "data"


class IncompleteHarvestError(RuntimeError):
    """The HAL harvest could not be completed; nothing must be saved."""


# --------------------------------------------------------------------- query


def _get_page(session: requests.Session, params: dict[str, Any]) -> dict[str, Any]:
    """Get one page from the HAL API, retrying transient failures."""
    last_error: Exception | None = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            response = session.get(HAL_API_URL, params=params, timeout=API_TIMEOUT)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data
        except (requests.RequestException, ValueError) as err:
            last_error = err
            logging.warning("HAL request failed (attempt %d/%d): %s", attempt, API_RETRIES, err)
            if attempt < API_RETRIES:
                time.sleep(5 * attempt)
    raise IncompleteHarvestError(f"HAL request failed after {API_RETRIES} attempts: {last_error}")


def harvest(session: requests.Session | None = None) -> dict[str, Any]:
    """
    Retrieve every matching HAL record, or raise IncompleteHarvestError.

    Deep paging uses Solr's cursorMark on a docid sort, so records added while
    the harvest runs cannot shift pages. The harvest is accepted only if the
    number of distinct records equals HAL's own count (numFound).
    """
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", USER_AGENT)
    base = {
        "q": HAL_QUERY,
        "fq": HAL_FILTER,
        "fl": HAL_FIELDS,
        "wt": "json",
        "rows": HAL_BATCH_SIZE,
        "sort": "docid asc",
    }
    docs: list[dict[str, Any]] = []
    cursor = "*"
    num_found: int | None = None
    while True:
        data = _get_page(session, {**base, "cursorMark": cursor})
        try:
            page = data["response"]["docs"]
            num_found = int(data["response"]["numFound"])
            next_cursor = data["nextCursorMark"]
        except (KeyError, TypeError, ValueError) as err:
            raise IncompleteHarvestError(f"Unexpected HAL reply: {err}") from err
        docs.extend(page)
        logging.info("Retrieved %d records, %d of %d so far", len(page), len(docs), num_found)
        if next_cursor == cursor or not page:
            break
        cursor = next_cursor
        time.sleep(API_DELAY)

    unique: dict[str, dict[str, Any]] = {}
    for doc in docs:
        hal_id = doc.get("halId_s")
        if not hal_id:
            logging.warning("Record without halId_s dropped: docid %s", doc.get("docid"))
            continue
        unique.setdefault(hal_id, doc)
    if num_found is None or len(docs) != num_found:
        raise IncompleteHarvestError(
            f"Harvest incomplete: received {len(docs)} records, HAL reports {num_found}"
        )
    records = sorted(unique.values(), key=lambda d: d["halId_s"])
    return {
        "query_timestamp": datetime.now().isoformat(timespec="seconds"),
        "query_params": {k: v for k, v in base.items() if k != "rows"},
        "hal_num_found": num_found,
        "total_records": len(records),
        "response": {"docs": records},
    }


def save_raw(data_dir: Path, result: dict[str, Any]) -> Path:
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"hal_response_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logging.info("Saved %d records to %s", result["total_records"], path)
    return path


# ------------------------------------------------------------------- prepare


def normalize_title(title: Any) -> str:
    if isinstance(title, list):
        title = title[0] if title else ""
    if not isinstance(title, str) or not title:
        return ""
    title = re.sub(r"[^\w\s]", "", title.lower())
    return re.sub(r"\s+", " ", title).strip()


WORKING_PAPER_TYPES = {"UNDEFINED", "OTHER", "REPORT"}
TYPE_PRIORITY = {"ART": 1, "COMM": 2, "UNDEFINED": 3}


def _is_working_paper(doc: dict[str, Any]) -> bool:
    doc_type = doc.get("docType_s")
    return isinstance(doc_type, str) and doc_type.upper() in WORKING_PAPER_TYPES


def prepare(raw: dict[str, Any], source: str) -> dict[str, Any]:
    """
    Deduplicate raw HAL records, as cired.digital's prepare_catalog.py did.

    1. Same normalised title: if any version is not a working paper
       (UNDEFINED/OTHER/REPORT), drop the working-paper versions.
    2. Same DOI: keep one record, preferring ART, then COMM, then UNDEFINED,
       then the highest docid; except a DOI carrying both OUV and COUV
       (a book and its chapters), where all records are kept.
    Records without DOI are all kept.
    """
    docs = [dict(d) for d in raw["response"]["docs"]]
    total = len(docs)
    for doc in docs:
        if isinstance(doc.get("label_s"), str):
            doc["label_s"] = html.unescape(doc["label_s"])
        if "producedDate_tdate" in doc:
            doc["producedDate_tdate"] = str(doc["producedDate_tdate"]).split("T")[0]

    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        by_title[normalize_title(doc.get("title_s", doc.get("label_s")))].append(doc)
    kept: list[dict[str, Any]] = []
    for group in by_title.values():
        not_wp = [d for d in group if not _is_working_paper(d)]
        kept.extend(not_wp or group)
    wp_excluded = total - len(kept)

    no_doi = [d for d in kept if not d.get("doiId_s")]
    by_doi: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in kept:
        if doc.get("doiId_s"):
            by_doi[doc["doiId_s"]].append(doc)
    final = list(no_doi)
    for group in by_doi.values():
        if {"OUV", "COUV"} <= {d.get("docType_s") for d in group}:
            final.extend(group)
        else:
            final.append(
                min(group, key=lambda d: (TYPE_PRIORITY.get(d.get("docType_s"), 99), -int(d.get("docid", 0))))
            )
    return {
        "processing_timestamp": datetime.now().isoformat(timespec="seconds"),
        "source_file": source,
        "filtering_statistics": {
            "total_retrieved": total,
            "working_papers_excluded": wp_excluded,
            "doi_duplicates_excluded": len(kept) - len(final),
            "final_count": len(final),
        },
        "publications": sorted(final, key=lambda d: d.get("halId_s", "")),
    }


def latest(directory: Path, pattern: str) -> Path | None:
    files = sorted(directory.glob(pattern))  # timestamped names sort chronologically
    return files[-1] if files else None


def save_catalog(data_dir: Path, catalog: dict[str, Any]) -> Path:
    cat_dir = data_dir / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    path = cat_dir / f"catalog_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    logging.info("Catalogue: %s -> %s", catalog["filtering_statistics"], path)
    return path


# ------------------------------------------------------------------ download


def safe_name(hal_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in hal_id)


def _extension(head: bytes, content_type: str) -> str:
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        return ".zip"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head[4:8] == b"ftyp":
        return ".mp4"
    import mimetypes

    return mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"


def download_one(session: requests.Session, url: str, stem: Path) -> Path:
    """Download url to stem + detected extension, atomically."""
    part = stem.with_suffix(".part")
    with session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        with part.open("wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        content_type = r.headers.get("Content-Type", "")
    with part.open("rb") as f:
        head = f.read(16)
    target = stem.with_name(stem.name + _extension(head, content_type))
    part.rename(target)
    return target


def download(data_dir: Path, catalog: dict[str, Any], limit: int | None = None) -> dict[str, Any]:
    docs_dir = data_dir / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for stale in docs_dir.glob("*.part"):
        stale.unlink()
    present = {p.stem for p in docs_dir.iterdir() if p.is_file()}
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    stats = {"present": 0, "downloaded": 0, "failed": 0, "no_file": 0}
    failures: list[dict[str, str]] = []
    attempts = 0
    for pub in catalog["publications"]:
        url, hal_id = pub.get("fileMain_s"), pub.get("halId_s")
        if not url or not hal_id:
            stats["no_file"] += 1
            continue
        stem = safe_name(hal_id)
        if stem in present:
            stats["present"] += 1
            continue
        if limit is not None and attempts >= limit:
            break
        attempts += 1
        try:
            path = download_one(session, url, docs_dir / stem)
            stats["downloaded"] += 1
            logging.info("Downloaded %s", path.name)
        except requests.RequestException as err:
            stats["failed"] += 1
            failures.append({"halId_s": hal_id, "url": url, "error": str(err)})
            logging.warning("Failed %s: %s", hal_id, err)
            (docs_dir / f"{stem}.part").unlink(missing_ok=True)
        time.sleep(DOWNLOAD_DELAY)
    if failures:
        report = data_dir / "catalog" / f"download_failures_{datetime.now():%Y%m%d_%H%M%S}.json"
        report.write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logging.warning("%d failures listed in %s (HTTP 403 usually means an embargo)", len(failures), report)
    logging.info("Download summary: %s", stats)
    return stats


# -------------------------------------------------------------------- status


def status(data_dir: Path) -> dict[str, Any]:
    cat_path = latest(data_dir / "catalog", "catalog_*.json")
    if not cat_path:
        raise SystemExit("No catalogue yet: run `update` first.")
    catalog = json.loads(cat_path.read_text(encoding="utf-8"))
    wanted = {safe_name(p["halId_s"]) for p in catalog["publications"] if p.get("fileMain_s")}
    on_disk = {p.stem for p in (data_dir / "documents").glob("*") if p.is_file()}
    result = {
        "catalogue": cat_path.name,
        "catalogue_records": len(catalog["publications"]),
        "with_full_text": len(wanted),
        "files_on_disk": len(on_disk),
        "missing": sorted(wanted - on_disk),
        "not_in_catalogue": sorted(on_disk - wanted),
    }
    return result


# ----------------------------------------------------------------------- ris

RIS_TYPES = {
    "ART": "JOUR",
    "COMM": "CPAPER",
    "POSTER": "CPAPER",
    "PROCEEDINGS": "CONF",
    "COUV": "CHAP",
    "OUV": "BOOK",
    "THESE": "THES",
    "HDR": "THES",
    "MEM": "THES",
    "REPORT": "RPRT",
    "UNDEFINED": "RPRT",  # HAL's "preprint, working paper"
    "IMG": "FIGURE",
    "VIDEO": "VIDEO",
    "SOFTWARE": "COMP",
    "BLOG": "BLOG",
    "NOTICE": "ENCYC",
    "ISSUE": "JFULL",
}
CONTAINER_FIELD = {"journalTitle_s", "bookTitle_s", "conferenceTitle_s"}


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return "" if value is None else str(value)


def ris_record(pub: dict[str, Any], attachment: str | None) -> list[str]:
    """One RIS record for a HAL publication; attachment is a path relative to the RIS file."""
    lines = [("TY", RIS_TYPES.get(_first(pub.get("docType_s")), "GEN"))]
    lines.append(("TI", _first(pub.get("title_s")) or html.unescape(_first(pub.get("label_s")))))
    last, first = pub.get("authLastName_s") or [], pub.get("authFirstName_s") or []
    if last and len(last) == len(first):
        lines += [("AU", f"{ln}, {fn}") for ln, fn in zip(last, first, strict=True)]
    else:
        lines += [("AU", name) for name in pub.get("authFullName_s") or []]
    date = _first(pub.get("producedDate_tdate")).split("T")[0]
    if date[:4].isdigit():
        lines.append(("PY", date[:4]))
        lines.append(("DA", date.replace("-", "/")))
    container = next((_first(pub[f]) for f in CONTAINER_FIELD if pub.get(f)), "")
    for tag, value in (
        ("T2", container),
        ("SN", _first(pub.get("journalIssn_s"))),
        ("VL", _first(pub.get("volume_s"))),
        ("IS", _first(pub.get("issue_s"))),
        ("PB", _first(pub.get("publisher_s"))),
        ("LA", _first(pub.get("language_s"))),
        ("DO", _first(pub.get("doiId_s"))),
        ("AB", _first(pub.get("abstract_s"))),
    ):
        if value:
            lines.append((tag, value))
    pages = _first(pub.get("page_s"))
    if pages:
        start, _, end = pages.partition("-")
        lines.append(("SP", start.strip()))
        if end.strip():
            lines.append(("EP", end.strip()))
    lines += [("KW", kw) for kw in pub.get("keyword_s") or []]
    hal_id = pub["halId_s"]
    lines.append(("UR", f"https://hal.science/{hal_id}"))
    lines.append(("AN", hal_id))
    lines.append(("DB", "HAL"))
    if attachment:
        lines.append(("L1", attachment))
    out = [f"{tag}  - {' '.join(str(value).split())}" for tag, value in lines]
    return out + ["ER  - ", ""]


def write_ris(data_dir: Path, catalog: dict[str, Any], new_since: Path | None = None) -> Path:
    """
    Write the catalogue as RIS, each record pointing to its downloaded file.

    Zotero resolves L1 paths relative to the RIS file and copies the files into
    its storage on import, so the RIS must stay next to documents/ when imported.
    With new_since (an earlier catalogue, e.g. the one last imported), only the
    records absent from it are written, to data/cired_hal_new.ris.
    """
    files = {p.stem: p for p in (data_dir / "documents").glob("*") if p.is_file()}
    pubs = catalog["publications"]
    out = data_dir / "cired_hal.ris"
    if new_since:
        old = json.loads(new_since.read_text(encoding="utf-8"))
        seen = {p.get("halId_s") for p in old["publications"]}
        pubs = [p for p in pubs if p.get("halId_s") not in seen]
        out = data_dir / "cired_hal_new.ris"
    attached = 0
    with out.open("w", encoding="utf-8") as f:
        for pub in pubs:
            path = files.get(safe_name(pub.get("halId_s", "")))
            rel = f"documents/{path.name}" if path else None
            attached += bool(rel)
            f.write("\n".join(ris_record(pub, rel)) + "\n")
    logging.info("RIS: %d records, %d with full text, in %s", len(pubs), attached, out)
    return out


# ------------------------------------------------------------------- extract


def extract_text(data_dir: Path) -> dict[str, int]:
    """
    Convert new PDFs to UTF-8 text with poppler's pdftotext, layout dropped.

    The text files make the collection searchable with grep or ripgrep, and
    readable by any tool, without an LLM. Scanned PDFs without a text layer
    yield (near) empty files; they are counted, not OCRed.
    """
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise SystemExit("pdftotext not found: install poppler-utils.")
    text_dir = data_dir / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    stats = {"converted": 0, "present": 0, "failed": 0, "empty": 0}
    for pdf in sorted((data_dir / "documents").glob("*.pdf")):
        txt = text_dir / f"{pdf.stem}.txt"
        if txt.exists() and txt.stat().st_mtime >= pdf.stat().st_mtime:
            stats["present"] += 1
            continue
        result = subprocess.run(
            [pdftotext, "-enc", "UTF-8", "-q", str(pdf), str(txt)], check=False, timeout=300
        )
        if result.returncode != 0:
            stats["failed"] += 1
            logging.warning("pdftotext failed on %s", pdf.name)
            txt.unlink(missing_ok=True)
            continue
        stats["converted"] += 1
        if txt.stat().st_size < 500:
            stats["empty"] += 1
            logging.info("No text layer (scanned?): %s", pdf.name)
    logging.info("Text extraction: %s", stats)
    return stats


# ----------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command", choices=["query", "prepare", "download", "ris", "extract", "update", "status"]
    )
    parser.add_argument(
        "--data", type=Path, default=DEFAULT_DATA, help="data directory (default: %(default)s)"
    )
    parser.add_argument("--limit", type=int, help="download at most N new files")
    parser.add_argument(
        "--new-since",
        type=Path,
        metavar="CATALOGUE",
        help="ris: export only records absent from this earlier catalogue",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    data_dir: Path = args.data

    if args.command == "extract":
        extract_text(data_dir)
        return 0

    if args.command == "status":
        s = status(data_dir)
        for key, value in s.items():
            if isinstance(value, list):
                shown = " ".join(value[:20]) + (" ..." if len(value) > 20 else "")
                logging.info("%s: %d %s", key, len(value), shown)
            else:
                logging.info("%s: %s", key, value)
        return 0

    if args.command in ("query", "update"):
        try:
            raw_path = save_raw(data_dir, harvest())
        except IncompleteHarvestError as err:
            logging.error("%s. Nothing saved; previous harvest kept. Retry later.", err)
            return 1
        if args.command == "query":
            return 0
    if args.command in ("prepare", "update"):
        raw_path = latest(data_dir / "raw", "hal_response_*.json")
        if not raw_path:
            logging.error("No raw harvest in %s: run `query` first.", data_dir / "raw")
            return 1
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        cat_path = save_catalog(data_dir, prepare(raw, raw_path.name))
        if args.command == "prepare":
            return 0
    cat_path = latest(data_dir / "catalog", "catalog_*.json")
    if not cat_path:
        logging.error("No catalogue: run `prepare` first.")
        return 1
    catalog = json.loads(cat_path.read_text(encoding="utf-8"))
    if args.command == "ris":
        write_ris(data_dir, catalog, args.new_since)
        return 0
    stats = download(data_dir, catalog, args.limit)
    if args.command == "update":
        write_ris(data_dir, catalog)
        if shutil.which("pdftotext"):
            extract_text(data_dir)
        else:
            logging.warning("pdftotext not found: text extraction skipped.")
        s = status(data_dir)
        log = data_dir / "harvests.tsv"
        new = not log.exists()
        with log.open("a", encoding="utf-8") as f:
            if new:
                f.write("date\thal_records\tcatalogue\tfiles_on_disk\tdownloaded\tfailed\tnot_in_catalogue\n")
            f.write(
                f"{datetime.now():%Y-%m-%d}\t{catalog['filtering_statistics']['total_retrieved']}\t"
                f"{s['catalogue_records']}\t{s['files_on_disk']}\t{stats['downloaded']}\t"
                f"{stats['failed']}\t{len(s['not_in_catalogue'])}\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
