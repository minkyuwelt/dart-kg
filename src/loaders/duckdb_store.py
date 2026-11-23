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
