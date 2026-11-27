"""DuckDB 연결과 공통 적재 함수.

DuckDB 파일 위치는 .env의 DUCKDB_PATH (없으면 저장소 루트의 data/dartkg.duckdb).

주의: DuckDB는 파일을 프로세스 단위로 잠근다. 동시에 가능한 조합은 둘뿐이다.
- 쓰기 연결을 연 프로세스 하나만, 또는
- 읽기 전용(read_only=True) 연결을 연 프로세스 여러 개만
읽기 전용이라도 다른 프로세스가 파일을 열고 있으면 쓰기 연결을 열 수 없고, 그 반대도 마찬가지다.
(duckdb 1.5.6에서 두 방향 모두 "Could not set lock" 에러 확인)

그래서 이 프로젝트에서는
- Dagster는 dagster_home/dagster.yaml에서 동시에 도는 run을 1개로 제한하고,
  잠금에 걸리면 몇 분 뒤 다시 시도한다 (src/pipeline/definitions.py의 write_connection)
- 노트북·탐색 코드는 `with connect(read_only=True) as con:`으로 열고 블록이 끝나면 바로 닫는다.
  연결을 열어 둔 채 두면 다음 수집이 잠금에 걸린다
"""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "dartkg.duckdb"


def db_path() -> Path:
    load_dotenv()
    return Path(os.getenv("DUCKDB_PATH") or DEFAULT_DB_PATH)


def is_lock_error(error: BaseException) -> bool:
    """DuckDB 파일 잠금 때문에 연결에 실패한 에러인지 확인한다."""
    return isinstance(error, duckdb.IOException) and "lock" in str(error).lower()


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """DuckDB 파일에 연결한다. 쓰기 연결이면 schema.sql을 먼저 실행해 테이블을 만든다.

    with 문으로 쓰면 블록이 끝날 때 연결이 닫힌다.
        with connect() as con:
            ...
    """
    path = db_path()
    if read_only and not path.exists():
        raise FileNotFoundError(f"DuckDB 파일이 없습니다: {path} (먼저 파이프라인을 한 번 실행하세요)")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


@contextmanager
def transaction(con: duckdb.DuckDBPyConnection) -> Iterator[duckdb.DuckDBPyConnection]:
    """블록이 끝나면 commit, 예외가 나면 rollback 후 다시 던진다. rebuild처럼 "비우고 다시 채우기"를 한 덩어리로 묶을 때 쓴다.

        with transaction(con):
            con.execute("DELETE FROM ...")
            ...
    """
    con.begin()
    try:
        yield con
    except BaseException:
        con.rollback()
        raise
    con.commit()


def replace_rows(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict], columns: list[str],
                 expressions: dict[str, str] | None = None) -> int:
    """테이블을 비우고 rows로 다시 채운다 (loaded_at = now()). 넣은 행 수를 돌려준다. 트랜잭션은 호출하는 쪽이 연다.

    rows의 값은 columns 순서로 DataFrame에 담아 통째로 넘긴다 (한 행씩 INSERT하지 않음). expressions에 적은 열은 그 SQL 식으로
    바꿔 넣는다 — 문자열 날짜를 TRY_STRPTIME으로 바꾸는 식. 식 안에서는 columns의 이름을 그대로 쓸 수 있다.
    """
    con.execute(f"DELETE FROM {table}")
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=columns)
    exprs = expressions or {}
    select = ", ".join(exprs.get(c, c) for c in columns)
    view = f"incoming_{table}"
    con.register(view, df)
    try:
        con.execute(f"INSERT INTO {table} ({', '.join(columns)}, loaded_at) SELECT {select}, now() FROM {view}")
    finally:
        con.unregister(view)
    return len(df)


def save_raw(con: duckdb.DuckDBPyConnection, endpoint: str, request_key: str, body: dict) -> None:
    """API 원본 응답 한 건을 저장한다. 같은 요청이면 최신 응답으로 덮어쓴다."""
    con.execute(
        """
        INSERT INTO raw_api_response (endpoint, request_key, status, body, fetched_at)
        VALUES (?, ?, ?, ?, now())
        ON CONFLICT (endpoint, request_key) DO UPDATE SET
            status = excluded.status,
            body = excluded.body,
            fetched_at = excluded.fetched_at
        """,
        [endpoint, request_key, body.get("status"), json.dumps(body, ensure_ascii=False)],
    )


