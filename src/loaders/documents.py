"""공시 원문 ZIP을 디스크에 보관하고 DuckDB document 테이블에 메타데이터를 적는다.

왜 ZIP을 DuckDB에 넣지 않고 파일로 두는가
- raw_api_response는 JSON 컬럼이라 바이너리를 넣을 수 없다
- 사업보고서 원문은 수 MB다. 618개를 BLOB으로 넣으면 DuckDB 파일이 수 GB로 커져 매일 쓰기·백업이 느려진다
- 원칙은 같다: 받은 원본은 그대로 지키고, 파싱(다음 PR)은 여기서 언제든 다시 한다

파일 위치: .env의 DOCUMENTS_PATH (없으면 저장소 루트의 data/documents). 파일 이름은 {rcept_no}.zip.
data/는 .gitignore에 있어 커밋되지 않는다. 노트북을 잃으면 백필 스크립트로 다시 받는다(API 618건, 한도의 3%).

멱등성: 같은 rcept_no를 다시 저장하면 파일을 덮어쓰고 행을 갱신한다. 내용이 바뀌었는지는 sha256으로 안다.

CLI (저장소 루트에서, API 호출 없음):
    python -m src.loaders.documents inspect 20240318000xxx   # 저장된 ZIP의 파일 목록·인코딩·XML 앞부분 출력
    python -m src.loaders.documents status                   # 회사별 정기보고서 수 대비 원문 보관 수
    python -m src.loaders.documents tree 20240318000xxx      # 한 원문의 섹션 트리를 파싱해 출력 (DB에 쓰지 않음)
    python -m src.loaders.documents parse                    # 저장된 ZIP 전체 → document_section·document_parse 재생성
    python -m src.loaders.documents why-lxml [rcept_no ...]  # lxml(3단계)로 간 파일들의 2단계 실패 원인을 종류별로 모아 출력
    python -m src.loaders.documents section <rcept_no> <file_name> <제목 일부>  # 한 섹션의 문단·표 모양 출력
    python -m src.loaders.documents raw <rcept_no> <file_name> <찾을 글자> [앞뒤 줄 수]  # 원문 XML에서 그 글자가 있는 줄 앞뒤 출력

파싱 규칙은 src/loaders/document_parser.py.
"""

import hashlib
import json
import os
import sys
import zipfile
import io
from pathlib import Path

import duckdb
from dotenv import load_dotenv

from src.collectors.document import ZipEntry, inspect_zip
from src.loaders.document_parser import ParsedFile, explain_failure, normalize_encoding, parse_document_xml, section_rows, table_stats
from src.loaders.duckdb_store import REPO_ROOT, connect
from src.loaders.periodic_reports import periodic_reports_of

DEFAULT_DOCUMENTS_DIR = REPO_ROOT / "data" / "documents"


def documents_dir() -> Path:
    load_dotenv()
    return Path(os.getenv("DOCUMENTS_PATH") or DEFAULT_DOCUMENTS_DIR)


def document_path(rcept_no: str) -> Path:
    return documents_dir() / f"{rcept_no}.zip"


def save_document(con: duckdb.DuckDBPyConnection, rcept_no: str, zip_bytes: bytes) -> dict:
    """ZIP을 파일로 쓰고 document 행을 upsert한다. 돌려주는 값은 저장 결과 요약.

    회사·보고서명은 disclosure 테이블에서 가져온다(없으면 NULL). 원문은 접수번호만으로 받을 수 있으므로
    공시 목록에 없는 접수번호도 저장은 되지만, 정상 흐름에서는 항상 목록에 있는 보고서만 받는다.
    """
    path = document_path(rcept_no)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zip_bytes)

    entries = inspect_zip(zip_bytes)
    xml_encodings = sorted({e.encoding for e in entries if e.encoding})
    sha256 = hashlib.sha256(zip_bytes).hexdigest()
    con.execute(
        """
        INSERT INTO document (rcept_no, corp_code, report_nm, file_path, file_size, sha256, entry_count, entries, xml_encoding, fetched_at)
        SELECT ?, d.corp_code, d.report_nm, ?, ?, ?, ?, ?, ?, now()
        FROM (SELECT 1) one LEFT JOIN disclosure d ON d.rcept_no = ?
        ON CONFLICT (rcept_no) DO UPDATE SET
            corp_code = coalesce(excluded.corp_code, document.corp_code),
            report_nm = coalesce(excluded.report_nm, document.report_nm),
            file_path = excluded.file_path, file_size = excluded.file_size, sha256 = excluded.sha256,
            entry_count = excluded.entry_count, entries = excluded.entries, xml_encoding = excluded.xml_encoding,
            fetched_at = excluded.fetched_at
        """,
        [rcept_no, str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path),
         len(zip_bytes), sha256, len(entries),
         json.dumps([{"name": e.name, "size": e.size, "encoding": e.encoding} for e in entries], ensure_ascii=False),
         ",".join(xml_encodings) or None, rcept_no],
    )
    return {"rcept_no": rcept_no, "file_size": len(zip_bytes), "entries": len(entries),
            "xml_encoding": ",".join(xml_encodings) or None, "sha256": sha256[:12]}


