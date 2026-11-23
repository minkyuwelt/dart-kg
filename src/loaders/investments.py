"""타법인 출자현황 원본 응답 → investment 테이블 (출자 대상 회사명을 corp_code로 연결).

출자 대상(inv_prm)은 회사 "이름"으로만 온다. 2025 사업보고서 4개 지주 61행에서 확인한 문제:

| 문제                         | 예                                   | 처리                         |
|------------------------------|--------------------------------------|------------------------------|
| 합계 행                      | '합계'                               | 제외                         |
| 법인 표기                    | '㈜우리은행', '우리금융캐피탈㈜'      | 제거                         |
| 각주 표시                    | '핀크(주3)', '㈜우리투자증권 \\n주4)'  | 제거                         |
| 주식 종류                    | '하나캐피탈(보통주)', '큐리오시스 보통주', '(상환우)', '(무의결권전환주)' | share_class 컬럼으로 분리 |
| 같은 회사 여러 행(취득 시점별) | '태영건설' 2행 (최초 취득일만 다름)    | row_seq(응답 내 순서)로 구분   |
| 영문 약자를 한글로 등록       | 'KB증권' → DART '케이비증권'          | 알파벳을 한글 읽기로 바꿔 재시도 |
| 같은 이름의 과거 법인         | '하나은행' 3곳, '국민은행' 3곳        | 수동 매핑(근거 포함)          |
| 약칭                         | 'KB라이프생명' → '케이비라이프생명보험' | 수동 매핑                     |

연결 순서 (resolve_method 값)
1. override    : config/corp_name_overrides.csv에 있으면 그 값 (corp_code가 비어 있으면 override_unresolvable)
2. exact       : 정규화한 이름이 corp 테이블의 회사 하나와 정확히 일치
3. hangul      : 알파벳을 한글 읽기로 바꾼 이름이 회사 하나와 일치
4. ambiguous   : 후보가 2곳 이상 → 추측하지 않고 NULL로 둔다. 근거를 확인해 override에 추가할 것
5. not_found   : 후보 없음

실행 (원본 응답에서 investment 테이블을 다시 만든다. API 호출 없음):
    python -m src.loaders.investments rebuild
"""

import csv
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

import duckdb
import pandas as pd

from src.loaders.duckdb_store import REPO_ROOT, connect, raw_bodies

ENDPOINT = "otrCprInvstmntSttus.json"
OVERRIDES_PATH = REPO_ROOT / "config" / "corp_name_overrides.csv"

# 알파벳 한글 읽기. DART 등록명은 'KB' → '케이비', 'DS' → '디에스', 'EZ' → '이지'처럼 적혀 있다.
LETTER_READINGS = dict(
    zip(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "에이 비 씨 디 이 에프 지 에이치 아이 제이 케이 엘 엠 엔 오 피 큐 알 에스 티 유 브이 더블유 엑스 와이 지".split(),
    )
)

# '(보통주)'처럼 괄호 안에 있거나, '큐리오시스 보통주'처럼 이름 끝에 붙어 있다
SHARE_CLASS_RE = re.compile(
    r"\((보통주|우선주|종류주|상환우|상환전환우|전환우|무의결권전환주|주식)\)|\s(보통주|우선주|종류주)\s*$"
)
FOOTNOTE_RE = re.compile(r"\(?\s*주\s*\d+\)")  # '(주3)', '주4)'
CORP_MARK_RE = re.compile(r"\(주\)|㈜|주식회사|\(株\)")


def normalize_name(raw: str) -> tuple[str, str | None]:
    """출자 대상 이름을 정규화한다. (정규화한 이름, 주식 종류)를 돌려준다.

    순서가 중요하다: 각주 '(주3)'를 먼저 지워야 법인 표기 '(주)'와 헷갈리지 않는다.
    """
    s = raw.replace("\n", " ")
    share = SHARE_CLASS_RE.search(s)
    s = SHARE_CLASS_RE.sub("", s)
    s = FOOTNOTE_RE.sub("", s)
    s = CORP_MARK_RE.sub("", s)
    s = re.sub(r"\s+", "", s)
    return s, (share.group(1) or share.group(2) if share else None)


def to_hangul_letters(name: str) -> str:
    """이름 안의 알파벳을 한글 읽기로 바꾼다. 'KB증권' → '케이비증권'."""
    return "".join(LETTER_READINGS.get(ch, ch) for ch in name.upper())


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[str, str | None]:
    """수동 매핑을 읽는다. {정규화한 이름: corp_code 또는 None(연결 불가)}"""
    with path.open(encoding="utf-8") as f:
        return {normalize_name(row["raw_name"])[0]: (row["corp_code"] or None) for row in csv.DictReader(f)}


def build_name_index(con: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    """corp 테이블 전체를 {정규화한 회사명: [corp_code, ...]}로 만든다."""
    index: dict[str, list[str]] = {}
    for corp_code, corp_name in con.execute("SELECT corp_code, corp_name FROM corp").fetchall():
        index.setdefault(normalize_name(corp_name)[0], []).append(corp_code)
    return index


def resolve(name: str, index: dict[str, list[str]], overrides: dict[str, str | None]) -> tuple[str | None, str]:
    """정규화한 이름을 corp_code로 연결한다. (corp_code 또는 None, resolve_method)"""
    if name in overrides:
        code = overrides[name]
        return code, "override" if code else "override_unresolvable"
    for method, candidate in (("exact", name), ("hangul", to_hangul_letters(name))):
        codes = index.get(candidate, [])
        if len(codes) == 1:
            return codes[0], method
        if len(codes) > 1:
            return None, "ambiguous"
    return None, "not_found"


def to_number(text: str | None) -> float | None:
    """'1,234,567' → 1234567.0, '-' 또는 '' → None"""
    if text is None:
        return None
    s = text.replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


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

    df = pd.DataFrame(rows)
    con.begin()
    try:
        con.execute("DELETE FROM investment")
        if not df.empty:
            con.register("incoming_investment", df)
            con.execute(
                """
                INSERT INTO investment
                SELECT rcept_no, row_seq, investor_corp_code, bsns_year, reprt_code, inv_prm_raw, investee_name, share_class,
                       investee_corp_code, resolve_method, invstmnt_purps,
                       TRY_STRPTIME(NULLIF(frst_acqs_de, '-'), '%Y.%m.%d')::DATE,
                       trmend_blce_qy::BIGINT, trmend_blce_qota_rt, trmend_blce_acntbk_amount::BIGINT,
                       TRY_CAST(NULLIF(stlm_dt, '') AS DATE),
                       now()
                FROM incoming_investment
                """
            )
            con.unregister("incoming_investment")
        con.commit()
    except Exception:
        con.rollback()
        raise
    summary = con.execute(
        "SELECT resolve_method, count(*) FROM investment GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    return {"rows": len(df), "by_method": dict(summary)}


if __name__ == "__main__":
    if sys.argv[1:] != ["rebuild"]:
        sys.exit("사용법: python -m src.loaders.investments rebuild")
    with connect() as con:
        print(rebuild_investments(con))
