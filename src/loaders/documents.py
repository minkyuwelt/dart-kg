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
        LEFT JOIN document doc ON doc.rcept_no = p.rcept_no
        GROUP BY ALL ORDER BY listed DESC, t.corp_name
        """
    ).fetchall()


def main(argv: list[str]) -> None:
    usage = "사용법: python -m src.loaders.documents inspect <rcept_no> [entry_name] | status"
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
    else:
        sys.exit(usage)


if __name__ == "__main__":
    main(sys.argv[1:])
