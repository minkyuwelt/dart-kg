"""정기보고서 주요정보 원본 응답 → 관계 테이블 3개 (major_shareholder, major_shareholder_change, executive).

적재는 두 단계다 (docs/decisions/0005-temporal-model.md).
1) parse_*  : 응답 하나를 행으로 바꾼다. 사실 시간의 시작(valid_from = 보고서 기준일)만 채운다
2) close    : 테이블 전체를 보고 다음 보고서와 비교해 valid_to·superseded_by를 채우고 person을 만든다 (src/loaders/temporal.py)

이 모듈은 1)과 "원본에서 통째로 다시 만들기(rebuild)"를 맡고, rebuild 끝에 2)를 부른다. investment 테이블과
같은 방식이라 파서 규칙을 고친 뒤 rebuild만 다시 돌리면 되고, API는 다시 부르지 않는다.

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
| 임원 시작일     | 없음. 재직기간(월)만 있음                          | tenure_start_on = 기준일 - 재직기간(월). 월 단위 근사. valid_from은 기준일 |
| 사람 식별       | 이름·성별·생년월                                   | person_id = '이름|생년월|성별' (결정 0006)     |

실행 (API 호출 없음):
    python -m src.loaders.relations rebuild
"""

import re
import sys
from datetime import date
from urllib.parse import parse_qs

import duckdb
import pandas as pd

from src.collectors.report_detail import ENDPOINTS
from src.loaders.duckdb_store import connect, raw_bodies
from src.loaders.investments import build_name_index, load_overrides, normalize_name, resolve, to_number
from src.loaders.temporal import close_relations

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
PERIOD_RE = re.compile(r"(?:(\d+)년)?(?:(\d+)개?월)?")  # 공백을 뗀 문자열에 쓴다
DECIMAL_YEARS_RE = re.compile(r"(\d+\.\d+)년")  # '3.2년'
PERIOD_WORD_RE = re.compile(r"\d년|\d개?월")
# 재직기간 안의 날짜: 2022.01.07 / 2022.1.7 / '23.3.22 / 24.05.27. (공백을 뗀 문자열에 쓴다)
TENURE_DATE_RE = re.compile(r"'?(\d{4}|\d{2})\.(\d{1,2})\.(\d{1,2})\.?")
# 임원 현황에서 사람이 아닌 자리 표시 행
PLACEHOLDER_NAMES = {"-", "해당없음", "해당사항없음", "없음", "기재생략"}


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
    """재직기간 '2년 \n1개월' → 25, '3.2년' → 38. 년·월 중 하나도 없으면 None.

    뒤에 '(초임)', '(1회 연임)' 같은 설명이 붙어도 앞의 기간만 읽는다.
    """
    s = re.sub(r"\s+", "", _clean(text))
    if s in ("", "-"):
        return None
    m = DECIMAL_YEARS_RE.match(s)
    if m:
        return round(float(m.group(1)) * 12)
    m = PERIOD_RE.match(s)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    return int(m.group(1) or 0) * 12 + int(m.group(2) or 0)


def _loose_date(m: re.Match) -> date | None:
    """TENURE_DATE_RE의 매치를 date로. 두 자리 연도('23, 24)는 2000년대로 본다. 말이 안 되는 날짜(00일)는 None."""
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_tenure(text: str | None, stlm_dt: date | None) -> dict:
    """재직기간(hffc_pd) 원문에서 선임 시점을 읽는다. 결과: tenure_start_on, tenure_start_method, hffc_months, range_end_on.

    백필에서 본 실제 표기 (devlog 2026-11-23). 회사마다 제각각이라 두 갈래로 읽는다.
      1) 날짜 범위: '2022.01.07~', '2021.7.1 ~ 기준일', '2024.02.01\n~\n현재', "'23.3.22~현재", '24.05.27. ~ 현재',
         '2017.09.\n22~현재'(줄바꿈이 날짜 안에), '2019.03.21~현재(FY2023 정기주총일)', '2022.01.01~2023.12.31'
         → 선임일이 **정확히** 적혀 있으므로 그대로 쓴다 (method 'hffc_pd_range'). 끝 날짜가 있으면 range_end_on
      2) 기간: '2년 1개월', '3.2년', '2.5년(초임)' → 기준일에서 빼서 월 단위로 근사 (method 'stlm_dt_minus_hffc_pd')
    둘 다 아니면('기재생략', '-') 모두 None.
    """
    none = {"tenure_start_on": None, "tenure_start_method": None, "hffc_months": None, "range_end_on": None}
    s = re.sub(r"\s+", "", _clean(text))
    if s in ("", "-"):
        return none
    dates = list(TENURE_DATE_RE.finditer(s))
    if dates and not PERIOD_WORD_RE.search(s[: dates[0].start()]):
        start = _loose_date(dates[0])
        if start is None:
            return none
        end = None
        if len(dates) > 1 and re.fullmatch(r"[~\-–]", s[dates[0].end(): dates[1].start()]):  # '시작~끝'일 때만 끝 날짜
            end = _loose_date(dates[1])
        months = None
        if stlm_dt is not None and stlm_dt >= start:
            months = (stlm_dt.year - start.year) * 12 + (stlm_dt.month - start.month) - (stlm_dt.day < start.day)
        return {"tenure_start_on": start, "tenure_start_method": "hffc_pd_range", "hffc_months": months, "range_end_on": end}
    months = parse_period_months(text)
    if months is None or stlm_dt is None:
        return none
    return {"tenure_start_on": shift_months(stlm_dt, -months), "tenure_start_method": "stlm_dt_minus_hffc_pd",
            "hffc_months": months, "range_end_on": None}


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
        if nm in ("", "계", "합계") or re.sub(r"\s+", "", nm) in PLACEHOLDER_NAMES:
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


