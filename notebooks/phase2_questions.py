"""Phase 2 F 단계: 시점 질의 검증. 관계 테이블(최대주주·변동·임원·출자)이 질문에 맞는 답을 주는지 눈으로 확인한다.

백필(src/pipeline/backfill_reports.py)이 끝난 뒤, 저장소 루트에서 실행한다 (DuckDB를 읽기 전용으로만 연다. API 호출 없음):
    python notebooks/phase2_questions.py                                   # 화면에 출력
    python notebooks/phase2_questions.py --out notebooks/phase2_questions_result.txt   # 파일에도 기록

출력은 두 부분이다.
    1) 데이터 상태: 테이블 행 수, 회사별 보고서 수, 정정본 수, 생년월 없는 사람 비율(0006), 기준일 분포 등.
       0005·0006 문서의 "다시 판단할 조건"에 해당하는 숫자를 여기서 본다
    2) 질문 5개: 각 질문에 대한 SQL과 결과 표, 그리고 근거 공시를 DART 뷰어에서 확인하는 링크.
       결과가 맞는지는 사람이 DART 원문과 비교해 확인한다 (docs/devlog에 기록)

시점 조회의 기본 꼴 (0005): 날짜 d에 참인 행 = valid_from <= d AND (valid_to IS NULL OR valid_to > d) AND superseded_by IS NULL
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.collectors.corp_code import HOLDING_CORPS  # noqa: E402
from src.loaders.duckdb_store import connect  # noqa: E402

DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

# 질문에 쓰는 회사 이름. 코드는 적지 않고 target_corp에서 찾는다 (CLAUDE.md: 대상 목록은 코드에 적지 않는다)
Q1_BANK, Q1_AS_OF = "신한은행", date(2023, 3, 31)
Q2_BANK, Q2_AS_OF = "하나은행", date(2022, 12, 31)

AS_OF_FILTER = "valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) AND superseded_by IS NULL"


class Report:
    """출력을 화면과 파일에 같이 쓴다."""

    def __init__(self, out_path: str | None):
        self.file = open(out_path, "w", encoding="utf-8") if out_path else None

    def __call__(self, text: str = "") -> None:
        print(text)
        if self.file:
            self.file.write(text + "\n")

    def close(self) -> None:
        if self.file:
            self.file.close()


def table(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None, max_rows: int = 40) -> str:
    """SQL 결과를 표 문자열로. 행이 많으면 앞부분만 보여 주고 전체 행 수를 적는다."""
    df = con.execute(sql, params or []).df()
    if df.empty:
        return "(결과 없음)"
    with pd.option_context("display.max_columns", None, "display.width", 200, "display.max_colwidth", 40):
        text = df.head(max_rows).to_string(index=False)
    if len(df) > max_rows:
        text += f"\n... 전체 {len(df):,}행 중 {max_rows}행"
    return text


def find_target(con: duckdb.DuckDBPyConnection, name: str) -> tuple[str, str] | None:
    """target_corp에서 이름으로 회사를 찾는다. 정확히 같은 이름을 먼저, 없으면 포함하는 이름 하나."""
    rows = con.execute(
        "SELECT corp_code, corp_name FROM target_corp WHERE corp_name = ? "
        "UNION ALL SELECT corp_code, corp_name FROM target_corp WHERE corp_name LIKE ? AND corp_name <> ? ORDER BY 2 LIMIT 1",
        [name, f"%{name}%", name],
    ).fetchall()
    return rows[0] if rows else None


def links(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> str:
    """SQL이 돌려주는 접수번호마다 DART 뷰어 링크 한 줄."""
    rows = con.execute(sql, params or []).fetchall()
    return "\n".join(f"  근거 {r[0]}: {DART_VIEWER}{r[0]}" for r in rows) or "  (근거 없음)"


# ---------------------------------------------------------------- 1) 데이터 상태

def data_health(con: duckdb.DuckDBPyConnection, out: Report) -> None:
    out("=" * 100)
    out("1) 데이터 상태")
    out("=" * 100)

    out("\n[1-1] 테이블 행 수 (전체 / 정정되지 않은 행 / 현재 상태 뷰)")
    out(table(con, """
        SELECT 'investment' AS tbl, count(*) AS rows_all, count(*) FILTER (superseded_by IS NULL) AS rows_live,
               (SELECT count(*) FROM investment_current) AS rows_current FROM investment
        UNION ALL SELECT 'major_shareholder', count(*), count(*) FILTER (superseded_by IS NULL), (SELECT count(*) FROM major_shareholder_current) FROM major_shareholder
        UNION ALL SELECT 'major_shareholder_change', count(*), count(*) FILTER (superseded_by IS NULL), (SELECT count(*) FROM major_shareholder_change_current) FROM major_shareholder_change
        UNION ALL SELECT 'executive', count(*), count(*) FILTER (superseded_by IS NULL), (SELECT count(*) FROM executive_current) FROM executive
        UNION ALL SELECT 'person', count(*), count(*), count(*) FROM person
    """))

    out("\n[1-2] 회사별 보고서 수 (임원 현황 기준, 정정본 제외). 대상 회사인데 0이면 백필이 빠진 것")
    out(table(con, """
        SELECT t.corp_code, t.corp_name, t.role,
               count(DISTINCT e.bsns_year || '-' || e.reprt_code) AS reports,
               min(e.stlm_dt) AS first_stlm_dt, max(e.stlm_dt) AS last_stlm_dt
        FROM target_corp t LEFT JOIN executive e ON e.corp_code = t.corp_code AND e.superseded_by IS NULL
        GROUP BY 1, 2, 3 ORDER BY t.role DESC, t.corp_name
    """, max_rows=80))

    out("\n[1-3] 정정본이 있는 보고서 (같은 회사·사업연도·보고서종류에 접수번호가 둘 이상)")
    out(table(con, """
        SELECT corp_code, bsns_year, reprt_code, count(DISTINCT source_rcept_no) AS versions,
               min(source_rcept_no) AS first_rcept_no, max(source_rcept_no) AS latest_rcept_no
        FROM executive GROUP BY 1, 2, 3 HAVING count(DISTINCT source_rcept_no) > 1 ORDER BY 1, 2, 3
    """))

    out("\n[1-4] 사람 식별(0006): 생년월 없는 사람 비율. 10% 이상이면 0006의 '다시 판단할 조건'")
    out(table(con, """
        SELECT count(*) AS persons, count(*) FILTER (birth_ym IS NULL) AS no_birth_ym,
               round(100.0 * count(*) FILTER (birth_ym IS NULL) / nullif(count(*), 0), 1) AS no_birth_ym_pct,
               count(*) FILTER (corp_count >= 2) AS multi_corp
        FROM person
    """))

    out("\n[1-5] 같은 이름인데 person_id가 다른 경우 (동명이인으로 갈라진 사례. 같은 사람이 생년월 표기 차이로 갈라졌는지 확인)")
    out(table(con, """
        SELECT nm, count(*) AS ids, string_agg(person_id, ' / ' ORDER BY person_id) AS person_ids
        FROM person GROUP BY nm HAVING count(*) > 1 ORDER BY ids DESC, nm
    """))

    out("\n[1-6] 보고서 기준일 분포. 12월 결산 가정이라 03-31/06-30/09-30/12-31 외의 날짜가 있으면 periodic_report_key 재검토")
    out(table(con, """
        SELECT strftime(stlm_dt, '%m-%d') AS month_day, reprt_code, count(DISTINCT corp_code || source_rcept_no) AS reports
        FROM executive WHERE superseded_by IS NULL GROUP BY 1, 2 ORDER BY 2, 1
    """))

    out("\n[1-7] 최대주주 이름 → corp_code 연결 결과 (not_found·ambiguous가 많으면 config/corp_name_overrides.csv 보강)")
    out(table(con, """
        SELECT resolve_method, count(*) AS rows_live, count(DISTINCT nm) AS names,
               string_agg(DISTINCT nm, ', ' ORDER BY nm) FILTER (resolve_method IN ('not_found', 'ambiguous')) AS unresolved_names
        FROM major_shareholder WHERE superseded_by IS NULL GROUP BY 1 ORDER BY 2 DESC
    """))

    out("\n[1-8] 임원 선임 시점(tenure_start_on)을 어떻게 구했나. 못 구한 행의 원문 표기는 최대 40개만")
    out(table(con, """
        SELECT coalesce(tenure_start_method, '(없음)') AS method, count(*) AS rows_live,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
        FROM executive WHERE superseded_by IS NULL GROUP BY 1 ORDER BY 2 DESC
    """))
    out(table(con, """
        SELECT replace(hffc_pd_raw, chr(10), ' ') AS raw, count(*) AS n FROM executive
        WHERE superseded_by IS NULL AND tenure_start_on IS NULL GROUP BY 1 ORDER BY 2 DESC
    """))


# ---------------------------------------------------------------- 2) 질문 5개

def q1_major_shareholder_as_of(con: duckdb.DuckDBPyConnection, out: Report) -> None:
    out(f"\n[Q1] {Q1_AS_OF} 기준 {Q1_BANK}의 최대주주와 지분율은?")
    target = find_target(con, Q1_BANK)
    if not target:
        out(f"  target_corp에 '{Q1_BANK}'이 없어 건너뜀")
        return
    code, name = target
    out(f"  대상: {name} ({code}). 기대: 지주 1곳이 100%에 가깝게 나오고, 행이 보고서 하나에서만 나와야 함")
    sql = f"""
        SELECT nm, relate, trmend_posesn_stock_qota_rt AS pct, holder_corp_code, valid_from, valid_to, source_rcept_no
        FROM major_shareholder WHERE corp_code = ? AND {AS_OF_FILTER} ORDER BY pct DESC NULLS LAST
    """
    out(table(con, sql, [code, Q1_AS_OF, Q1_AS_OF]))
    out(links(con, f"SELECT DISTINCT source_rcept_no FROM major_shareholder WHERE corp_code = ? AND {AS_OF_FILTER}",
              [code, Q1_AS_OF, Q1_AS_OF]))
    out("  확인: 링크의 보고서 'VII. 주주에 관한 사항 > 최대주주 및 특수관계인의 주식소유 현황'과 지분율 비교")


def q2_outside_directors_as_of(con: duckdb.DuckDBPyConnection, out: Report) -> None:
    out(f"\n[Q2] {Q2_AS_OF} 기준 {Q2_BANK}의 사외이사는 누구인가?")
    target = find_target(con, Q2_BANK)
    if not target:
        out(f"  target_corp에 '{Q2_BANK}'이 없어 건너뜀")
        return
    code, name = target
    out(f"  대상: {name} ({code}). 기대: {Q2_AS_OF.year} 사업보고서의 사외이사 명단과 같고, 한 사람이 한 번씩만 나와야 함")
    sql = f"""
        SELECT nm, birth_ym, ofcps, tenure_start_on, tenure_end_on, valid_from, valid_to, source_rcept_no
        FROM executive WHERE corp_code = ? AND rgist_exctv_at = '사외이사' AND {AS_OF_FILTER} ORDER BY nm
    """
    out(table(con, sql, [code, Q2_AS_OF, Q2_AS_OF]))
    dup = con.execute(f"SELECT person_id, count(*) FROM executive WHERE corp_code = ? AND {AS_OF_FILTER} GROUP BY 1 HAVING count(*) > 1",
                      [code, Q2_AS_OF, Q2_AS_OF]).fetchall()
    out(f"  시점 조회에서 두 번 나온 사람: {dup if dup else '없음 (정상)'}")
    out(links(con, f"SELECT DISTINCT source_rcept_no FROM executive WHERE corp_code = ? AND {AS_OF_FILTER}", [code, Q2_AS_OF, Q2_AS_OF]))
    out("  확인: 링크의 보고서 'VIII. 임원 및 직원 등에 관한 사항 > 임원 현황'의 사외이사와 비교. 선임 시점(tenure_start_on)은 월 단위 근사")


def q3_concurrent_positions(con: duckdb.DuckDBPyConnection, out: Report) -> None:
    out("\n[Q3] 지금 지주 임원이면서 그 지주의 자회사 임원을 겸하는 사람은? (0006의 person_id로 묶음)")
    out("  기대: 지주의 기타비상무이사·부문장이 은행·증권 등의 이사로 나오는 패턴. 같은 person_id가 다른 사람이면 0006에 분리 규칙 추가")
    sql = """
        SELECT h.person_id, h.corp_code AS holding_code, h.ofcps AS holding_ofcps,
               s.corp_code AS sub_code, t.corp_name AS sub_name, s.ofcps AS sub_ofcps, s.rgist_exctv_at AS sub_rgist,
               h.source_rcept_no AS holding_rcept_no, s.source_rcept_no AS sub_rcept_no
        FROM executive_current h
        JOIN executive_current s ON s.person_id = h.person_id AND s.corp_code <> h.corp_code
        JOIN target_corp t ON t.corp_code = s.corp_code AND t.role = 'subsidiary' AND t.parent_corp_code = h.corp_code
        WHERE h.corp_code IN (SELECT corp_code FROM target_corp WHERE role = 'holding')
        ORDER BY h.corp_code, h.person_id, s.corp_code
    """
    out(table(con, sql, max_rows=80))
    out("\n  (참고) 두 회사 이상에서 임원으로 나온 사람 전체 (시기가 달라도 셈):")
    out(table(con, "SELECT person_id, corp_count, first_seen, last_seen FROM person WHERE corp_count >= 2 ORDER BY corp_count DESC, person_id", max_rows=30))
    out("  확인: 위 표에서 몇 명을 골라 지주·자회사 보고서의 임원 현황에 같은 생년월로 나오는지 비교")


def q4_major_shareholder_history(con: duckdb.DuckDBPyConnection, out: Report) -> None:
    out("\n[Q4] 4대 지주의 최대주주 변동 이력은? (변동현황 API, 변동일 단위)")
    out("  기대: 국민연금공단 등 변동일마다 한 행, valid_to가 다음 변동일로 이어짐. 여러 보고서에 겹쳐 실린 같은 변동은 한 번만")
    for code, name in HOLDING_CORPS.items():
        out(f"\n  - {name} ({code})")
        out(table(con, """
            SELECT change_on, mxmm_shrholdr_nm, qota_rt, change_cause, valid_to, source_rcept_no
            FROM major_shareholder_change WHERE corp_code = ? ORDER BY change_on
        """, [code], max_rows=20))
    out("\n  (대조) 같은 지주의 보고서별 최대주주 지분율 스냅샷 (최대주주 현황 API). 변동 이력과 방향이 맞아야 함")
    # relate 표기는 회사마다 다르다('본인', '대주주', '최대주주'...) → 보고서마다 지분율이 가장 큰 행을 최대주주로 본다
    out(table(con, """
        SELECT corp_code, stlm_dt, nm, relate, pct, valid_to, source_rcept_no FROM (
            SELECT corp_code, stlm_dt, nm, relate, trmend_posesn_stock_qota_rt AS pct, valid_to, source_rcept_no,
                   row_number() OVER (PARTITION BY source_rcept_no ORDER BY trmend_posesn_stock_qota_rt DESC NULLS LAST) AS rn
            FROM major_shareholder WHERE corp_code IN (SELECT corp_code FROM target_corp WHERE role = 'holding') AND superseded_by IS NULL
        ) WHERE rn = 1 ORDER BY corp_code, stlm_dt
    """, max_rows=80))


def q5_corrections(con: duckdb.DuckDBPyConnection, out: Report) -> None:
    out("\n[Q5] 정정 공시가 있으면 원본과 정정본에서 무엇이 달라졌나? (기록 시간: superseded_by)")
    out("  기대: 원본 행은 지워지지 않고 superseded_by에 정정본 접수번호가 적혀 있음. 아래는 최대주주 지분율과 임원 명단의 차이")
    out("\n  - 최대주주 지분율이 바뀐 행 (원본 vs 정정본)")
    out(table(con, """
        SELECT o.corp_code, o.bsns_year, o.reprt_code, o.nm, o.trmend_posesn_stock_qota_rt AS pct_before,
               n.trmend_posesn_stock_qota_rt AS pct_after, o.source_rcept_no AS before_rcept_no, n.source_rcept_no AS after_rcept_no
        FROM major_shareholder o JOIN major_shareholder n
          ON n.source_rcept_no = o.superseded_by AND n.corp_code = o.corp_code AND n.nm = o.nm AND n.superseded_by IS NULL
        WHERE o.superseded_by IS NOT NULL AND o.trmend_posesn_stock_qota_rt IS DISTINCT FROM n.trmend_posesn_stock_qota_rt
        ORDER BY 1, 2, 3, 4
    """))
    out("\n  - 정정본에서 빠지거나 새로 들어온 임원")
    out(table(con, """
        WITH pairs AS (
            SELECT DISTINCT corp_code, bsns_year, reprt_code, source_rcept_no AS before_rcept_no, superseded_by AS after_rcept_no
            FROM executive WHERE superseded_by IS NOT NULL
        )
        SELECT p.corp_code, p.bsns_year, p.reprt_code, p.before_rcept_no, p.after_rcept_no,
               (SELECT string_agg(DISTINCT person_id, ', ') FROM executive b WHERE b.source_rcept_no = p.before_rcept_no
                  AND b.person_id NOT IN (SELECT person_id FROM executive a WHERE a.source_rcept_no = p.after_rcept_no)) AS removed,
               (SELECT string_agg(DISTINCT person_id, ', ') FROM executive a WHERE a.source_rcept_no = p.after_rcept_no
                  AND a.person_id NOT IN (SELECT person_id FROM executive b WHERE b.source_rcept_no = p.before_rcept_no)) AS added
        FROM pairs p ORDER BY 1, 2, 3, 4
    """))
    out("\n  - 공시 목록의 [기재정정] 보고서 수 (원본 응답에 두 버전이 모두 있는지와 비교)")
    out(table(con, """
        SELECT corp_code, count(*) FILTER (report_nm LIKE '[기재정정]%') AS corrected_in_listing, count(*) AS periodic_total
        FROM disclosure WHERE corp_code IN (SELECT corp_code FROM target_corp)
          AND (report_nm LIKE '%사업보고서%' OR report_nm LIKE '%반기보고서%' OR report_nm LIKE '%분기보고서%')
        GROUP BY 1 HAVING count(*) FILTER (report_nm LIKE '[기재정정]%') > 0 ORDER BY 2 DESC
    """))
    out(links(con, "SELECT DISTINCT superseded_by FROM executive WHERE superseded_by IS NOT NULL ORDER BY 1 LIMIT 5"))
    out("  확인: 링크(정정본)의 '정정신고 (보고)' 표에서 정정 사유와 위 차이를 비교")


QUESTIONS = (q1_major_shareholder_as_of, q2_outside_directors_as_of, q3_concurrent_positions,
             q4_major_shareholder_history, q5_corrections)


def run(con: duckdb.DuckDBPyConnection, out: Report) -> None:
    data_health(con, out)
    out("\n" + "=" * 100)
    out("2) 시점 질의 5개")
    out("=" * 100)
    for question in QUESTIONS:
        question(con, out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 시점 질의 검증 (읽기 전용, API 호출 없음)")
    parser.add_argument("--out", help="결과를 이 파일에도 기록")
    args = parser.parse_args()
    out = Report(args.out)
    try:
        with connect(read_only=True) as con:
            run(con, out)
    finally:
        out.close()


if __name__ == "__main__":
    main()