def raw_bodies(con: duckdb.DuckDBPyConnection, endpoint: str):
    """한 endpoint의 정상(000) 원본 응답을 (request_key, body)로 돌려준다. 같은 보고서(rcept_no)의 중복은 하나만.

    정기보고서 주요정보 응답은 행마다 rcept_no(그 보고서의 접수번호)를 가진다. 같은 보고서가 원본 테이블에
    두 번 있을 수 있다: Phase 1처럼 파라미터만으로 저장한 행과, Phase 2처럼 rcept_no까지 키에 넣어 저장한 행
    (collect_report_details 참고). 둘 다 정규화하면 기본키(rcept_no, row_seq)가 겹치므로 가장 최근에 받은 것만 쓴다.
    rcept_no가 다른 응답(원본과 정정본)은 모두 돌려준다. 그것이 0005의 기록 시간이다.
    """
    latest: dict[str, tuple] = {}
    rows = con.execute(
        "SELECT request_key, body, fetched_at FROM raw_api_response WHERE endpoint = ? AND status = '000' ORDER BY fetched_at",
        [endpoint],
    ).fetchall()
    for request_key, body_text, fetched_at in rows:
        body = json.loads(body_text)
        items = body.get("list") or []
        rcept_no = items[0].get("rcept_no") if items else None
        # 같은 rcept_no는 나중에 받은 것이 덮어쓴다 (fetched_at 오름차순이라 마지막이 최신)
        latest[rcept_no or request_key] = (request_key, body)
    return list(latest.values())


def upsert_corps(con: duckdb.DuckDBPyConnection, corps: list[dict]) -> int:
    """회사 목록을 corp 테이블에 넣는다 (corp_code 기준 upsert). 넣은 행 수를 돌려준다.

    12만 건 가까이 되므로 한 행씩 INSERT하지 않고 DataFrame을 통째로 넘긴다.
    """
    df = pd.DataFrame(corps, columns=["corp_code", "corp_name", "stock_code", "modify_date"])
    df = df.drop_duplicates("corp_code", keep="last")
    con.register("incoming_corp", df)
    try:
        con.execute(
            """
            INSERT INTO corp (corp_code, corp_name, stock_code, modify_date, loaded_at)
            SELECT corp_code,
                   corp_name,
                   NULLIF(stock_code, ''),
                   TRY_STRPTIME(NULLIF(modify_date, ''), '%Y%m%d')::DATE,
                   now()
            FROM incoming_corp
            ON CONFLICT (corp_code) DO UPDATE SET
                corp_name = excluded.corp_name,
                stock_code = excluded.stock_code,
                modify_date = excluded.modify_date,
                loaded_at = excluded.loaded_at
            """
        )
    finally:
        con.unregister("incoming_corp")
    return len(df)


def upsert_disclosures(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    """공시 목록을 disclosure 테이블에 넣는다 (rcept_no 기준 upsert). 넣은 행 수를 돌려준다.

    같은 공시를 다시 받으면 내용을 최신 값으로 덮어쓴다. 정정 공시는 rcept_no가 새로 생기므로 별도 행이 된다.
    """
    if not rows:
        return 0
    columns = ["rcept_no", "corp_code", "corp_name", "stock_code", "corp_cls", "report_nm", "flr_nm", "rcept_dt", "rm"]
    df = pd.DataFrame(rows, columns=columns).drop_duplicates("rcept_no", keep="last")
    con.register("incoming_disclosure", df)
    try:
        con.execute(
            """
            INSERT INTO disclosure
                (rcept_no, corp_code, corp_name, stock_code, corp_cls, report_nm, flr_nm, rcept_dt, rm, loaded_at)
            SELECT rcept_no,
                   corp_code,
                   corp_name,
                   NULLIF(stock_code, ''),
                   NULLIF(corp_cls, ''),
                   report_nm,
                   NULLIF(flr_nm, ''),
                   STRPTIME(rcept_dt, '%Y%m%d')::DATE,
                   NULLIF(rm, ''),
                   now()
            FROM incoming_disclosure
            ON CONFLICT (rcept_no) DO UPDATE SET
                corp_code = excluded.corp_code,
                corp_name = excluded.corp_name,
                stock_code = excluded.stock_code,
                corp_cls = excluded.corp_cls,
                report_nm = excluded.report_nm,
                flr_nm = excluded.flr_nm,
                rcept_dt = excluded.rcept_dt,
                rm = excluded.rm,
                loaded_at = excluded.loaded_at
            """
        )
    finally:
        con.unregister("incoming_disclosure")
    return len(df)
