"""변화 리포트 2차 — 문장 변화 (결정 0012 결정 4, usecases 질문 8 "작년과 올해 위험요인·회계정책 문장이 어떻게 달라졌나").

핵심 생각
    1차(changes.py)는 관계 테이블의 숫자 변화를 본다. 2차는 보고서 **글**의 변화를 본다: 이번 정기보고서와 같은 회사의
    **전년 같은 종류 보고서**(사업보고서 ↔ 전년 사업보고서, 반기 ↔ 전년 반기)에서 같은 절(document_section.path)을 짝지어
    문장을 비교한다. LLM도, 새 API 호출도 없다. 결과는 저장하지 않고 질의할 때 계산한다(1차와 같은 원칙 — 원문이 정정되면 저절로 따라온다).

무엇을 변화로 치나
    - 절(section)은 경로(path)가 같은 것끼리 짝짓는다. 한쪽에만 있는 절은 양쪽에 다 있는 가장 가까운 상위 절로 올려 묶어 견준다
      (해마다 절이 나뉘는 굵기가 다르다, _pair_sections). 그래도 짝이 없으면 "절 추가/삭제"로 수만 센다.
    - 문장은 줄바꿈과 한글 뒤 마침표에서 자른다(뒤에 공백이 없어도). 표(tables)는 보지 않는다(표의 숫자 변화는 1차와 정형 표의 영역).
    - 순서는 보지 않는다: 같은 문장을 먼저 빼고, 남은 것끼리 닮은 정도가 PAIR_MIN_RATIO 이상일 때만 "바뀜" 짝으로 묶는다.
    - **숫자만 바뀐 문장과 글이 바뀐 문장을 가른다.** 숫자·날짜·금액을 '#'로 가린 꼴(_mask)이 같으면 "숫자만 바뀜"으로 수만 세고,
      가린 꼴이 다르면 글이 바뀐 것이다. 사업보고서는 해마다 "제N기", "2025년 12월 31일 현재", 금액이 바뀌는 문장이 수백 개라
      이걸 가르지 않으면 리포트가 숫자 갱신에 묻힌다. 글이 바뀐 문장만 전후 문장으로 보여 준다.
    - 어느 절을 비교하나: 기본은 경로에 SECTION_WORDS_DEFAULT(위험·회계정책·경영진단) 가운데 하나가 든 절. 보고서 전체를 비교하면
      회사 개요·사업 내용의 서술도 들어오는데 그건 `--section` 으로 넓힌다. 질문 8이 묻는 것은 위험요인과 회계정책이다.
    - 본문 파일({rcept_no}.xml)만 본다. 감사보고서 첨부는 회계법인 글이라 회사의 서술 변화가 아니다.

해석은 넣지 않는다. "위험이 커졌다"는 판단은 사람(또는 답할 때의 LLM)이 하고, 여기서는 어느 문장이 어떻게 바뀌었는지만 적는다.

CLI (저장소 루트, API 호출 없음, DuckDB 읽기 전용):
    python -m src.reports.text_changes --rcept 20260313000002                 # 이 보고서 ↔ 전년 같은 종류 보고서
    python -m src.reports.text_changes --days 7                               # 최근 7일 접수 정기보고서 전부 → data/reports/text_changes_YYYY-MM-DD.md
    python -m src.reports.text_changes --rcept 2026… --section 연혁 --section 위험   # 비교할 절 낱말 바꾸기
    python -m src.reports.text_changes --rcept 2026… --stdout
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from src.loaders.duckdb_store import REPO_ROOT, connect
from src.loaders.periodic_reports import periodic_reports_of
from src.reports.changes import VIEWER_URL, REPORTS_DIR

SECTION_WORDS_DEFAULT = ("위험", "회계정책", "경영진단")
MIN_SENTENCE_CHARS = 8          # 이보다 짧은 토막("가.", "(단위: 백만원)")은 문장으로 치지 않는다
SENTENCES_MAX = 20              # markdown에서 절마다 보여 주는 문장 변화 상한. 데이터(SectionDiff)에는 전부 남긴다

# 숫자·날짜·금액을 가린다: '2025년 12월 31일', '1,234백만원', '제17기', '12.5%', '(주)' 안의 숫자 등.
# 한글 뒤에 붙은 '제N기'·'N분기'도 숫자 자리로 보고, 남은 공백은 하나로 접는다.
_NUM_RE = re.compile(r"\d[\d,.]*")
_WS_RE = re.compile(r"\s+")
# 문장 끝: 한글(또는 닫는 괄호) 뒤의 마침표·물음표·느낌표. 뒤에 공백이 없어도 자른다 — 원문에 "…없습니다.- 기업회계기준서…"처럼
# 마침표 뒤에 바로 다음 항목이 붙는 곳이 많다(KB금융 2025 주석). 숫자 뒤 마침표(12.31, 1.5%)는 문장 끝이 아니다.
_SPLIT_RE = re.compile(r"(?<=[가-힣)\]][.!?])\s*|\n+")
PAIR_MIN_RATIO = 0.7            # 전년·이번 문장을 '바뀜' 짝으로 묶는 최소 닮음(짧은 쪽 글자의 몇 %가 긴 쪽에 같은 순서로 있나, 가린 꼴). 낮으면 삭제·추가
PAIR_MAX_WORK = 400_000         # 짝 찾기 비용 상한(남은 전년 문장 수 × 이번 문장 수). 넘으면 짝짓기를 건너뛰고 삭제·추가로만 적는다


def mask(sentence: str) -> str:
    """숫자를 '#'로 바꾸고 띄어쓰기를 모두 뺀 꼴. 두 문장의 mask가 같으면 숫자·띄어쓰기만 바뀐 것 ("영향은 없습니다" ↔ "영향은없습니다"는 같은 문장)."""
    return _WS_RE.sub("", _NUM_RE.sub("#", sentence))


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for raw in _SPLIT_RE.split(text or ""):
        s = _WS_RE.sub(" ", raw).strip()
        if len(s) >= MIN_SENTENCE_CHARS:
            out.append(s)
    return out


@dataclass
class SectionDiff:
    path: str
    status: str                         # 'changed' | 'numbers_only' | 'same' | 'added' | 'removed'
    removed: list[str] = field(default_factory=list)          # 전년에만 있던 문장 (글이 바뀐 것)
    added: list[str] = field(default_factory=list)            # 이번에만 있는 문장 (글이 바뀐 것)
    replaced: list[tuple[str, str]] = field(default_factory=list)   # (전년 문장, 이번 문장) 자리가 같은 짝
    numbers_only: int = 0               # 숫자·띄어쓰기만 바뀐 문장 수
    sentences_before: int = 0
    sentences_after: int = 0
    parts_before: int = 0               # 이 묶음에 들어간 전년 절 수 (1이면 경로 그대로 짝, 여럿이면 상위 절로 묶은 것)
    parts_after: int = 0

    @property
    def merged(self) -> bool:
        return self.parts_before > 1 or self.parts_after > 1

    @property
    def text_changes(self) -> int:
        return len(self.removed) + len(self.added) + len(self.replaced)


@dataclass
class TextChanges:
    rcept_no: str
    corp_code: str
    corp_name: str
    report_nm: str
    rcept_dt: date
    prev_rcept_no: str | None           # 전년 같은 종류 보고서 (없으면 None → 비교 없음)
    prev_report_nm: str | None
    section_words: tuple[str, ...]
    sections: list[SectionDiff] = field(default_factory=list)

    @property
    def url(self) -> str:
        return VIEWER_URL.format(rcept_no=self.rcept_no)

    @property
    def prev_url(self) -> str | None:
        return VIEWER_URL.format(rcept_no=self.prev_rcept_no) if self.prev_rcept_no else None

    @property
    def changed_sections(self) -> list[SectionDiff]:
        return [s for s in self.sections if s.status == "changed"]


def diff_sentences(before: list[str], after: list[str]) -> SectionDiff:
    """두 절의 문장 목록을 비교한다. 순서는 보지 않는다 — 절이 해마다 다르게 나뉘고 항목 순서가 바뀌는 일이 흔해서,
    순서대로 맞추면(difflib 옵코드) 엉뚱한 문장끼리 '바뀜' 짝이 된다(KB금융 2024 주석 1절 ↔ 2025 40절, 2026-11-27 노트북).

    1) 가린 꼴(mask)이 같은 문장은 양쪽에서 뺀다. 원문이 다르면 숫자만 바뀐 것으로 센다.
    2) 남은 전년 문장마다 남은 이번 문장 중 가장 닮은 것(짧은 쪽 글자의 PAIR_MIN_RATIO 이상이 긴 쪽에 같은 순서로 있음)을 '바뀜' 짝으로 묶는다.
    3) 짝이 없는 것은 삭제·추가.
    """
    d = SectionDiff(path="", status="same", sentences_before=len(before), sentences_after=len(after))
    by_mask_after: dict[str, list[str]] = {}
    for a in after:
        by_mask_after.setdefault(mask(a), []).append(a)
    rest_before: list[tuple[str, str]] = []
    for b in before:
        mb = mask(b)
        pool = by_mask_after.get(mb)
        if pool:
            a = pool.pop()
            if a != b:
                d.numbers_only += 1
        else:
            rest_before.append((b, mb))
    rest_after = [(a, m) for m, pool in by_mask_after.items() for a in pool]
    if rest_before and rest_after and len(rest_before) * len(rest_after) <= PAIR_MAX_WORK:
        for b, mb in rest_before:
            best, best_i = 0.0, -1
            sm = difflib.SequenceMatcher(autojunk=False)
            sm.set_seq2(mb)
            for i, (_, ma) in enumerate(rest_after):
                if ma is None:
                    continue
                sm.set_seq1(ma)
                shorter = min(len(ma), len(mb)) or 1
                # ratio()는 2M/(la+lb)라 짧은 문장이 길게 늘어난 경우("충분합니다." → "충분하며 외화 조달도 늘렸습니다.")에 낮게 나온다.
                # 그래서 '짧은 쪽이 얼마나 들어 있나'(M/shorter)로 잰다. 앞의 두 빠른 상한으로 가망 없는 쌍을 먼저 거른다
                if sm.real_quick_ratio() * (len(ma) + len(mb)) / 2 / shorter < PAIR_MIN_RATIO:
                    continue
                if sm.quick_ratio() * (len(ma) + len(mb)) / 2 / shorter < PAIR_MIN_RATIO:
                    continue
                matched = sum(bl.size for bl in sm.get_matching_blocks())
                r = matched / shorter
                if r > best:
                    best, best_i = r, i
            if best >= PAIR_MIN_RATIO:
                d.replaced.append((b, rest_after[best_i][0]))
                rest_after[best_i] = (rest_after[best_i][0], None)
            else:
                d.removed.append(b)
        d.added += [a for a, m in rest_after if m is not None]
    else:
        d.removed += [b for b, _ in rest_before]
        d.added += [a for a, _ in rest_after]
    # 원문 순서대로 보여 준다
    order_a = {a: i for i, a in enumerate(after)}
    d.added.sort(key=lambda a: order_a.get(a, 0))
    if d.text_changes:
        d.status = "changed"
    elif d.numbers_only:
        d.status = "numbers_only"
    return d


def _sections(con: duckdb.DuckDBPyConnection, rcept_no: str) -> list[tuple[str, str]]:
    """본문 파일의 글 절을 (경로, 본문) 순서대로. 절 낱말로 거르지 않는다 — 거르기는 묶은 뒤(_pair_sections)에 한다."""
    return con.execute(
        "SELECT path, text FROM document_section WHERE rcept_no = ? AND file_name = ? AND char_count > 0 ORDER BY section_id",
        [rcept_no, f"{rcept_no}.xml"],
    ).fetchall()


def _ancestors(path: str) -> list[str]:
    """'A > B > C' → ['A > B > C', 'A > B', 'A'] (자기 자신부터 위로)."""
    parts = path.split(" > ")
    return [" > ".join(parts[:i]) for i in range(len(parts), 0, -1)]


def _pair_sections(cur: list[tuple[str, str]], old: list[tuple[str, str]], words: tuple[str, ...]) -> list[tuple[str, list[tuple[str, str]], list[tuple[str, str]]]]:
    """두 보고서의 절을 견줄 단위로 묶는다 → [(묶음 경로, 전년 절들, 이번 절들)].

    경로가 양쪽에 다 있으면 그대로 짝이다. 한쪽에만 있으면 **양쪽에 다 있는(절이거나 절의 상위인) 가장 가까운 상위 경로**로 올려 묶는다.
    해마다 같은 글이 다른 굵기로 나뉘기 때문이다 — KB금융 2024년 보고서는 'IV. 이사의 경영진단 및 분석의견'이 절 하나(406문장)인데
    2025년은 그 아래 소절 6개로 파싱됐다(2026-11-27 노트북). 글자 그대로 맞추면 전부 '추가·삭제'가 되어 비교가 안 된다.
    묶은 뒤 절 낱말(words)로 거른다: 묶음 안의 어느 절 경로에라도 낱말이 들면 그 묶음을 본다.
    """
    cur_paths, old_paths = {p for p, _ in cur}, {p for p, _ in old}
    cur_pre = {a for p in cur_paths for a in _ancestors(p)}
    old_pre = {a for p in old_paths for a in _ancestors(p)}
    common = cur_pre & old_pre

    def key(path: str) -> str:
        return next((a for a in _ancestors(path) if a in common), path)

    groups: dict[str, tuple[list, list]] = {}
    for side, rows in ((0, old), (1, cur)):
        for path, text in rows:
            groups.setdefault(key(path), ([], []))[side].append((path, text))
    out = []
    for k, (o, c) in groups.items():
        if words and not any(any(w in p for w in words) for p, _ in o + c):
            continue
        out.append((k, o, c))
    return out


def previous_report(con: duckdb.DuckDBPyConnection, corp_code: str, rcept_no: str) -> dict | None:
    """같은 회사·같은 보고서 종류(reprt_code)·전년(bsns_year - 1)의 보고서. 정정본이 여럿이면 가장 늦은 접수(최신 정정본)."""
    reports = periodic_reports_of(con, corp_code)
    me = next((r for r in reports if r["rcept_no"] == rcept_no), None)
    if me is None:
        return None
    want = (str(int(me["bsns_year"]) - 1), me["reprt_code"])
    prev = [r for r in reports if (r["bsns_year"], r["reprt_code"]) == want and r["rcept_dt"] < me["rcept_dt"]]
    return prev[-1] if prev else None


def report_text_changes(con: duckdb.DuckDBPyConnection, rcept_no: str, section_words: tuple[str, ...] = SECTION_WORDS_DEFAULT,
                        prev_rcept_no: str | None = None) -> TextChanges | None:
    """한 보고서의 문장 변화. 공시 목록에 없으면 None. prev_rcept_no를 주면 그 보고서와 비교한다(정정본 ↔ 원본 등)."""
    row = con.execute("SELECT corp_code, corp_name, report_nm, rcept_dt FROM disclosure WHERE rcept_no = ?", [rcept_no]).fetchone()
    if row is None:
        return None
    corp_code, corp_name, report_nm, rcept_dt = row
    if prev_rcept_no:
        prev_nm = con.execute("SELECT report_nm FROM disclosure WHERE rcept_no = ?", [prev_rcept_no]).fetchone()
        prev = {"rcept_no": prev_rcept_no, "report_nm": prev_nm[0] if prev_nm else None}
    else:
        prev = previous_report(con, corp_code, rcept_no)
    tc = TextChanges(rcept_no, corp_code, corp_name, report_nm, rcept_dt, prev["rcept_no"] if prev else None,
                     prev["report_nm"] if prev else None, tuple(section_words))
    if prev is None:
        return tc
    pairs = _pair_sections(_sections(con, rcept_no), _sections(con, prev["rcept_no"]), tc.section_words)
    for path, o, c in sorted(pairs, key=lambda x: (not x[2], x[0])):
        before = split_sentences("\n".join(t for _, t in o))
        after = split_sentences("\n".join(t for _, t in c))
        if o and c:
            d = diff_sentences(before, after)
            d.path = path
        elif c:
            d = SectionDiff(c[0][0] if len(c) == 1 else path, "added", sentences_after=len(after))
        else:
            d = SectionDiff(o[0][0] if len(o) == 1 else path, "removed", sentences_before=len(before))
        d.parts_before, d.parts_after = len(o), len(c)
        tc.sections.append(d)
    return tc


def periodic_reports_in_window(con: duckdb.DuckDBPyConnection, since: date, until: date, corp_codes: list[str] | None = None) -> list[str]:
    """기간 안에 접수된 정기보고서(본문이 적재된 것)의 접수번호. 정정본도 각각."""
    where = "d.rcept_dt BETWEEN ? AND ? AND d.report_nm LIKE '%보고서 (%'"
    params: list = [since, until]
    if corp_codes:
        where += " AND d.corp_code IN (SELECT unnest(?::VARCHAR[]))"
        params.append(corp_codes)
    rows = con.execute(
        f"""SELECT d.rcept_no FROM disclosure d
            WHERE {where} AND EXISTS (SELECT 1 FROM document_section s WHERE s.rcept_no = d.rcept_no)
            ORDER BY d.rcept_dt, d.corp_code, d.rcept_no""", params).fetchall()
    return [r[0] for r in rows]


def build_text_report(con: duckdb.DuckDBPyConnection, since: date, until: date, corp_codes: list[str] | None = None,
                      section_words: tuple[str, ...] = SECTION_WORDS_DEFAULT) -> list[TextChanges]:
    out = []
    for rcept_no in periodic_reports_in_window(con, since, until, corp_codes):
        tc = report_text_changes(con, rcept_no, section_words)
        if tc is not None:
            out.append(tc)
    return out


def summarize(results: list[TextChanges]) -> dict:
    """요약: 보고서 수, 비교 대상이 있는 보고서 수, 글이 바뀐 절 수, 글 변화 문장 수, 숫자만 바뀐 문장 수."""
    compared = [r for r in results if r.prev_rcept_no]
    return {
        "reports": len(results),
        "compared": len(compared),
        "sections_changed": sum(len(r.changed_sections) for r in compared),
        "text_changes": sum(s.text_changes for r in compared for s in r.sections),
        "numbers_only": sum(s.numbers_only for r in compared for s in r.sections),
    }


def _clip(s: str, n: int = 160) -> str:
    s = s.replace("|", "／")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_markdown(results: list[TextChanges], since: date | None = None, until: date | None = None) -> str:
    title = f"# 문장 변화 리포트 {since} ~ {until}" if since else "# 문장 변화 리포트"
    lines = [title, ""]
    s = summarize(results)
    lines.append(f"정기보고서 {s['reports']}개, 전년 같은 보고서와 비교한 것 {s['compared']}개. 글이 바뀐 절 {s['sections_changed']}개, "
                 f"바뀐 문장 {s['text_changes']}개 (숫자·띄어쓰기만 바뀐 문장 {s['numbers_only']}개는 세기만 함).")
    words = results[0].section_words if results else SECTION_WORDS_DEFAULT
    lines.append(f"기준: 경로에 {' · '.join(words)} 가운데 하나가 든 절. 같은 경로의 절끼리 문장을 비교하고, 숫자·날짜·띄어쓰기만 바뀐 문장은 글 변화로 치지 않는다. "
                 "표는 보지 않는다. 해석은 넣지 않았다 (0012 결정 4).")
    lines.append("")
    if not results:
        lines.append("이 기간에 본문이 적재된 정기보고서가 없습니다.")
        return "\n".join(lines) + "\n"
    for r in results:
        lines.append(f"## {r.corp_name} — {r.report_nm}")
        if not r.prev_rcept_no:
            lines.append(f"접수 {r.rcept_dt} · [공시 원문]({r.url}) · 전년 같은 종류의 보고서가 없어 비교하지 않음")
            lines.append("")
            continue
        lines.append(f"접수 {r.rcept_dt} · [이번]({r.url}) ↔ [전년 {r.prev_report_nm}]({r.prev_url})")
        lines.append("")
        changed = r.changed_sections
        quiet = [x for x in r.sections if x.status in ("numbers_only", "same")]
        extra = [x for x in r.sections if x.status in ("added", "removed")]
        if not changed and not extra:
            lines.append(f"글이 바뀐 절 없음 (비교한 절 {len(quiet)}개, 숫자·띄어쓰기만 바뀐 문장 {sum(x.numbers_only for x in quiet)}개).")
            lines.append("")
            continue
        for d in changed:
            merged = f", 전년 {d.parts_before}절 ↔ 이번 {d.parts_after}절을 묶어 견줌" if d.merged else ""
            lines.append(f"**{d.path}** — 바뀐 문장 {d.text_changes}개, 숫자·띄어쓰기만 {d.numbers_only}개 (문장 {d.sentences_before} → {d.sentences_after}{merged})")
            lines.append("")
            lines.append("| 구분 | 전년 | 이번 |")
            lines.append("|---|---|---|")
            shown = 0
            for b, a in d.replaced:
                if shown >= SENTENCES_MAX:
                    break
                lines.append(f"| 바뀜 | {_clip(b)} | {_clip(a)} |")
                shown += 1
            for b in d.removed:
                if shown >= SENTENCES_MAX:
                    break
                lines.append(f"| 삭제 | {_clip(b)} |  |")
                shown += 1
            for a in d.added:
                if shown >= SENTENCES_MAX:
                    break
                lines.append(f"| 추가 |  | {_clip(a)} |")
                shown += 1
            if d.text_changes > shown:
                lines.append(f"| … | 나머지 {d.text_changes - shown}개 | |")
            lines.append("")
        for d in extra:
            lines.append(f"- 절 {'추가' if d.status == 'added' else '삭제'}: {d.path} (문장 {d.sentences_after or d.sentences_before}개)")
        if extra:
            lines.append("")
        if quiet:
            lines.append(f"글이 바뀌지 않은 절 {len(quiet)}개 (숫자·띄어쓰기만 바뀐 문장 {sum(x.numbers_only for x in quiet)}개).")
            lines.append("")
    return "\n".join(lines) + "\n"


def write_report(text: str, until: date, out_dir: Path = REPORTS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"text_changes_{until.isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    return path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="문장 변화 리포트 (0012 결정 4). API 호출 없음, DuckDB 읽기 전용")
    p.add_argument("--rcept", help="이 보고서 하나만 (전년 같은 종류 보고서와 비교)")
    p.add_argument("--prev", help="--rcept와 비교할 보고서를 직접 지정 (정정본 ↔ 원본 등)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--corp", action="append")
    p.add_argument("--section", action="append", help=f"비교할 절 경로의 낱말 (여러 번). 기본 {' '.join(SECTION_WORDS_DEFAULT)}")
    p.add_argument("--stdout", action="store_true")
    a = p.parse_args(argv)
    words = tuple(a.section) if a.section else SECTION_WORDS_DEFAULT
    until = datetime.strptime(a.until, "%Y-%m-%d").date() if a.until else date.today()
    since = datetime.strptime(a.since, "%Y-%m-%d").date() if a.since else until - timedelta(days=a.days)
    with connect(read_only=True) as con:
        if a.rcept:
            tc = report_text_changes(con, a.rcept, words, a.prev)
            if tc is None:
                print(f"공시 목록에 {a.rcept}가 없습니다", file=sys.stderr)
                return 1
            results, since, until = [tc], None, None
        else:
            results = build_text_report(con, since, until, a.corp, words)
    text = render_markdown(results, since, until)
    if a.stdout or a.rcept:
        print(text)
    else:
        path = write_report(text, until)
        print(f"{path} — {summarize(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