def make_person_id(nm: str, birth_ym: str | None, sexdstn: str | None) -> str:
    """사람 식별자 '양종희|1961-06|남'. 생년월·성별이 없으면 '?' (결정 0006)."""
    return f"{nm}|{birth_ym or '?'}|{sexdstn or '?'}"


def parse_executives(request_key: str, body: dict) -> list[dict]:
    """임원 현황 응답 → executive 행.

    valid_from은 보고서 기준일이다 (스냅샷 체인, temporal.py 참고). 선임 시점은 별도 컬럼 tenure_start_on에
    둔다: 재직기간이 날짜 범위면 그 시작일(정확), 기간이면 기준일에서 뺀 근사값. 못 읽으면 NULL (parse_tenure).
    """
    params = _report_params(request_key)
    rows = []
    for row_seq, item in enumerate(body.get("list", []), start=1):
        nm = _clean(item.get("nm"))
        if not nm or re.sub(r"\s+", "", nm) in PLACEHOLDER_NAMES:
            continue
        stlm_dt = parse_date(item.get("stlm_dt"))
        tenure = parse_tenure(item.get("hffc_pd"), stlm_dt)
        # 임기만료일이 비어 있고 재직기간에 끝 날짜가 적혀 있으면('2022.01.01~2023.12.31') 그것을 임기만료일로 쓴다
        tenure_end = parse_date(item.get("tenure_end_on")) or tenure["range_end_on"]
        birth_ym, sexdstn = parse_year_month(item.get("birth_ym")), _text(item.get("sexdstn"))
        rows.append(
            {
                "source_rcept_no": item["rcept_no"],
                "row_seq": row_seq,
                "corp_code": item["corp_code"],
                "bsns_year": params["bsns_year"],
                "reprt_code": params["reprt_code"],
                "nm": nm,
                "sexdstn": sexdstn,
                "birth_ym": birth_ym,
                "person_id": make_person_id(nm, birth_ym, sexdstn),
                "ofcps": _text(item.get("ofcps")),
                "rgist_exctv_at": _text(item.get("rgist_exctv_at")),
                "fte_at": _text(item.get("fte_at")),
                "chrg_job": _text(item.get("chrg_job")),
                "main_career": _text(item.get("main_career")),
                "mxmm_shrholdr_relate": _text(item.get("mxmm_shrholdr_relate")),
                "hffc_pd_raw": _text(item.get("hffc_pd")),
                "hffc_months": tenure["hffc_months"],
                "tenure_start_on": tenure["tenure_start_on"],
                "tenure_start_method": tenure["tenure_start_method"],
                "tenure_end_on": tenure_end,
                "stlm_dt": stlm_dt,
                "valid_from": stlm_dt,
            }
        )
    return rows


def dedupe_changes(rows: list[dict]) -> list[dict]:
    """같은 (회사, 변동일, 주주명) 변동이 여러 보고서에 실리면 하나만 남긴다.

    주주명은 법인 표기를 뗀 정규화 이름으로 비교한다. 같은 변동이 보고서마다 '하나은행', '(주)하나은행'처럼
    다르게 적혀 둘 다 남는 일이 백필에서 실제로 있었다 (devlog 2026-11-23). 지분율이 같은 것으로 같은 사건임을 확인함.

    남기는 것은 접수번호가 가장 작은(=가장 먼저 보고한) 보고서의 행이다. 접수번호 앞 8자리가 접수일이라
    문자열 비교가 시간 순서와 같다. "언제 처음 알게 됐나"(기록 시간)를 보존하기 위해서다.
    """
    best: dict[tuple, dict] = {}
    for row in rows:
        key = (row["corp_code"], row["change_on"], normalize_name(row["mxmm_shrholdr_nm"])[0])
        if key not in best or row["source_rcept_no"] < best[key]["source_rcept_no"]:
            best[key] = row
    return list(best.values())


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
    "tenure_start_on", "tenure_start_method", "tenure_end_on", "person_id", "stlm_dt", "valid_from",
]


def rebuild_relations(con: duckdb.DuckDBPyConnection) -> dict:
    """raw_api_response의 주요정보 응답 전체로 관계 테이블 3개와 person을 다시 만든다 (API 호출 없음).

    행을 넣은 뒤 같은 트랜잭션에서 닫기(valid_to·superseded_by, src/loaders/temporal.py)까지 한다.
    """
    index = build_name_index(con)
    overrides = load_overrides()

    shareholders: list[dict] = []
    for request_key, body in raw_bodies(con, ENDPOINTS["major_shareholder"]):
        shareholders.extend(parse_major_shareholders(request_key, body, index, overrides))
    changes: list[dict] = []
    for request_key, body in raw_bodies(con, ENDPOINTS["major_shareholder_change"]):
        changes.extend(parse_major_shareholder_changes(request_key, body))
    changes = dedupe_changes(changes)
    executives: list[dict] = []
    for request_key, body in raw_bodies(con, ENDPOINTS["executive"]):
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
        # 지주 사업보고서의 "계열회사간 임원 겸직 현황" 표 (0014). 임원 표가 없는 자회사의 겸직 자리. 사람을 임원 표로 잇기 때문에 executive 다음
        from src.loaders.concurrent_positions import rebuild_concurrent_positions
        counts.update(rebuild_concurrent_positions(con))
        counts.update(close_relations(con))
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