def save_missing_document(con: duckdb.DuckDBPyConnection, rcept_no: str, status: str, message: str | None) -> dict:
    """DART가 '파일 없음'(014) 등으로 원문을 주지 않은 접수번호를 기록한다. 다음 백필이 다시 부르지 않게 하기 위해서다.

    2026-11-24 확인: 사업보고서 190건 중 3건이 014였고 모두 [첨부정정]이었다. 첨부만 고친 정정 공시는 본문 원문이 따로 없다.
    """
    con.execute(
        """
        INSERT INTO document (rcept_no, corp_code, report_nm, file_path, file_size, sha256, entry_count, entries, xml_encoding,
                              fetched_at, status, message)
        SELECT ?, d.corp_code, d.report_nm, '', 0, '', 0, '[]', NULL, now(), ?, ?
        FROM (SELECT 1) one LEFT JOIN disclosure d ON d.rcept_no = ?
        ON CONFLICT (rcept_no) DO UPDATE SET status = excluded.status, message = excluded.message, fetched_at = excluded.fetched_at
        """,
        [rcept_no, status, message, rcept_no],
    )
    return {"rcept_no": rcept_no, "status": status, "file_size": 0, "no_file": True}


def missing_documents(con: duckdb.DuckDBPyConnection, corp_codes: list[str], from_year: int,
                      reprt_codes: set[str] | None = None) -> list[dict]:
    """공시 목록의 정기보고서 중 원문이 아직 없는 것. 최근 접수가 먼저.

    주요정보 API(Phase 2)와 달리 정정본은 접수번호가 다르고 원문도 다르므로 **정정본도 각각** 받는다.
    reprt_codes로 종류를 좁힐 수 있다 (예: {"11011"} 사업보고서만 — 임베딩 범위를 줄일 때).
    """
    have = {r[0] for r in con.execute("SELECT rcept_no FROM document").fetchall()}
    rows = []
    for corp_code in corp_codes:
        for r in periodic_reports_of(con, corp_code):
            if int(r["bsns_year"]) < from_year or r["rcept_no"] in have:
                continue
            if reprt_codes and r["reprt_code"] not in reprt_codes:
                continue
            rows.append({**r, "corp_code": corp_code})
    rows.sort(key=lambda r: (r["rcept_dt"], r["rcept_no"]), reverse=True)
    return rows


def missing_documents_of_report(con: duckdb.DuckDBPyConnection, corp_code: str, bsns_year: str, reprt_code: str) -> list[dict]:
    """한 회사의 한 보고서(회사 × 보고서 파티션)에서 원문이 아직 없는 접수번호. 정정본도 각각 (Dagster documents 자산용).

    주요정보 API와 달리 원문은 정정본마다 다르므로 최신 정정본만 받지 않고 목록에 있는 접수번호를 모두 받는다.
    """
    have = {r[0] for r in con.execute("SELECT rcept_no FROM document").fetchall()}
    return [r for r in periodic_reports_of(con, corp_code)
            if (r["bsns_year"], r["reprt_code"]) == (bsns_year, reprt_code) and r["rcept_no"] not in have]


def unparsed_documents(con: duckdb.DuckDBPyConnection) -> list[str]:
    """정상 수신(status '000')했지만 document_parse 행이 하나도 없는 접수번호. 새로 받은 원문을 파싱할 때 쓴다 (chunks 자산)."""
    rows = con.execute(
        "SELECT d.rcept_no FROM document d ANTI JOIN document_parse p USING (rcept_no) WHERE d.status = '000' ORDER BY d.rcept_no"
    ).fetchall()
    return [r[0] for r in rows]


def target_corp_codes(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("SELECT corp_code FROM target_corp ORDER BY corp_code").fetchall()]


