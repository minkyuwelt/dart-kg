"""타법인 출자현황 원본 응답 → investment 테이블.

출자 대상(inv_prm)은 회사 "이름"으로만 온다. 이름을 corp_code로 잇는 규칙(법인 표기·각주·주식 종류 떼기, 수동 매핑, 후보가 여럿이면
추측하지 않기)은 src/loaders/corp_names.py에 있고, 여기서는 그 결과를 investee_name·share_class·investee_corp_code·resolve_method 열에 적는다.

이 표에서만 있는 일
- 합계 행 '합계'는 뺀다
- 같은 회사가 취득 시점별로 여러 행('태영건설' 2행, 최초 취득일만 다름) → row_seq(응답 내 순서)로 구분
- 끝의 '우' 하나('삼성전자우')는 회사 목록으로 확인한 뒤에만 뗀다 (strip_bare_preferred)
- 날짜(frst_acqs_de·stlm_dt)는 SQL에서 바꾼다 (TRY_STRPTIME, 형식 '2019.01.11'). 다른 표처럼 parse_date로 옮기는 것은 노트북에서
  형식을 확인한 뒤 (src/loaders/parsing.py 참고)

실행 (원본 응답에서 investment 테이블을 다시 만든다. API 호출 없음):
    python -m src.loaders.investments rebuild
"""

import re
import sys
from urllib.parse import parse_qs

import duckdb

from src.loaders.corp_names import build_name_index, load_overrides, normalize_name, resolve, strip_bare_preferred
from src.loaders.duckdb_store import connect, raw_bodies, replace_rows, transaction
from src.loaders.parsing import to_number
from src.loaders.temporal import close_investments

ENDPOINT = "otrCprInvstmntSttus.json"

# 적재 열 순서. 날짜·정수 열은 SQL 식으로 바꿔 넣는다 (replace_rows의 expressions)
INVESTMENT_COLUMNS = [
    "rcept_no", "row_seq", "investor_corp_code", "bsns_year", "reprt_code", "inv_prm_raw", "investee_name", "share_class",
    "investee_corp_code", "resolve_method", "invstmnt_purps", "frst_acqs_de",
    "trmend_blce_qy", "trmend_blce_qota_rt", "trmend_blce_acntbk_amount", "stlm_dt", "valid_from",
]
INVESTMENT_EXPRESSIONS = {
    "frst_acqs_de": "TRY_STRPTIME(NULLIF(frst_acqs_de, '-'), '%Y.%m.%d')::DATE",
    "trmend_blce_qy": "trmend_blce_qy::BIGINT",
    "trmend_blce_acntbk_amount": "trmend_blce_acntbk_amount::BIGINT",
    "stlm_dt": "TRY_CAST(NULLIF(stlm_dt, '') AS DATE)",
    "valid_from": "TRY_CAST(NULLIF(valid_from, '') AS DATE)",  # valid_from = 보고서 기준일 (0005)
}


def parse_investment_rows(request_key: str, body: dict, index: dict, overrides: dict) -> list[dict]:
    """원본 응답 하나를 investment 행들로 바꾼다. 합계 행은 뺀다."""
    params = {k: v[0] for k, v in parse_qs(request_key).items()}
    rows = []
    # 같은 회사를 다른 시점에 취득한 기록이 이름이 같은 별도 행으로 실리기도 한다 (최초 취득일만 다름).
    # 이름으로는 행을 구분할 수 없으므로 응답 안에서의 순서(row_seq)를 행 식별자로 쓴다.
    for row_seq, item in enumerate(body.get("list", []), start=1):
        name, share_class = normalize_name(item["inv_prm"])
        if name == "합계":
            continue
        if base := strip_bare_preferred(name, index, overrides):  # '삼성전자우' → '삼성전자'
            name, share_class = base, share_class or "우"
        corp_code, method = resolve(name, index, overrides)
        rows.append(
            {
                "rcept_no": item["rcept_no"],
                "row_seq": row_seq,
                "investor_corp_code": item["corp_code"],
                "bsns_year": params["bsns_year"],
                "reprt_code": params["reprt_code"],
                "inv_prm_raw": item["inv_prm"],
                "investee_name": name,
                "share_class": share_class,
                "investee_corp_code": corp_code,
                "resolve_method": method,
                "invstmnt_purps": re.sub(r"\s+", " ", item.get("invstmnt_purps") or "").strip() or None,
                "frst_acqs_de": (item.get("frst_acqs_de") or "").strip(),
                "trmend_blce_qy": to_number(item.get("trmend_blce_qy")),
                "trmend_blce_qota_rt": to_number(item.get("trmend_blce_qota_rt")),
                "trmend_blce_acntbk_amount": to_number(item.get("trmend_blce_acntbk_amount")),
                "stlm_dt": (item.get("stlm_dt") or "").strip(),
                "valid_from": (item.get("stlm_dt") or "").strip(),
            }
        )
    return rows


def rebuild_investments(con: duckdb.DuckDBPyConnection) -> dict:
    """raw_api_response에 있는 타법인 출자현황 응답 전체로 investment 테이블을 다시 만든다.

    매칭 규칙이나 수동 매핑을 고친 뒤 이 함수만 다시 돌리면 된다 (API 호출 없음).
    """
    index = build_name_index(con)
    overrides = load_overrides()
    rows = []
    for request_key, body in raw_bodies(con, ENDPOINT):
        rows.extend(parse_investment_rows(request_key, body, index, overrides))

    with transaction(con):
        count = replace_rows(con, "investment", rows, INVESTMENT_COLUMNS, INVESTMENT_EXPRESSIONS)
        closed = close_investments(con)  # valid_to·superseded_by (src/loaders/temporal.py)
    summary = con.execute(
        "SELECT resolve_method, count(*) FROM investment GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    return {"rows": count, "by_method": dict(summary), **closed}


if __name__ == "__main__":
    if sys.argv[1:] != ["rebuild"]:
        sys.exit("사용법: python -m src.loaders.investments rebuild")
    with connect() as con:
        print(rebuild_investments(con))
