"""이중 시간축의 "닫기" 단계: valid_to와 superseded_by를 채운다 (docs/decisions/0005-temporal-model.md).

parse 단계는 보고서 하나만 보고 행을 만들므로 "언제까지 참이었나"를 모른다. 이 모듈은 테이블 전체를 보고
같은 회사의 다음 보고서와 비교해 닫는다. 모두 SQL이라 rebuild 때마다 몇 초 안에 다시 계산된다.

규칙 (스냅샷 테이블: investment, major_shareholder, executive)
1) superseded_by : 같은 보고서(회사·사업연도·보고서종류)의 여러 버전(원본, 정정본) 중 접수번호가 가장 큰 것이
                   최신이다. 나머지 버전의 행에 최신 접수번호를 적는다. 정정된 행은 남지만 조회에서 빠진다
2) valid_to      : 한 보고서의 행은 그 보고서 기준일(valid_from = stlm_dt)부터 같은 회사의 **다음 보고서 기준일**까지
                   참이다. 다음 보고서가 없으면 NULL(아직 유효). 보고서마다 값(지분율 등)이 바뀌므로 보고서 단위 행을
                   이어 붙이는 방식(snapshot chaining)이다. 그래서 "날짜 d 기준" 조회는 키마다 행 하나만 돌려준다
3) 임원 보정      : 다음 보고서에 그 사람이 없고 임기만료일(tenure_end_on)이 다음 기준일보다 이르면 임기만료일로 닫는다

규칙 (변동 테이블: major_shareholder_change)
- valid_to = 같은 회사의 다음 변동일(change_on). 변동일이 있어 일 단위다

"언제부터 그 관계였나"(최초 취득일, 임원 선임 시점)는 valid_from이 아니라 별도 컬럼(frst_acqs_de, tenure_start_on)이다.
valid_from을 그 날짜로 두면 연속된 보고서의 행이 겹쳐서 시점 조회가 행을 여러 개 돌려준다 (B 단계의 설계를 E에서 고침).
"""

import duckdb


def mark_superseded(con: duckdb.DuckDBPyConnection, table: str, rcept_col: str, corp_col: str) -> int:
    """같은 보고서의 옛 버전 행에 최신 버전 접수번호를 적는다. 적은 행 수를 돌려준다."""
    con.execute(f"UPDATE {table} SET superseded_by = NULL")
    con.execute(
        f"""
        UPDATE {table} SET superseded_by = v.latest
        FROM (SELECT {corp_col} AS corp, bsns_year, reprt_code, max({rcept_col}) AS latest
              FROM {table} GROUP BY 1, 2, 3) v
        WHERE {table}.{corp_col} = v.corp AND {table}.bsns_year = v.bsns_year AND {table}.reprt_code = v.reprt_code
          AND {table}.{rcept_col} < v.latest
        """
    )
    return con.execute(f"SELECT count(*) FROM {table} WHERE superseded_by IS NOT NULL").fetchone()[0]


def chain_snapshots(con: duckdb.DuckDBPyConnection, table: str, corp_col: str) -> None:
    """valid_to = 같은 회사의 다음 보고서 기준일. 정정된(superseded) 행은 valid_to를 비워 둔다."""
    con.execute(f"UPDATE {table} SET valid_to = NULL")
    con.execute(
        f"""
        WITH reports AS (SELECT DISTINCT {corp_col} AS corp, stlm_dt FROM {table} WHERE superseded_by IS NULL AND stlm_dt IS NOT NULL),
             nxt AS (SELECT corp, stlm_dt, lead(stlm_dt) OVER (PARTITION BY corp ORDER BY stlm_dt) AS next_dt FROM reports)
        UPDATE {table} SET valid_to = nxt.next_dt
        FROM nxt
        WHERE {table}.{corp_col} = nxt.corp AND {table}.stlm_dt = nxt.stlm_dt AND {table}.superseded_by IS NULL
        """
    )


def close_executive_tenures(con: duckdb.DuckDBPyConnection) -> int:
    """다음 보고서에 없는 임원은 임기만료일이 더 이르면 그 날짜로 닫는다. 보정한 행 수를 돌려준다."""
    return con.execute(
        """
        UPDATE executive SET valid_to = tenure_end_on
        WHERE superseded_by IS NULL AND valid_to IS NOT NULL AND tenure_end_on IS NOT NULL
          AND tenure_end_on > valid_from AND tenure_end_on < valid_to
          AND NOT EXISTS (
              SELECT 1 FROM executive n
              WHERE n.corp_code = executive.corp_code AND n.stlm_dt = executive.valid_to
                AND n.superseded_by IS NULL AND n.person_id = executive.person_id
          )
        RETURNING 1
        """
    ).fetchall().__len__()


def chain_changes(con: duckdb.DuckDBPyConnection) -> None:
    """major_shareholder_change.valid_to = 같은 회사의 다음 변동일."""
    con.execute("UPDATE major_shareholder_change SET valid_to = NULL")
    con.execute(
        """
        WITH nxt AS (SELECT corp_code, change_on, mxmm_shrholdr_nm,
                            lead(change_on) OVER (PARTITION BY corp_code ORDER BY change_on, mxmm_shrholdr_nm) AS next_on
                     FROM major_shareholder_change)
        UPDATE major_shareholder_change SET valid_to = nxt.next_on
        FROM nxt
        WHERE major_shareholder_change.corp_code = nxt.corp_code AND major_shareholder_change.change_on = nxt.change_on
          AND major_shareholder_change.mxmm_shrholdr_nm = nxt.mxmm_shrholdr_nm
        """
    )


def rebuild_person(con: duckdb.DuckDBPyConnection) -> int:
    """executive의 (이름, 생년월, 성별)마다 person 행 하나. 결정 0006. 행 수를 돌려준다."""
    con.execute("DELETE FROM person")
    con.execute(
        """
        INSERT INTO person (person_id, nm, birth_ym, sexdstn, first_seen, last_seen, corp_count, loaded_at)
        SELECT person_id, any_value(nm), any_value(birth_ym), any_value(sexdstn),
               min(coalesce(tenure_start_on, valid_from)), max(coalesce(valid_to, stlm_dt)),
               count(DISTINCT corp_code), now()
        FROM executive WHERE superseded_by IS NULL
        GROUP BY person_id
        """
    )
    return con.execute("SELECT count(*) FROM person").fetchone()[0]


def close_investments(con: duckdb.DuckDBPyConnection) -> dict:
    superseded = mark_superseded(con, "investment", "rcept_no", "investor_corp_code")
    chain_snapshots(con, "investment", "investor_corp_code")
    return {"investment_superseded": superseded}


def close_relations(con: duckdb.DuckDBPyConnection) -> dict:
    stats = {}
    for table in ("major_shareholder", "executive"):
        stats[f"{table}_superseded"] = mark_superseded(con, table, "source_rcept_no", "corp_code")
        chain_snapshots(con, table, "corp_code")
    stats["executive_closed_by_tenure_end"] = close_executive_tenures(con)
    chain_changes(con)
    stats["person"] = rebuild_person(con)
    return stats
