"""지주 사업보고서 IX절 "회사와 계열회사간 임원 겸직 현황" 표 → concurrent_position (결정 0014).

왜: 사업보고서를 내지 않는 자회사(KB자산운용 등, 감사보고서만)는 임원 표가 없어 그래프가 그 회사 쪽 겸직을 모른다. 지주의 겸직 현황 표에는
그 자리가 있다. 원문은 이미 받아 파싱해 둔 document_section.tables(0007)에 있으니 API 호출이 없다.

흐름 (rebuild_relations 안, 임원 표 다음):
  1) 지주마다 정기보고서의 IX절 표에서 겸직 표를 고른다 (머리행에 성명 + 대상 계열사/대상회사/겸직회사명). 한 절에 둘이면 첫 표만 — 둘째는
     "작성기준일 이후" 표라 기준일이 다르다
  2) 행마다 이름·겸직 회사·직위·선임·상근을 열 별명 표로 읽는다 (지주마다 열 이름이 다르다, 0014 결정 1)
  3) 사람: 같은 지주 임원 표에서 같은 기준일에 유효한 같은 이름이 정확히 1명이면 그 person_id. 0명·2명 이상이면 비워 두고 방법만 적는다 (0006)
  4) 회사: 출자 표와 같은 이름 연결 (investments.resolve + config/corp_name_overrides.csv)
  5) 시간: valid_from = 기준일, 정정본 superseded_by, 다음 스냅샷 기준일로 valid_to (temporal.mark_superseded·chain_snapshots)

    python -m src.loaders.concurrent_positions rebuild      # 노트북에서 따로 돌려 볼 때 (보통은 relations rebuild가 함께 한다)
"""

from __future__ import annotations

import calendar
import json
import re
import sys
from datetime import date

import duckdb

from src.loaders.investments import build_name_index, load_overrides, normalize_name, resolve
from src.loaders.periodic_reports import periodic_report_key
from src.loaders.temporal import chain_snapshots, mark_superseded

# 열 별명 (2026-11-27 네 지주 2025 사업보고서에서 확인). 머리 글자의 공백은 뗀 뒤 비교한다 ("성 명" → "성명", "비 고" → "비고")
COLUMNS = {
    "nm": ("성명",),
    "target": ("대상계열사", "대상회사", "겸직회사명", "겸직회사"),
    "ofcps": ("직책", "직위명", "직위"),
    "appointed": ("선임시기", "선임일", "겸임일자", "선임일자"),
    "fte": ("비고", "상근여부", "상근/비상근여부", "상근/비상근"),
}
SECTION_PREFIX = "IX."
_STLM_RE = re.compile(r"\((\d{4})\.(\d{2})\)\s*$")
_DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*(?:[.\-/월]\s*(\d{1,2})\s*)?")

# 임원인 직책의 낱말. 겸직 표에는 본부장·센터장·부장(임원이 아닌 직책)도 섞여 있다 — 국민은행 임원 표에 없는 사람 5명이 그랬다(2026-11-27 g11).
# "금융AI1센터장 상무"처럼 임원 낱말이 함께 있으면 임원. 표에는 모두 남기고(is_officer) 그래프에는 임원만 올린다 (0014 결정 4)
OFFICER_WORDS = ("이사", "회장", "사장", "전무", "상무", "행장", "감사", "대표", "임원", "CEO", "CFO", "CIO", "CRO")


def is_officer_title(ofcps: str | None) -> bool:
    text = (ofcps or "").replace(" ", "")
    return any(w in text for w in OFFICER_WORDS)


COLUMN_LIST = ["source_rcept_no", "row_seq", "holding_corp_code", "bsns_year", "reprt_code", "nm", "person_id", "person_method",
               "target_name", "target_corp_code", "resolve_method", "ofcps", "appointed_raw", "appointed_on", "appointed_method",
               "fte_at", "is_officer", "stlm_dt", "valid_from"]


def _squash(text: str | None) -> str:
    return "".join((text or "").split())


def header_map(table: dict) -> dict[str, int] | None:
    """표 머리행 → {뜻: 열 번호}. 이름 열과 겸직 회사 열이 둘 다 있어야 겸직 표다. 머리행이 여럿이면 마지막 머리행."""
    rows = table.get("rows") or []
    n_head = int(table.get("header_rows") or 0)
    if n_head == 0 or len(rows) <= n_head:
        return None
    head = [_squash(c) for c in rows[n_head - 1]]
    out: dict[str, int] = {}
    for meaning, names in COLUMNS.items():
        for i, cell in enumerate(head):
            if any(cell == n or (meaning != "nm" and cell.startswith(n)) for n in names) and meaning not in out:
                out[meaning] = i
    return out if "nm" in out and "target" in out else None


def concurrent_table(tables: list[dict]) -> tuple[dict, dict[str, int]] | None:
    """절의 표들 중 첫 겸직 표와 그 열 지도. 없으면 None."""
    for t in tables:
        m = header_map(t)
        if m:
            return t, m
    return None


