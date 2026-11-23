"""정기보고서 주요정보 원본 응답 → 관계 테이블 3개 (major_shareholder, major_shareholder_change, executive).

적재는 두 단계다 (docs/decisions/0005-temporal-model.md).
1) parse_*  : 응답 하나를 행으로 바꾼다. 사실 시간의 시작(valid_from)만 채운다
2) close_*  : 테이블 전체를 보고 다음 보고서와 비교해 valid_to·superseded_by를 채운다 (Phase 2 E 단계, 아직 없음)

이 모듈은 1)과 "원본에서 통째로 다시 만들기(rebuild)"를 맡는다. investment 테이블과 같은 방식이라
파서 규칙을 고친 뒤 rebuild만 다시 돌리면 되고, API는 다시 부르지 않는다.

응답 형식에서 확인한 것 (tests/fixtures/*_00688996_2025_11011.json, 2026-11-23)

| 항목            | 형식                                              | 처리                                   |
|-----------------|---------------------------------------------------|----------------------------------------|
| 날짜            | '2025-12-31', '2019년 12월 31일', '2019.01.11'     | parse_date가 세 형식을 모두 받는다     |
| 생년월          | '1961년 06월'                                     | parse_year_month → '1961-06'           |
| 주식 수         | '33,107,971'                                      | 쉼표 제거                              |
| 지분율          | 현황 '8.68', 변동현황 '9.97%'                     | % 제거                                 |
| 재직기간        | '2년 \\n1개월', '1년8개월', '9개월', '-'           | parse_period_months → 25, 20, 9, None  |
| 합계 행         | 최대주주 현황 nm='계' (relate 키 없음)            | 제외                                   |
| 변동 누적       | 2025 보고서에 2019년 변동까지 28행                | (회사, 변동일, 주주명)으로 중복 제거. 처음 보고한 보고서를 source로 |
| 임원 시작일     | 없음. 재직기간(월)만 있음                          | valid_from = 기준일 - 재직기간(월). 월 단위 근사 |

실행 (API 호출 없음):
    python -m src.loaders.relations rebuild
"""

import json
import re
import sys
from datetime import date
from urllib.parse import parse_qs

import duckdb
import pandas as pd

from src.collectors.report_detail import ENDPOINTS
from src.loaders.duckdb_store import connect
from src.loaders.investments import build_name_index, load_overrides, normalize_name, resolve, to_number

DATE_PATTERNS = (
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"),  # 2025-12-31
    re.compile(r"^(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일$"),  # 2019년 12월 31일
    re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$"),  # 2019.01.11
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),  # 20190111
)
YEAR_MONTH_PATTERNS = (
    re.compile(r"^(\d{4})\s*년\s*(\d{1,2})\s*월$"),  # 1961년 06월
    re.compile(r"^(\d{4})[.-](\d{1,2})$"),  # 1961.06, 1961-06
)
PERIOD_RE = re.compile(r"(?:(\d+)\s*년)?\s*(?:(\d+)\s*개?월)?")


def _clean(text: str | None) -> str:
    return (text or "").strip()


def _text(text: str | None) -> str | None:
    """비어 있거나 '-'(DART의 "해당 없음" 표기)이면 None, 아니면 앞뒤 공백을 뗀 문자열."""
    s = _clean(text)
    return None if s in ("", "-") else s