def read_xml_head(rcept_no: str, entry_name: str | None = None, nbytes: int = 1500) -> tuple[ZipEntry, str]:
    """저장된 ZIP에서 XML 하나의 앞부분을 글자로 읽어 돌려준다 (형식 확인용).

    선언된 인코딩으로 먼저 풀고, 없거나 실패하면 utf-8 → cp949 순으로 시도한다.
    DART 원문 인코딩이 무엇인지는 노트북에서 실제 파일로 확인한다.
    """
    zip_bytes = document_path(rcept_no).read_bytes()
    entries = inspect_zip(zip_bytes)
    xmls = [e for e in entries if e.name.lower().endswith(".xml")]
    target = next((e for e in entries if e.name == entry_name), None) if entry_name else (xmls[0] if xmls else entries[0])
    if target is None:
        raise FileNotFoundError(f"{entry_name}이 ZIP에 없습니다: {[e.name for e in entries]}")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf, zf.open(target.name) as f:
        head = f.read(nbytes)
    for enc in [target.encoding, "utf-8", "cp949"]:
        if not enc:
            continue
        try:
            return target, head.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return target, head.decode("utf-8", errors="replace")


def parse_zip(rcept_no: str, zip_bytes: bytes) -> tuple[list[ParsedFile], list[tuple[str, str]]]:
    """ZIP 안 모든 XML을 파싱한다. 돌려주는 값: (성공한 파일들, [(파일 이름, 오류 메시지)]).

    본문과 첨부를 모두 파싱한다. 첨부(감사보고서 등)의 주석도 근거 문서가 될 수 있다 (0002).
    깨진 XML은 그 파일만 실패로 남기고 나머지는 계속한다.
    """
    parsed, failed = [], []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith(".xml"):
                continue
            try:
                parsed.append(parse_document_xml(name, zf.read(name)))
            except Exception as e:  # ParseError 등. 파일 하나의 실패가 전체를 멈추지 않게
                failed.append((name, f"{type(e).__name__}: {str(e)[:300]}"))
    return parsed, failed


def rebuild_sections(con: duckdb.DuckDBPyConnection, rcept_nos: list[str] | None = None) -> dict:
    """document 테이블의 ZIP을 모두 읽어 document_section·document_parse를 다시 만든다. API 호출 없음.

    rcept_nos를 주면 그 원문만 다시 만든다(그 접수번호의 기존 행은 지움). 없으면 전체 재생성.
    """
    if rcept_nos is None:
        rows = con.execute("SELECT rcept_no FROM document WHERE status = '000' ORDER BY rcept_no").fetchall()
        rcept_nos = [r[0] for r in rows]
    stats = {"documents": len(rcept_nos), "files": 0, "files_failed": 0, "sections": 0, "tables": 0, "missing_zip": 0,
             "by_parser": {}}
    con.begin()
    try:
        if rcept_nos:
            con.execute("DELETE FROM document_section WHERE rcept_no IN (SELECT unnest(?::VARCHAR[]))", [rcept_nos])
            con.execute("DELETE FROM document_parse WHERE rcept_no IN (SELECT unnest(?::VARCHAR[]))", [rcept_nos])
        for rcept_no in rcept_nos:
            path = document_path(rcept_no)
            if not path.exists():
                stats["missing_zip"] += 1
                con.execute("INSERT INTO document_parse (rcept_no, file_name, ok, error, parsed_at) VALUES (?, ?, false, ?, now())",
                            [rcept_no, "", f"ZIP 없음: {path}"])
                continue
            parsed, failed = parse_zip(rcept_no, path.read_bytes())
            for pf in parsed:
                rows = section_rows(rcept_no, pf)
                if rows:
                    con.executemany("INSERT INTO document_section VALUES (" + ",".join("?" * 18) + ")", rows)
                n_tables = sum(len(s.tables) for s in pf.sections)
                n_chars = sum(len(s.text) for s in pf.sections)
                con.execute("""INSERT INTO document_parse (rcept_no, file_name, ok, doc_kind, doc_kind_name, section_count, table_count,
                                                           char_count, parsed_at, parser) VALUES (?, ?, true, ?, ?, ?, ?, ?, now(), ?)""",
                            [rcept_no, pf.file_name, pf.doc_kind, pf.doc_kind_name, len(pf.sections), n_tables, n_chars, pf.parser])
                stats["files"] += 1; stats["sections"] += len(pf.sections); stats["tables"] += n_tables
                stats["by_parser"][pf.parser] = stats["by_parser"].get(pf.parser, 0) + 1
            for name, err in failed:
                con.execute("INSERT INTO document_parse (rcept_no, file_name, ok, error, parsed_at) VALUES (?, ?, false, ?, now())",
                            [rcept_no, name, err])
                stats["files"] += 1; stats["files_failed"] += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    return stats


