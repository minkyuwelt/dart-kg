"""변화 리포트 1차 (Phase 3 PR 10b, 결정 0012): 최근 접수된 정기보고서가 최대주주·임원·출자 관계에 만든 변화를 markdown 한 장으로.

핵심 생각
    Phase 2 관계 테이블(major_shareholder·executive·investment)은 보고서마다 "기준일(stlm_dt)의 스냅샷"을 한 벌 갖고,
    닫기 단계가 같은 회사의 다음 보고서 기준일을 valid_to에 적는다(0005). 그래서 "이 보고서가 무엇을 바꿨나"는
    **이 보고서의 스냅샷(valid_from = 기준일)**과 **같은 회사의 직전 스냅샷(그보다 이른 가장 늦은 기준일)**을 키로 맞춰 비교하면 나온다.
    LLM도, 새 API 호출도 없다. 숫자가 다르면 다른 것이고, 기준값(지분율 문턱 0.1%p)은 0012에 적었다.

변화 종류 (CHANGE_KINDS)
    executive_new / executive_gone         임원 신규·퇴임 (키 person_id, 직위가 바뀐 사람은 둘 다에 나오지 않고 executive_title로)
    executive_title                        같은 사람의 직위 변경
    shareholder_new / shareholder_gone     최대주주 명단에 들어옴·빠짐 (키 주주명+주식 종류)
    shareholder_pct                        지분율 변화 (|차이| >= PCT_THRESHOLD)
    shareholder_change_event               최대주주 변동현황에 새로 보고된 변동 (변동일·원인)
    investment_new / investment_gone       출자 대상 추가·제외 (키 출자 대상 이름+주식 종류)
    investment_pct                         출자 지분율 변화
    revision                               정정 공시가 원본의 행을 대체함 (몇 행이 바뀌었나만. 값 비교는 2차)

사람 이름 옆에 생년월은 적지 않는다 (0006: 식별에만 쓰고 표시하지 않는다).

CLI (저장소 루트에서, API 호출 없음, DuckDB 읽기 전용):
    python -m src.reports.changes --days 7                      # 최근 7일 접수 보고서 → data/reports/changes_YYYY-MM-DD.md
    python -m src.reports.changes --since 2026-03-01 --until 2026-03-31 --corp 00688996
    python -m src.reports.changes --days 7 --stdout            # 파일 대신 화면에
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import duckdb

from src.loaders.duckdb_store import REPO_ROOT, connect

from src.viewer import VIEWER_URL  # noqa: F401  (text_changes가 여기서 가져간다)
PCT_THRESHOLD = 0.1  # 지분율 변화로 치는 최소 차이(%p). 0012 참고
REPORTS_DIR = REPO_ROOT / "data" / "reports"

CHANGE_KINDS = {
    "executive_new": "임원 신규", "executive_gone": "임원 퇴임", "executive_title": "임원 직위 변경",
    "shareholder_new": "최대주주 명단 추가", "shareholder_gone": "최대주주 명단 제외", "shareholder_pct": "최대주주 지분율 변화",
    "shareholder_change_event": "최대주주 변동 보고",
    "investment_new": "출자 대상 추가", "investment_gone": "출자 대상 제외", "investment_pct": "출자 지분율 변화",
    "revision": "정정 공시",
}


@dataclass
class Change:
    kind: str
    subject: str           # 사람 이름(직위) / 주주명 / 출자 대상
    before: str = ""       # 직전 값 (없으면 빈 문자열)
    after: str = ""        # 이번 값
    note: str = ""         # 변동 원인·관계 등 짧은 설명


@dataclass
class ReportChanges:
    rcept_no: str
    corp_code: str
    corp_name: str
    report_nm: str
    rcept_dt: date
    stlm_dt: date | None
    prev_stlm_dt: date | None       # 비교한 직전 스냅샷 기준일. None이면 첫 보고서(비교 대상 없음)
    changes: list[Change] = field(default_factory=list)

    @property
    def url(self) -> str:
        return VIEWER_URL.format(rcept_no=self.rcept_no)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.changes if c.kind == kind)


def _is_placeholder(name) -> bool:
    """출자 대상 이름이 '-'·'해당사항 없음'·빈 값이면 표에 내용이 없다는 자리 표시 행."""
    s = (name or "").strip()
    return s in ("", "-", "해당사항 없음", "해당없음")


def _pct(v) -> str:
    return "" if v is None else f"{float(v):.2f}%"


def reports_in_window(con: duckdb.DuckDBPyConnection, since: date, until: date, corp_codes: list[str] | None = None) -> list[dict]:
    """기간 안에 접수됐고 관계 테이블의 근거가 된(또는 정정으로 다른 행을 대체한) 보고서."""
    where = "d.rcept_dt BETWEEN ? AND ?"
    params: list = [since, until]
    if corp_codes:
        where += " AND d.corp_code IN (SELECT unnest(?::VARCHAR[]))"
        params.append(corp_codes)
    rows = con.execute(
        f"""
        WITH sources AS (
            SELECT source_rcept_no AS rcept_no FROM major_shareholder UNION
            SELECT source_rcept_no FROM executive UNION
            SELECT rcept_no FROM investment UNION
            SELECT source_rcept_no FROM major_shareholder_change UNION
            SELECT superseded_by FROM major_shareholder WHERE superseded_by IS NOT NULL UNION
            SELECT superseded_by FROM executive WHERE superseded_by IS NOT NULL UNION
            SELECT superseded_by FROM investment WHERE superseded_by IS NOT NULL
        )
        SELECT d.rcept_no, d.corp_code, d.corp_name, d.report_nm, d.rcept_dt
        FROM disclosure d JOIN sources s USING (rcept_no)
        WHERE {where}
        ORDER BY d.rcept_dt, d.corp_code, d.rcept_no
        """,
        params,
    ).fetchall()
    return [{"rcept_no": r[0], "corp_code": r[1], "corp_name": r[2], "report_nm": r[3], "rcept_dt": r[4]} for r in rows]


def _snapshot_dates(con, table: str, corp_col: str, corp_code: str, rcept_no: str, rcept_col: str) -> tuple[date | None, date | None]:
    """(이 보고서의 기준일, 같은 회사의 직전 기준일). 정정된 행(superseded_by)은 스냅샷에 넣지 않는다."""
    # 모든 행이 정정본으로 대체된 원본 보고서는 스냅샷이 없는 것으로 본다 (변화는 정정본 쪽에서 센다)
    row = con.execute(f"SELECT max(valid_from) FROM {table} WHERE {rcept_col} = ? AND superseded_by IS NULL", [rcept_no]).fetchone()
    stlm = row[0] if row else None
    if stlm is None:
        return None, None
    prev = con.execute(
        f"SELECT max(valid_from) FROM {table} WHERE {corp_col} = ? AND valid_from < ? AND superseded_by IS NULL", [corp_code, stlm]
    ).fetchone()[0]
    return stlm, prev


def _snapshot(con, table: str, corp_col: str, corp_code: str, stlm: date, cols: str) -> list[dict]:
    """그 기준일 스냅샷의 행들을 이름 있는 dict로 (SELECT의 열 이름이 그대로 키)."""
    cur = con.execute(f"SELECT {cols} FROM {table} WHERE {corp_col} = ? AND valid_from = ? AND superseded_by IS NULL", [corp_code, stlm])
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _snapshots(con, table: str, corp_col: str, rcept_col: str, corp_code: str, rcept_no: str, cols: str, key=None):
    """(이번 스냅샷, 직전 스냅샷, 직전 기준일). key가 있으면 {key(행): 행}, 없으면 행 목록. 이 보고서에 그 표가 없으면 None."""
    stlm, prev = _snapshot_dates(con, table, corp_col, corp_code, rcept_no, rcept_col)
    if stlm is None:
        return None
    now = _snapshot(con, table, corp_col, corp_code, stlm, cols)
    before = _snapshot(con, table, corp_col, corp_code, prev, cols) if prev else []
    if key is not None:
        now, before = {key(r): r for r in now}, {key(r): r for r in before}
    return now, before, prev


def _diff(now: dict, before: dict, prev: date | None, *, new, gone, changed) -> list[Change]:
    """세 표가 같이 쓰는 비교 뼈대: 이번에만 있으면 신규(직전 스냅샷이 있을 때만), 둘 다 있으면 changed(직전 값, 이번 값)가 Change를
    주면 변경, 직전에만 있으면 제외. 순서는 이번 스냅샷 행 순서 → 직전 스냅샷 행 순서."""
    out: list[Change] = []
    for key, r in now.items():
        if key not in before:
            if prev is not None:
                out.append(new(r))
        elif (c := changed(before[key], r)) is not None:
            out.append(c)
    for key, r in before.items():
        if key not in now:
            out.append(gone(r))
    return out


def _pct_change(kind: str, label: str, a, b) -> Change | None:
    """지분율이 둘 다 있고 PCT_THRESHOLD 이상 다르면 변화."""
    if a is not None and b is not None and abs(float(b) - float(a)) >= PCT_THRESHOLD:
        return Change(kind, label, before=_pct(a), after=_pct(b), note=f"{float(b) - float(a):+.2f}%p")
    return None


def executive_changes(con, corp_code: str, rcept_no: str) -> list[Change]:
    """키 person_id. 직위·등기 여부가 바뀐 사람은 신규·퇴임이 아니라 직위 변경 하나로."""
    snaps = _snapshots(con, "executive", "corp_code", "source_rcept_no", corp_code, rcept_no,
                       "person_id, nm, coalesce(ofcps, '') AS ofcps, coalesce(rgist_exctv_at, '') AS registered", key=lambda r: r["person_id"])
    if snaps is None:
        return []
    now, before, prev = snaps

    def title(r) -> str:
        return f"{r['ofcps']} {r['registered']}".strip()

    return _diff(now, before, prev,
                 new=lambda r: Change("executive_new", f"{r['nm']} ({r['ofcps']})", after=r["ofcps"], note=r["registered"]),
                 gone=lambda r: Change("executive_gone", f"{r['nm']} ({r['ofcps']})", before=r["ofcps"], note=r["registered"]),
                 changed=lambda a, b: Change("executive_title", b["nm"], before=title(a), after=title(b))
                 if (a["ofcps"], a["registered"]) != (b["ofcps"], b["registered"]) else None)


def shareholder_changes(con, corp_code: str, rcept_no: str) -> list[Change]:
    """키 (주주명, 주식 종류). 지분율은 PCT_THRESHOLD 이상 차이만."""
    snaps = _snapshots(con, "major_shareholder", "corp_code", "source_rcept_no", corp_code, rcept_no,
                       "nm, coalesce(stock_knd, '') AS stock_knd, trmend_posesn_stock_qota_rt AS pct, coalesce(relate, '') AS relate",
                       key=lambda r: (r["nm"], r["stock_knd"]))
    if snaps is None:
        return []
    now, before, prev = snaps

    def label(r) -> str:
        return f"{r['nm']} {r['stock_knd']}".strip()

    return _diff(now, before, prev,
                 new=lambda r: Change("shareholder_new", label(r), after=_pct(r["pct"]), note=r["relate"]),
                 gone=lambda r: Change("shareholder_gone", label(r), before=_pct(r["pct"]), note=r["relate"]),
                 changed=lambda a, b: _pct_change("shareholder_pct", label(b), a["pct"], b["pct"]))


def shareholder_change_events(con, corp_code: str, rcept_no: str) -> list[Change]:
    """최대주주 변동현황에서 이 보고서가 처음 보고한 변동."""
    rows = con.execute(
        """SELECT change_on, mxmm_shrholdr_nm, qota_rt, coalesce(change_cause, '') FROM major_shareholder_change
           WHERE corp_code = ? AND source_rcept_no = ? AND superseded_by IS NULL ORDER BY change_on""",
        [corp_code, rcept_no],
    ).fetchall()
    return [Change("shareholder_change_event", r[1], after=_pct(r[2]), note=f"{r[0]} {r[3]}".strip()) for r in rows]


def _sum_investments(rows: list[dict]) -> dict[tuple, dict]:
    """같은 대상이 취득 시점별로 여러 행이면 지분율을 합쳐 한 행으로. 이름이 '-'·빈 행은 "해당사항 없음" 자리 표시라 대상이 아니다."""
    out: dict[tuple, dict] = {}
    for r in rows:
        if _is_placeholder(r["name"]):
            continue
        key = (r["name"], r["share_class"])
        cur = out.setdefault(key, {"name": r["name"], "share_class": r["share_class"], "pct": None})
        if r["pct"] is not None:
            cur["pct"] = (cur["pct"] or 0.0) + float(r["pct"])
    return out


def investment_changes(con, corp_code: str, rcept_no: str) -> list[Change]:
    """키 (출자 대상 이름, 주식 종류). 지분율은 취득 시점별 행을 합친 값."""
    snaps = _snapshots(con, "investment", "investor_corp_code", "rcept_no", corp_code, rcept_no,
                       "investee_name AS name, coalesce(share_class, '') AS share_class, trmend_blce_qota_rt AS pct")
    if snaps is None:
        return []
    now_rows, before_rows, prev = snaps
    now, before = _sum_investments(now_rows), _sum_investments(before_rows)
    # 표가 통째로 없으면(자리 표시 행뿐) 비교하지 않는다 (2026-11-27): 분기보고서는 타법인 출자 표를 싣지 않는 회사가 있어,
    # 직전 사업보고서의 출자 전부가 "제외"로 잡혔다(하나증권 5,587건). 표가 없는 것은 변화가 아니라 "이 보고서에 그 표가 없음"이다
    if not now or not before:
        return []

    def label(r) -> str:
        return f"{r['name']} {r['share_class']}".strip()

    return _diff(now, before, prev,
                 new=lambda r: Change("investment_new", label(r), after=_pct(r["pct"])),
                 gone=lambda r: Change("investment_gone", label(r), before=_pct(r["pct"])),
                 changed=lambda a, b: _pct_change("investment_pct", label(b), a["pct"], b["pct"]))


def revision_changes(con, rcept_no: str) -> list[Change]:
    """이 접수번호가 정정본으로서 대체한 원본 행 수 (테이블별)."""
    out: list[Change] = []
    for table, label in [("major_shareholder", "최대주주"), ("executive", "임원"), ("investment", "출자")]:
        rows = con.execute(
            f"SELECT {'rcept_no' if table == 'investment' else 'source_rcept_no'}, count(*) FROM {table} WHERE superseded_by = ? GROUP BY 1",
            [rcept_no],
        ).fetchall()
        for original, n in rows:
            out.append(Change("revision", f"{label} 표", before=original, after=f"{n}행 대체"))
    return out


def report_changes(con: duckdb.DuckDBPyConnection, report: dict) -> ReportChanges:
    rcept_no, corp_code = report["rcept_no"], report["corp_code"]
    # 리포트에 적는 기준일·직전 기준일은 임원 표에서, 임원 표가 없는 보고서(자산운용사 등)는 최대주주 표에서
    stlm, prev = _snapshot_dates(con, "executive", "corp_code", corp_code, rcept_no, "source_rcept_no")
    if stlm is None:
        stlm, prev = _snapshot_dates(con, "major_shareholder", "corp_code", corp_code, rcept_no, "source_rcept_no")
    changes = executive_changes(con, corp_code, rcept_no) + shareholder_changes(con, corp_code, rcept_no) \
        + shareholder_change_events(con, corp_code, rcept_no) + investment_changes(con, corp_code, rcept_no) + revision_changes(con, rcept_no)
    return ReportChanges(rcept_no, corp_code, report["corp_name"], report["report_nm"], report["rcept_dt"], stlm, prev, changes)


def build_report(con: duckdb.DuckDBPyConnection, since: date, until: date, corp_codes: list[str] | None = None) -> list[ReportChanges]:
    return [report_changes(con, r) for r in reports_in_window(con, since, until, corp_codes)]


def summarize(results: list[ReportChanges]) -> dict:
    """Dagster 메타데이터·CLI 요약용: 보고서 수, 변화 있는 보고서 수, 종류별 건수."""
    by_kind = {k: 0 for k in CHANGE_KINDS}
    for r in results:
        for c in r.changes:
            by_kind[c.kind] += 1
    return {"reports": len(results), "reports_with_changes": sum(1 for r in results if r.changes),
            "changes": sum(by_kind.values()), "by_kind": {k: v for k, v in by_kind.items() if v}}


def render_markdown(results: list[ReportChanges], since: date, until: date) -> str:
    lines = [f"# 변화 리포트 {since} ~ {until}", ""]
    s = summarize(results)
    lines.append(f"접수된 관계 보고서 {s['reports']}개, 변화가 있는 보고서 {s['reports_with_changes']}개, 변화 {s['changes']}건.")
    if s["by_kind"]:
        lines.append(" · ".join(f"{CHANGE_KINDS[k]} {v}" for k, v in s["by_kind"].items()))
    lines.append("")
    lines.append(f"기준: 같은 회사의 직전 보고서 스냅샷과 비교. 지분율은 {PCT_THRESHOLD}%p 이상 차이만. 숫자는 공시 그대로이며 해석은 넣지 않았다 (0012).")
    lines.append("")
    if not results:
        lines.append("이 기간에 관계 테이블의 근거가 된 보고서가 없습니다.")
        return "\n".join(lines) + "\n"
    for r in results:
        lines.append(f"## {r.corp_name} — {r.report_nm}")
        basis = f"기준일 {r.stlm_dt}" + (f", 직전 {r.prev_stlm_dt}과 비교" if r.prev_stlm_dt else ", 첫 보고서라 비교 대상 없음")
        lines.append(f"접수 {r.rcept_dt} · {basis} · [공시 원문]({r.url})")
        lines.append("")
        if not r.changes:
            lines.append("변화 없음.")
            lines.append("")
            continue
        for kind, label in CHANGE_KINDS.items():
            rows = [c for c in r.changes if c.kind == kind]
            if not rows:
                continue
            lines.append(f"**{label}** ({len(rows)})")
            lines.append("")
            lines.append("| 대상 | 이전 | 이번 | 비고 |")
            lines.append("|---|---|---|---|")
            for c in rows:
                lines.append(f"| {c.subject} | {c.before} | {c.after} | {c.note} |")
            lines.append("")
    return "\n".join(lines) + "\n"


def write_report(text: str, until: date, out_dir: Path = REPORTS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"changes_{until.isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="최근 정기보고서가 만든 관계 변화 → markdown")
    ap.add_argument("--days", type=int, default=7, help="오늘까지 며칠 (기본 7). --since가 있으면 무시")
    ap.add_argument("--since", type=date.fromisoformat)
    ap.add_argument("--until", type=date.fromisoformat, default=date.today())
    ap.add_argument("--corp", nargs="*", help="corp_code로 좁히기")
    ap.add_argument("--stdout", action="store_true", help="파일 대신 화면에 출력")
    args = ap.parse_args(argv)
    since = args.since or (args.until - timedelta(days=args.days))
    with connect(read_only=True) as con:
        results = build_report(con, since, args.until, args.corp)
    text = render_markdown(results, since, args.until)
    if args.stdout:
        print(text)
    else:
        path = write_report(text, args.until)
        s = summarize(results)
        print(f"{path}  보고서 {s['reports']}개, 변화 {s['changes']}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
