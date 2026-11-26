"""같은 이름의 회사가 여럿이라 연결하지 못한(ambiguous) 출자 대상의 후보를 확인하는 도구.

출자 표의 이름을 corp 테이블에서 찾았을 때 후보가 2곳 이상이면 코드는 추측하지 않고 비워 둔다
(`src/loaders/investments.py`의 resolve). 그런 이름이 2026-11-26 기준 193개였다.
이 모듈은 그중 **사람이 근거를 보고 override로 적을 만한 이름**을 골라, 후보마다 최근 공시 수를 조회해 보여 준다.

고르는 기준 (`pick_candidates`):
- 후보 중 상장코드(stock_code)가 있는 회사가 **정확히 하나**인 이름만 본다. 출자 표의 이런 이름은 대개 상장사
  지분(단순투자)이라 상장사가 맞을 가능성이 높지만, 그래도 코드가 자동으로 고르지는 않는다.
  - 상장코드는 상장폐지 뒤에도 남을 수 있다 (예: 에피밸리 068630, 위다스 056810은 modify_date가 2017-06-30이고
    비상장 후보가 더 최근까지 살아 있다). 그래서 "상장코드 하나면 고른다"는 규칙은 틀릴 수 있다.
  - 대신 후보마다 list.json으로 최근 공시 수를 조회해(API 1건씩) 사람이 보고 override CSV에 적는다.
- 상장코드가 둘 이상인 이름(SK, 삼성물산, 미래에셋증권, 원텍)은 옛 코드가 합병으로 사라졌는지 봐야 하므로
  후보 전부를 조회해 결과만 찍는다. 적는 것은 사람이 한다.
- 후보가 모두 비상장인 이름(약 125개)은 이름만으로 고를 근거가 없어 다루지 않는다.

사용 (노트북, API 호출):
    python -m src.loaders.ambiguous_names check            # 최근 3개월 공시 수 조회 후 출력
    python -m src.loaders.ambiguous_names check --dry-run  # 호출 없이 어떤 이름을 볼지만 출력
출력의 'OK' 줄은 "상장 후보에 공시가 있고 나머지 후보는 0건"인 이름이고, 그 줄의 CSV 꼴을 그대로
config/corp_name_overrides.csv에 붙일 수 있다. 'CHECK' 줄은 사람이 더 봐야 하는 이름이다.
"""

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb

from src.collectors.corp_code import get_api_key
from src.collectors.disclosure_list import fetch_disclosures, parse_disclosures
from src.loaders.duckdb_store import connect
from src.loaders.investments import build_name_index, load_overrides, to_hangul_letters

RECENT_DAYS = 90  # "최근 공시"로 보는 기간. 기존 override 근거(최근 3개월)와 같게 둔다


@dataclass
class Candidate:
    corp_code: str
    corp_name: str
    stock_code: str | None
    modify_date: str | None
    recent_count: int | None = None  # list.json 조회 전에는 None


@dataclass
class AmbiguousName:
    name: str            # 정규화한 출자 대상 이름
    rows: int            # investment 행 수
    investors: int       # 들고 있는 출자회사 수
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def listed(self) -> list[Candidate]:
        return [c for c in self.candidates if c.stock_code]


def ambiguous_names(con: duckdb.DuckDBPyConnection) -> list[AmbiguousName]:
    """investment에서 resolve_method='ambiguous'인 이름과 corp 후보를 모은다. 영문 이름은 한글 읽기로도 찾는다."""
    index = build_name_index(con)
    corp = {
        code: Candidate(code, cname, scode or None, str(mdate) if mdate else None)
        for code, cname, scode, mdate in con.execute("SELECT corp_code, corp_name, stock_code, modify_date FROM corp").fetchall()
    }
    rows = con.execute(
        """
        SELECT investee_name, count(*), count(DISTINCT investor_corp_code)
        FROM investment WHERE resolve_method = 'ambiguous'
        GROUP BY 1 ORDER BY 2 DESC, 1
        """
    ).fetchall()
    out = []
    for name, n, investors in rows:
        codes = index.get(name) or index.get(to_hangul_letters(name)) or []
        out.append(AmbiguousName(name, n, investors, [corp[c] for c in codes]))
    return out