def print_tree(rcept_no: str, file_name: str | None = None) -> None:
    """한 원문을 파싱해 섹션 트리를 출력한다 (DB에 쓰지 않음). 노트북에서 파서 결과를 눈으로 확인하는 용도."""
    parsed, failed = parse_zip(rcept_no, document_path(rcept_no).read_bytes())
    for name, err in failed:
        print(f"실패 {name}: {err}")
    for pf in parsed:
        if file_name and pf.file_name != file_name:
            continue
        print(f"\n=== {pf.file_name}  [{pf.doc_kind}] {pf.doc_kind_name}  {pf.company_name}  섹션 {len(pf.sections)}  파서 {pf.parser}")
        for s in pf.sections:
            mark = "" if s.explicit else "·"
            note = " (주석)" if s.is_note else ""
            print(f"{'  ' * s.depth}{mark}{s.title[:50]}{note}  [문단 {len(s.paragraphs)}, 표 {len(s.tables)}, {len(s.text):,}자]")


def why_lxml(con: duckdb.DuckDBPyConnection, rcept_nos: list[str] | None = None) -> dict[str, list[tuple[str, str, int, str]]]:
    """3단계(lxml)로 파싱된 파일마다 2단계가 실패한 원인을 모은다. 결과: {오류 종류: [(rcept_no, 파일, 줄, 앞뒤 글자)]}.

    원인별 건수가 많은 것부터 전처리 규칙을 추가하면 lxml 비율이 내려간다 (0007). API 호출 없음.
    """
    sql = "SELECT rcept_no, file_name FROM document_parse WHERE parser LIKE 'lxml%'"  # lxml, lxml+cp949
    params: list = []
    if rcept_nos:
        sql += " AND rcept_no IN (" + ",".join("?" * len(rcept_nos)) + ")"
        params = list(rcept_nos)
    by_kind: dict[str, list[tuple[str, str, int, str]]] = {}
    for rcept_no, file_name in con.execute(sql + " ORDER BY 1, 2", params).fetchall():
        path = document_path(rcept_no)
        if not path.exists():
            continue
        with zipfile.ZipFile(path) as zf:
            found = explain_failure(zf.read(file_name))
        if found is None:  # 코드가 바뀌어 지금은 2단계가 되는 파일. parse를 다시 돌리면 사라진다
            by_kind.setdefault("(지금은 2단계 성공)", []).append((rcept_no, file_name, 0, ""))
            continue
        kind, lineno, snippet = found
        by_kind.setdefault(kind, []).append((rcept_no, file_name, lineno, snippet))
    return by_kind


def print_section(rcept_no: str, file_name: str, title_part: str, max_rows: int = 3) -> None:
    """제목에 title_part가 들어가는 섹션의 문단 앞부분과 표 모양(행×열, 첫 행)을 출력한다 (DB에 쓰지 않음)."""
    parsed, _ = parse_zip(rcept_no, document_path(rcept_no).read_bytes())
    for pf in parsed:
        if pf.file_name != file_name:
            continue
        for s in pf.sections:
            if title_part not in s.title:
                continue
            print(f"\n=== [{s.section_id}] {s.path}  (문단 {len(s.paragraphs)}, 표 {len(s.tables)}, 파서 {pf.parser})")
            for p in s.paragraphs[:max_rows]:
                print(f"  문단: {p[:120]}")
            for i, t in enumerate(s.tables, 1):
                ncols = max((len(r) for r in t.rows), default=0)
                st = table_stats(t)
                print(f"  표 {i}: {len(t.rows)}행 × {ncols}열, 머리행 {t.header_rows}, 캡션 {t.caption!r}, 단위 {t.unit!r},"
                      f" 글자 칸 {st['cells']}개 평균 {st['avg_len']}자, 숫자 칸 비율 {st['numeric_ratio']}")
                for r in t.rows[:max_rows]:
                    print("    | " + " | ".join(c[:40] for c in r[:6]) + (" | …" if len(r) > 6 else ""))


def raw_context(data: bytes, needle: str, around: int = 3) -> list[tuple[int, str]]:
    """원문 XML에서 needle이 처음 나오는 줄의 앞뒤 around줄을 (줄 번호, 글자)로. 파서 규칙을 정할 때 실물을 보는 용도."""
    text = normalize_encoding(data)[0].decode("utf-8", errors="replace")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if needle in line:
            lo, hi = max(0, i - around), min(len(lines), i + around + 1)
            return [(n + 1, lines[n]) for n in range(lo, hi)]
    return []