def parse_appointed(text: str | None) -> tuple[date | None, str | None]:
    """'2023.02.15' → 날짜(date), '2025. 1월' → 그달 1일(month). 못 읽으면 (None, None)."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None, None
    y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
    if not 1 <= mo <= 12:
        return None, None
    try:
        return (date(y, mo, int(d)), "date") if d else (date(y, mo, 1), "month")
    except ValueError:
        return None, None


def parse_rows(table: dict, cols: dict[str, int]) -> list[dict]:
    """데이터 행 → 이름·겸직 회사·직위·선임·상근. 이름이나 회사가 비어 있거나 '-'면 건너뛴다 (합계·빈 줄)."""
    rows = table.get("rows") or []
    out = []
    for r in rows[int(table.get("header_rows") or 0):]:
        def cell(key: str) -> str | None:
            i = cols.get(key)
            return " ".join(r[i].split()) if i is not None and i < len(r) and r[i] else None
        nm, target = cell("nm"), cell("target")
        if not nm or not target or nm == "-" or target == "-":
            continue
        appointed_on, method = parse_appointed(cell("appointed"))
        out.append({"nm": nm, "target_name": target, "ofcps": cell("ofcps"), "appointed_raw": cell("appointed"),
                    "appointed_on": appointed_on, "appointed_method": method, "fte_at": cell("fte"), "is_officer": is_officer_title(cell("ofcps"))})
    return out


def stlm_dt_of(report_nm: str) -> date | None:
    """'사업보고서 (2025.12)' → 2025-12-31 (결산월 말일). 표 머리의 기준일과 같다."""
    m = _STLM_RE.search(report_nm or "")
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return date(y, mo, calendar.monthrange(y, mo)[1])


def holding_reports(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """지주의 정기보고서 중 IX절이 파싱돼 있는 것. (holding_corp_code, rcept_no, report_nm, file_name, tables)"""
    rows = con.execute(
        f"""
        SELECT t.corp_code, d.rcept_no, d.report_nm, s.file_name, s.tables
        FROM target_corp t
        JOIN disclosure d ON d.corp_code = t.corp_code AND d.report_nm LIKE '%보고서 (%'
        JOIN document_section s ON s.rcept_no = d.rcept_no AND s.path LIKE '{SECTION_PREFIX}%' AND s.table_count > 0
        WHERE t.role = 'holding'
        ORDER BY t.corp_code, d.rcept_no, s.file_name, s.section_id
        """
    ).fetchall()
    return [{"holding_corp_code": r[0], "rcept_no": r[1], "report_nm": r[2], "file_name": r[3],
             "tables": json.loads(r[4]) if isinstance(r[4], str) else r[4]} for r in rows]


def link_person(con: duckdb.DuckDBPyConnection, holding: str, nm: str, stlm_dt: date) -> tuple[str | None, str]:
    """같은 지주 임원 표에서 그 기준일에 유효한 같은 이름. 1명 → person_id, 0명 → not_found, 여럿 → ambiguous (추측하지 않음, 0006)."""
    ids = [r[0] for r in con.execute(
        """SELECT DISTINCT person_id FROM executive
           WHERE corp_code = ? AND nm = ? AND person_id IS NOT NULL AND stlm_dt = ?""", [holding, nm, stlm_dt]).fetchall()]
    if len(ids) == 1:
        return ids[0], "holding_executive"
    return None, ("not_found" if not ids else "ambiguous")


def collect_rows(con: duckdb.DuckDBPyConnection) -> list[dict]:
    index, overrides = build_name_index(con), load_overrides()
    out: list[dict] = []
    seen_reports: set[str] = set()
    for rep in holding_reports(con):
        if rep["rcept_no"] in seen_reports:   # 한 보고서에 IX절 파일이 여럿(첨부)이어도 첫 겸직 표만
            continue
        found = concurrent_table(rep["tables"])
        if not found:
            continue
        key = periodic_report_key(rep["report_nm"])
        stlm = stlm_dt_of(rep["report_nm"])
        if not key or not stlm:
            continue
        seen_reports.add(rep["rcept_no"])
        table, cols = found
        for seq, row in enumerate(parse_rows(table, cols)):
            person_id, pm = link_person(con, rep["holding_corp_code"], row["nm"], stlm)
            code, rm = resolve(normalize_name(row["target_name"])[0] or row["target_name"], index, overrides)
            out.append({"source_rcept_no": rep["rcept_no"], "row_seq": seq, "holding_corp_code": rep["holding_corp_code"],
                        "bsns_year": key[0], "reprt_code": key[1], "person_id": person_id, "person_method": pm,
                        "target_corp_code": code, "resolve_method": rm, "stlm_dt": stlm, "valid_from": stlm, **row})
    return out


def rebuild_concurrent_positions(con: duckdb.DuckDBPyConnection) -> dict:
    """concurrent_position을 document_section에서 다시 만든다 (API 0건). 호출하는 쪽이 트랜잭션을 연다.
    executive가 먼저 채워져 있어야 사람을 이을 수 있다 (rebuild_relations가 그 순서로 부른다)."""
    rows = collect_rows(con)
    con.execute("DELETE FROM concurrent_position")
    if rows:
        con.executemany(
            f"INSERT INTO concurrent_position ({', '.join(COLUMN_LIST)}, loaded_at) VALUES ({', '.join('?' * len(COLUMN_LIST))}, now())",
            [[r[c] for c in COLUMN_LIST] for r in rows])
    superseded = mark_superseded(con, "concurrent_position", "source_rcept_no", "holding_corp_code")
    chain_snapshots(con, "concurrent_position", "holding_corp_code")
    by_method = dict(con.execute("SELECT person_method, count(*) FROM concurrent_position GROUP BY 1").fetchall())
    return {"concurrent_position": len(rows), "concurrent_superseded": superseded, "concurrent_person_by_method": by_method,
            "concurrent_non_officer": sum(1 for r in rows if not r["is_officer"]),
            "concurrent_reports": len({r["source_rcept_no"] for r in rows})}


if __name__ == "__main__":
    if sys.argv[1:] != ["rebuild"]:
        sys.exit("사용법: python -m src.loaders.concurrent_positions rebuild")
    from src.loaders.duckdb_store import connect

    with connect() as con:
        con.begin()
        print(rebuild_concurrent_positions(con))
        con.commit()