def pick_candidates(names: list[AmbiguousName]) -> tuple[list[AmbiguousName], list[AmbiguousName]]:
    """(상장 후보가 정확히 하나인 이름, 상장 후보가 둘 이상인 이름)을 돌려준다. 모두 비상장인 이름은 뺀다."""
    one = [a for a in names if len(a.listed) == 1]
    many = [a for a in names if len(a.listed) >= 2]
    return one, many


def judge(a: AmbiguousName) -> tuple[str, Candidate | None]:
    """조회 결과로 판정한다. 'OK'는 상장 후보에만 최근 공시가 있을 때. 그 밖은 'CHECK'."""
    if len(a.listed) != 1 or any(c.recent_count is None for c in a.candidates):
        return "CHECK", None
    listed = a.listed[0]
    others = [c for c in a.candidates if c is not listed]
    if listed.recent_count and all(c.recent_count == 0 for c in others):
        return "OK", listed
    return "CHECK", None


def override_line(a: AmbiguousName, chosen: Candidate, bgn: str, end: str) -> str:
    """corp_name_overrides.csv에 붙일 한 줄. 근거 문장에 후보 수·공시 수·나머지 후보의 상태를 적는다."""
    others = "·".join(
        f"{c.corp_code}은 0건(modify_date {c.modify_date})" for c in a.candidates if c is not chosen
    )
    reason = (
        f"동명 {len(a.candidates)}곳. 상장코드 {chosen.stock_code}. "
        f"최근 3개월({bgn[:4]}-{bgn[4:6]}-{bgn[6:]}~{end[:4]}-{end[4:6]}-{end[6:]}) 공시 {chosen.recent_count}건. {others}"
    )
    return f"{a.name},{chosen.corp_code},{reason}"


def count_recent(api_key: str, corp_code: str, bgn: str, end: str) -> int:
    """회사 하나의 기간 내 공시 수. dart_client를 거치며 결과는 저장하지 않는다 (확인용)."""
    return len(parse_disclosures(fetch_disclosures(api_key, corp_code, bgn, end)))


def run_check(dry_run: bool) -> None:
    with connect(read_only=True) as con:
        names = ambiguous_names(con)
    overrides = load_overrides()
    names = [a for a in names if a.name not in overrides]
    one, many = pick_candidates(names)
    calls = sum(len(a.candidates) for a in one + many)
    print(f"ambiguous 이름 {len(names)}개 → 상장 후보 하나 {len(one)}개, 둘 이상 {len(many)}개, API 호출 예정 {calls}건")
    if dry_run:
        for a in one + many:
            print(f"  {a.name}: " + ", ".join(f"{c.corp_code}({c.stock_code or '비상장'})" for c in a.candidates))
        return

    api_key = get_api_key()
    end = date.today()
    bgn = end - timedelta(days=RECENT_DAYS)
    bgn_s, end_s = bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    for a in one + many:
        for c in a.candidates:
            c.recent_count = count_recent(api_key, c.corp_code, bgn_s, end_s)

    print(f"\n## OK (override CSV에 그대로 붙일 수 있음) — 기간 {bgn_s}~{end_s}")
    ok = 0
    for a in one:
        verdict, chosen = judge(a)
        if verdict == "OK":
            ok += 1
            print(override_line(a, chosen, bgn_s, end_s))
    print(f"\n## CHECK (사람이 더 봐야 함) — OK {ok}개")
    for a in one + many:
        if judge(a)[0] == "CHECK":
            print(f"\n{a.name}  행 {a.rows}, 출자회사 {a.investors}곳")
            for c in a.candidates:
                print(f"    {c.corp_code} {c.stock_code or '비상장'} modify {c.modify_date} 최근공시 {c.recent_count}건")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] != "check" or any(x != "--dry-run" for x in args[1:]):
        sys.exit("사용법: python -m src.loaders.ambiguous_names check [--dry-run]")
    run_check(dry_run="--dry-run" in args)