def parse_date(text: str | None) -> date | None:
    """DART 주요정보 API의 날짜 세 형식을 date로 바꾼다. 비어 있거나 '-'이면 None."""
    s = _clean(text)
    if s in ("", "-"):
        return None
    for pattern in DATE_PATTERNS:
        m = pattern.match(s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
    return None


def parse_year_month(text: str | None) -> str | None:
    """'1961년 06월' → '1961-06'. 비어 있으면 None."""
    s = _clean(text)
    for pattern in YEAR_MONTH_PATTERNS:
        m = pattern.match(s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return None


def parse_percent(text: str | None) -> float | None:
    """'9.97%' → 9.97, '8.68' → 8.68, '-' → None."""
    return to_number(_clean(text).rstrip("%"))


def parse_period_months(text: str | None) -> int | None:
    """재직기간 '2년 \\n1개월' → 25. 년·월 중 하나도 없으면 None."""
    s = re.sub(r"\s+", "", _clean(text))
    if s in ("", "-"):
        return None
    m = PERIOD_RE.fullmatch(s)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    return int(m.group(1) or 0) * 12 + int(m.group(2) or 0)


def shift_months(d: date, months: int) -> date:
    """날짜를 months개월 앞뒤로 옮긴다. 말일이 없는 달이면 그 달의 마지막 날로 맞춘다."""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    next_month_first = date(year + (month == 12), month % 12 + 1, 1)
    last_day = (next_month_first - date.resolution).day
    return date(year, month, min(d.day, last_day))


def _report_params(request_key: str) -> dict:
    return {k: v[0] for k, v in parse_qs(request_key).items()}


def parse_major_shareholders(request_key: str, body: dict, index: dict, overrides: dict) -> list[dict]:
    """최대주주 현황 응답 → major_shareholder 행. 합계 행('계')은 뺀다.

    최대주주가 회사(지주, 국민연금공단 등)면 Phase 1의 이름 연결 규칙으로 corp_code를 찾아 본다.
    사람이거나 DART 공시 대상이 아니면 not_found로 남고, 이름(nm)은 그대로 보관한다.
    """
    params = _report_params(request_key)
    rows = []
    for row_seq, item in enumerate(body.get("list", []), start=1):
        nm = _clean(item.get("nm"))
        if nm in ("", "계", "합계"):
            continue
        name, _ = normalize_name(nm)
        holder_corp_code, method = resolve(name, index, overrides)
        stlm_dt = parse_date(item.get("stlm_dt"))
        rows.append(
            {
                "source_rcept_no": item["rcept_no"],
                "row_seq": row_seq,
                "corp_code": item["corp_code"],
                "bsns_year": params["bsns_year"],
                "reprt_code": params["reprt_code"],
                "nm": nm,
                "relate": _text(item.get("relate")),
                "stock_knd": _text(item.get("stock_knd")),
                "holder_corp_code": holder_corp_code,
                "resolve_method": method,
                "bsis_posesn_stock_co": to_number(item.get("bsis_posesn_stock_co")),
                "bsis_posesn_stock_qota_rt": parse_percent(item.get("bsis_posesn_stock_qota_rt")),
                "trmend_posesn_stock_co": to_number(item.get("trmend_posesn_stock_co")),
                "trmend_posesn_stock_qota_rt": parse_percent(item.get("trmend_posesn_stock_qota_rt")),
                "rm": _text(item.get("rm")),
                "stlm_dt": stlm_dt,
                "valid_from": stlm_dt,
            }
        )
    return rows


def parse_major_shareholder_changes(request_key: str, body: dict) -> list[dict]:
    """최대주주 변동현황 응답 → major_shareholder_change 행.

    변동일(change_on)이 있어 사실 시간이 일 단위다. 보고서마다 과거 변동이 누적되어 실리므로
    여기서는 행을 그대로 뽑고, 중복 제거는 rebuild에서 (corp_code, change_on, 주주명) 기준으로 한다.
    """
    params = _report_params(request_key)
    rows = []
    for item in body.get("list", []):
        change_on = parse_date(item.get("change_on"))
        name = _clean(item.get("mxmm_shrholdr_nm"))
        if change_on is None or not name:
            continue
        rows.append(
            {
                "corp_code": item["corp_code"],
                "change_on": change_on,
                "mxmm_shrholdr_nm": name,
                "posesn_stock_co": to_number(item.get("posesn_stock_co")),
                "qota_rt": parse_percent(item.get("qota_rt")),
                "change_cause": _text(item.get("change_cause")),
                "rm": _text(item.get("rm")),
                "source_rcept_no": item["rcept_no"],
                "bsns_year": params["bsns_year"],
                "reprt_code": params["reprt_code"],
                "stlm_dt": parse_date(item.get("stlm_dt")),
                "valid_from": change_on,
            }
        )
    return rows


def parse_executives(request_key: str, body: dict) -> list[dict]:
    """임원 현황 응답 → executive 행.

    선임일 필드가 없어서 valid_from은 보고서 기준일에서 재직기간(월)을 뺀 날짜다 (월 단위 근사).
    재직기간을 못 읽으면 기준일을 그대로 쓴다. 어느 쪽인지 valid_from_method에 남긴다.
    """
    params = _report_params(request_key)
    rows = []
    for row_seq, item in enumerate(body.get("list", []), start=1):
        nm = _clean(item.get("nm"))
        if not nm:
            continue
        stlm_dt = parse_date(item.get("stlm_dt"))
        months = parse_period_months(item.get("hffc_pd"))
        if stlm_dt is not None and months is not None:
            valid_from, method = shift_months(stlm_dt, -months), "stlm_dt_minus_hffc_pd"
        else:
            valid_from, method = stlm_dt, "stlm_dt"
        rows.append(
            {
                "source_rcept_no": item["rcept_no"],
                "row_seq": row_seq,
                "corp_code": item["corp_code"],
                "bsns_year": params["bsns_year"],
                "reprt_code": params["reprt_code"],
                "nm": nm,
                "sexdstn": _text(item.get("sexdstn")),
                "birth_ym": parse_year_month(item.get("birth_ym")),
                "ofcps": _text(item.get("ofcps")),
                "rgist_exctv_at": _text(item.get("rgist_exctv_at")),
                "fte_at": _text(item.get("fte_at")),
                "chrg_job": _text(item.get("chrg_job")),
                "main_career": _text(item.get("main_career")),
                "mxmm_shrholdr_relate": _text(item.get("mxmm_shrholdr_relate")),
                "hffc_pd_raw": _text(item.get("hffc_pd")),
                "hffc_months": months,
                "tenure_end_on": parse_date(item.get("tenure_end_on")),
                "stlm_dt": stlm_dt,
                "valid_from": valid_from,
                "valid_from_method": method,
            }
        )
    return rows


def dedupe_changes(rows: list[dict]) -> list[dict]:
    """같은 (회사, 변동일, 주주명) 변동이 여러 보고서에 실리면 하나만 남긴다.

    남기는 것은 접수번호가 가장 작은(=가장 먼저 보고한) 보고서의 행이다. 접수번호 앞 8자리가 접수일이라
    문자열 비교가 시간 순서와 같다. "언제 처음 알게 됐나"(기록 시간)를 보존하기 위해서다.
    """
    best: dict[tuple, dict] = {}
    for row in rows:
        key = (row["corp_code"], row["change_on"], row["mxmm_shrholdr_nm"])
        if key not in best or row["source_rcept_no"] < best[key]["source_rcept_no"]:
            best[key] = row
    return list(best.values())


def _raw_bodies(con: duckdb.DuckDBPyConnection, endpoint: str):
    for request_key, body in con.execute(
        "SELECT request_key, body FROM raw_api_response WHERE endpoint = ? AND status = '000'", [endpoint]
    ).fetchall():
        yield request_key, json.loads(body)


def _replace_table(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict], columns: list[str]) -> int:
    """테이블을 비우고 rows로 다시 채운다. 트랜잭션은 호출하는 쪽이 연다."""
    con.execute(f"DELETE FROM {table}")
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=columns)
    view = f"incoming_{table}"
    con.register(view, df)
    try:
        con.execute(f"INSERT INTO {table} ({', '.join(columns)}, loaded_at) SELECT {', '.join(columns)}, now() FROM {view}")
    finally:
        con.unregister(view)
    return len(df)