def print_raw(rcept_no: str, file_name: str, needle: str, around: int = 3) -> None:
    with zipfile.ZipFile(document_path(rcept_no)) as zf:
        found = raw_context(zf.read(file_name), needle, around)
    if not found:
        print(f"'{needle}'이(가) {file_name}에 없습니다")
    for lineno, line in found:
        print(f"{lineno:>7}: {line[:300]}")


def status(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """회사별: 공시 목록의 정기보고서 접수번호 수, 원문 보관 수."""
    return con.execute(
        """
        WITH periodic AS (
            SELECT d.corp_code, d.rcept_no FROM disclosure d
            JOIN target_corp t USING (corp_code)
            WHERE regexp_matches(d.report_nm, '^(\\[[^\\]]*\\]\\s*)*(사업|반기|분기)보고서\\s*\\(\\d{4}\\.\\d{2}\\)')
        )
        SELECT t.corp_name, t.corp_code, count(p.rcept_no) AS listed, count(doc.rcept_no) AS stored,
               sum(doc.file_size) AS bytes
        FROM target_corp t
        LEFT JOIN periodic p ON p.corp_code = t.corp_code
        LEFT JOIN document doc ON doc.rcept_no = p.rcept_no AND doc.status = '000'
        GROUP BY ALL ORDER BY listed DESC, t.corp_name
        """
    ).fetchall()


def main(argv: list[str]) -> None:
    usage = ("사용법: python -m src.loaders.documents inspect <rcept_no> [entry_name] | status | tree <rcept_no> [file_name]"
             " | parse [rcept_no ...] | why-lxml [rcept_no ...] | section <rcept_no> <file_name> <제목 일부>"
             " | raw <rcept_no> <file_name> <찾을 글자> [앞뒤 줄 수]")
    if not argv:
        sys.exit(usage)
    if argv[0] == "inspect" and len(argv) in (2, 3):
        rcept_no = argv[1]
        path = document_path(rcept_no)
        if not path.exists():
            sys.exit(f"저장된 원문이 없습니다: {path} (먼저 python -m src.loaders.collect document {rcept_no})")
        zip_bytes = path.read_bytes()
        print(f"파일: {path} ({len(zip_bytes):,} bytes)")
        for e in inspect_zip(zip_bytes):
            print(f"  {e.name}  {e.size:,} bytes  encoding={e.encoding}")
        entry, head = read_xml_head(rcept_no, argv[2] if len(argv) == 3 else None)
        print(f"\n--- {entry.name} 앞부분 ---")
        print(head)
    elif argv[0] == "status" and len(argv) == 1:
        with connect(read_only=True) as con:
            rows = status(con)
        print(f"{'회사':<14}{'corp_code':<10}{'정기보고서':>8}{'원문':>6}{'MB':>8}")
        total_listed = total_stored = total_bytes = 0
        for name, code, listed, stored, nbytes in rows:
            print(f"{name:<14}{code:<10}{listed:>8}{stored:>6}{(nbytes or 0) / 1e6:>8.1f}")
            total_listed += listed; total_stored += stored; total_bytes += nbytes or 0
        print(f"{'합계':<24}{total_listed:>8}{total_stored:>6}{total_bytes / 1e6:>8.1f}")
    elif argv[0] == "tree" and len(argv) in (2, 3):
        print_tree(argv[1], argv[2] if len(argv) == 3 else None)
    elif argv[0] == "parse":
        with connect() as con:
            stats = rebuild_sections(con, argv[1:] or None)
        print(stats)
        if stats["files_failed"]:
            print(f"실패한 파일이 {stats['files_failed']}개 있습니다. SELECT * FROM document_parse WHERE NOT ok 로 확인하세요.")
    elif argv[0] == "why-lxml":
        with connect(read_only=True) as con:
            by_kind = why_lxml(con, argv[1:] or None)
        total = sum(len(v) for v in by_kind.values())
        print(f"lxml로 파싱된 파일 {total}개의 2단계 실패 원인:")
        for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
            print(f"\n[{len(items)}개] {kind}")
            for rcept_no, file_name, lineno, snippet in items[:3]:
                print(f"  {rcept_no} {file_name} {lineno}번째 줄: {snippet}")
    elif argv[0] == "section" and len(argv) == 4:
        print_section(argv[1], argv[2], argv[3])
    elif argv[0] == "raw" and len(argv) in (4, 5):
        print_raw(argv[1], argv[2], argv[3], int(argv[4]) if len(argv) == 5 else 3)
    else:
        sys.exit(usage)


if __name__ == "__main__":
    main(sys.argv[1:])
