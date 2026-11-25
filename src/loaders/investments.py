"""타법인 출자현황 원본 응답 → investment 테이블 (출자 대상 회사명을 corp_code로 연결).

출자 대상(inv_prm)은 회사 "이름"으로만 온다. 2025 사업보고서 4개 지주 61행에서 확인한 문제:

| 문제                         | 예                                   | 처리                         |
|------------------------------|--------------------------------------|------------------------------|
| 합계 행                      | '합계'                               | 제외                         |
| 법인 표기                    | '㈜우리은행', '우리금융캐피탈㈜'      | 제거                         |
| 각주 표시                    | '핀크(주3)', '㈜우리투자증권 \\n주4)'  | 제거                         |
| 주식 종류                    | '하나캐피탈(보통주)', '팀프레시_상환전환우선주', '키움증권RCPS', '현대차2우B' | share_class 컬럼으로 분리 |
| 끝의 '우' 하나               | '삼성전자우' ('대우'는 원래 이름)      | 뗀 이름이 회사 하나와 맞을 때만 분리 |
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
import unicodedata
import sys
from pathlib import Path
from urllib.parse import parse_qs

import duckdb
import pandas as pd

from src.loaders.duckdb_store import REPO_ROOT, connect, raw_bodies
from src.loaders.temporal import close_investments

ENDPOINT = "otrCprInvstmntSttus.json"
OVERRIDES_PATH = REPO_ROOT / "config" / "corp_name_overrides.csv"

# 알파벳 한글 읽기. DART 등록명은 'KB' → '케이비', 'DS' → '디에스', 'EZ' → '이지'처럼 적혀 있다.
LETTER_READINGS = dict(
    zip(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "에이 비 씨 디 이 에프 지 에이치 아이 제이 케이 엘 엠 엔 오 피 큐 알 에스 티 유 브이 더블유 엑스 와이 지".split(),
    )
)

# 주식 종류 표기. 이름 끝에 괄호·밑줄과 함께, 또는 그냥 붙어 온다 (2026-11-25 노트북 진단에서 955개 이름을 보고 정함):
#   '하나캐피탈(보통주)', '큐리오시스 보통주', '팀프레시_상환전환우선주', '키움증권RCPS', '현대차2우B', '더존비즈온1종RCPS',
#   '콥틱제6종상환전환우선주식', '한국디지털에셋(무의결권전환주)', '딕스젠1우선주(전환상환)', '페리지에어로스페이스RCPS2'
# 받침 없는 '우' 하나('삼성전자우')는 원래 이름의 끝일 수도 있어서('대우', '영우', '좋은만남선우') 여기서 떼지 않는다 → strip_bare_preferred
_CLASS_CORE = (
    r"(?:제?\d+종)?(?:기명식)?(?:무의결권?)?(?:상환전환|전환상환|상환|전환|환)?"
    r"(?:우선주식?|종류주식?|보통주|전환주|(?<![A-Za-z])(?:RCPS|CPS|RPS))"  # 영문 이름 끝(CORPS)은 떼지 않게
    r"|\d+우(?:선주|B)?"                       # '2우B', '1우', '3우선주', '14우'
    r"|(?:상환전환|전환상환|상환|전환)우"          # '상환전환우', '전환우' (앞에 종류 말이 있는 '우'만)
)
# 밑줄 뒤에서만 인정하는 잘린 꼴: '_우선', '_보통', '_2종', '_1종종', '_1종종류', '_제2종'
_CLASS_AFTER_UNDERSCORE = r"(?:제?\d+종(?:종류?)?|우선|보통|종류)"
# 종류 말 뒤에 붙는 꼬리말: '보통주비상장', '보통주구주', '1종종류주인수', '2종종류주추가증자', '보통주출자', 'RCPS2'
_CLASS_TAIL = r"(?:비상장|상장|구주|신주|추가증자|증자|인수|출자|\d)?"
CLASS_SUFFIX_RE = re.compile(rf"(?:[_\-]?({_CLASS_CORE})|_({_CLASS_AFTER_UNDERSCORE})){_CLASS_TAIL}$")
# 괄호 안이 종류 말이면 뗀다. '(우선주,의결권제한有)', '(보통주/우선주)', '(전환상환)'
PAREN_CLASS_RE = re.compile(rf"\(((?:{_CLASS_CORE})(?:[,/][^()]*)?|상환전환|전환상환|상환|전환)\)$")
# 괄호 안이 주석이면 뗀다 (종류 칸에는 넣지 않음). '(비상장)', '(RCPS)(USD)', '(출자금)', '(전문)'
PAREN_NOTE_RE = re.compile(r"\((비상장|상장|구주|신주|출자금|주식|전문|USD|EUR)\)$")
FOOTNOTE_RE = re.compile(r"\(?\s*주\s*\d+\)|\(\*\d+\)")  # '(주3)', '주4)', '(*7)'
CORP_MARK_RE = re.compile(r"\(주\)|㈜|주식회사|\(株\)|^주\)")


def _strip_share_suffixes(s: str) -> tuple[str, str | None]:
    """이름 끝의 주식 종류·주석을 더 없을 때까지 뗀다. (남은 이름, 종류 말을 원래 순서로 이은 것)

    '키움증권4우선주(전환)' → ('키움증권', '4우선주(전환)'). 주석('비상장', 'USD')과 잘린 괄호는 종류 칸에 넣지 않는다
    """
    parts: list[str] = []
    while True:
        before = s
        if s.count("(") > s.count(")"):      # 잘린 괄호: '딕스젠1우선주(전환상', '로킷헬스케어우선주('
            s = s[: s.rindex("(")]
        elif s.endswith(")") and s.count(")") > s.count("("):  # 짝 없는 닫는 괄호: '와이디427피에프브이_1종종)'
            s = s[:-1]
        elif m := PAREN_NOTE_RE.search(s):
            s = s[: m.start()]
        elif m := PAREN_CLASS_RE.search(s):
            s = s[: m.start()]
            parts.append(f"({m.group(1)})")
        elif (m := CLASS_SUFFIX_RE.search(s)) and m.start() > 0:
            s = s[: m.start()]
            parts.append(m.group(1) or m.group(2))
        s = s.rstrip("_-")                   # '천안제이디씨피에프브이제2호_'
        if s == before or not s:
            s = s or before
            share = "".join(reversed(parts)) or None
            if share and share.startswith("(") and len(parts) == 1:
                share = share[1:-1]          # 괄호 하나뿐이면 괄호 없이: '하나캐피탈(보통주)' → '보통주'
            return s, share


def normalize_name(raw: str) -> tuple[str, str | None]:
    """출자 대상 이름을 정규화한다. (정규화한 이름, 주식 종류)를 돌려준다.

    순서가 중요하다: 각주 '(주3)'를 먼저 지워야 법인 표기 '(주)'와 헷갈리지 않는다.
    맨 앞의 NFKC는 전각 글자를 보통 글자로 바꾼다 ('ＰＯＳＣＯ' → 'POSCO', 'Ⅳ' → 'IV'). 같은 회사가 표기만 달라 갈라지지 않게
    공백을 지운 뒤 끝의 주식 종류를 떼므로 '큐리오시스 보통주'와 '큐리오시스보통주'가 같아진다
    """
    s = unicodedata.normalize("NFKC", raw).replace("\n", " ")
    s = FOOTNOTE_RE.sub("", s)
    s = CORP_MARK_RE.sub("", s.strip()).lstrip("*")  # '*고피자전환우선주' (표의 표시 기호)
    s = re.sub(r"\s+", "", s)
    return _strip_share_suffixes(s)


def strip_bare_preferred(name: str, index: dict[str, list[str]], overrides: dict[str, str | None]) -> str | None:
    """끝의 '우' 하나가 우선주 표시인지 확인한다. 떼고 나면 아는 회사 하나와 맞을 때만 뗀 이름을 돌려준다.

    '삼성전자우' → '삼성전자'(회사 목록에 있음). '대우'·'영우'·'좋은만남선우'처럼 원래 이름이면 그대로 두려고,
    이름 그대로는 못 찾았고 뗀 이름은 찾았을 때만 뗀다 (추측해서 합치지 않는다)
    """
    if len(name) < 3 or not name.endswith("우"):
        return None
    if resolve(name, index, overrides)[0]:
        return None
    base = name[:-1]
    return base if resolve(base, index, overrides)[0] else None


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
                    (rcept_no, row_seq, investor_corp_code, bsns_year, reprt_code, inv_prm_raw, investee_name, share_class,
                     investee_corp_code, resolve_method, invstmnt_purps, frst_acqs_de,
                     trmend_blce_qy, trmend_blce_qota_rt, trmend_blce_acntbk_amount, stlm_dt, valid_from, loaded_at)
                SELECT rcept_no, row_seq, investor_corp_code, bsns_year, reprt_code, inv_prm_raw, investee_name, share_class,
                       investee_corp_code, resolve_method, invstmnt_purps,
                       TRY_STRPTIME(NULLIF(frst_acqs_de, '-'), '%Y.%m.%d')::DATE,
                       trmend_blce_qy::BIGINT, trmend_blce_qota_rt, trmend_blce_acntbk_amount::BIGINT,
                       TRY_CAST(NULLIF(stlm_dt, '') AS DATE),
                       TRY_CAST(NULLIF(stlm_dt, '') AS DATE),  -- valid_from = 보고서 기준일 (0005)
                       now()
                FROM incoming_investment
                """
            )
            con.unregister("incoming_investment")
        closed = close_investments(con)  # valid_to·superseded_by (src/loaders/temporal.py)
        con.commit()
    except Exception:
        con.rollback()
        raise
    summary = con.execute(
        "SELECT resolve_method, count(*) FROM investment GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    return {"rows": len(df), "by_method": dict(summary), **closed}


if __name__ == "__main__":
    if sys.argv[1:] != ["rebuild"]:
        sys.exit("사용법: python -m src.loaders.investments rebuild")
    with connect() as con:
        print(rebuild_investments(con))