MAJOR_SHAREHOLDER_COLUMNS = [
    "source_rcept_no", "row_seq", "corp_code", "bsns_year", "reprt_code", "nm", "relate", "stock_knd",
    "holder_corp_code", "resolve_method", "bsis_posesn_stock_co", "bsis_posesn_stock_qota_rt",
    "trmend_posesn_stock_co", "trmend_posesn_stock_qota_rt", "rm", "stlm_dt", "valid_from",
]
MAJOR_SHAREHOLDER_CHANGE_COLUMNS = [
    "corp_code", "change_on", "mxmm_shrholdr_nm", "posesn_stock_co", "qota_rt", "change_cause", "rm",
    "source_rcept_no", "bsns_year", "reprt_code", "stlm_dt", "valid_from",
]
EXECUTIVE_COLUMNS = [
    "source_rcept_no", "row_seq", "corp_code", "bsns_year", "reprt_code", "nm", "sexdstn", "birth_ym", "ofcps",
    "rgist_exctv_at", "fte_at", "chrg_job", "main_career", "mxmm_shrholdr_relate", "hffc_pd_raw", "hffc_months",
    "tenure_end_on", "stlm_dt", "valid_from", "valid_from_method",
]


def rebuild_relations(con: duckdb.DuckDBPyConnection) -> dict:
    """raw_api_response의 주요정보 응답 전체로 관계 테이블 3개를 다시 만든다 (API 호출 없음).

    valid_to·superseded_by는 여기서 채우지 않는다 (close 단계, 0005). 다시 만들면 NULL로 돌아간다.
    """
    index = build_name_index(con)
    overrides = load_overrides()

    shareholders: list[dict] = []
    for request_key, body in _raw_bodies(con, ENDPOINTS["major_shareholder"]):
        shareholders.extend(parse_major_shareholders(request_key, body, index, overrides))
    changes: list[dict] = []
    for request_key, body in _raw_bodies(con, ENDPOINTS["major_shareholder_change"]):
        changes.extend(parse_major_shareholder_changes(request_key, body))
    changes = dedupe_changes(changes)
    executives: list[dict] = []
    for request_key, body in _raw_bodies(con, ENDPOINTS["executive"]):
        executives.extend(parse_executives(request_key, body))

    con.begin()
    try:
        counts = {
            "major_shareholder": _replace_table(con, "major_shareholder", shareholders, MAJOR_SHAREHOLDER_COLUMNS),
            "major_shareholder_change": _replace_table(
                con, "major_shareholder_change", changes, MAJOR_SHAREHOLDER_CHANGE_COLUMNS
            ),
            "executive": _replace_table(con, "executive", executives, EXECUTIVE_COLUMNS),
        }
        con.commit()
    except Exception:
        con.rollback()
        raise
    counts["holder_by_method"] = dict(
        con.execute("SELECT resolve_method, count(*) FROM major_shareholder GROUP BY 1 ORDER BY 2 DESC").fetchall()
    )
    return counts


if __name__ == "__main__":
    if sys.argv[1:] != ["rebuild"]:
        sys.exit("사용법: python -m src.loaders.relations rebuild")
    with connect() as con:
        print(rebuild_relations(con))
