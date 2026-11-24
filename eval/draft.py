"""질문 후보 초안 만들기. 접수번호와 근거 섹션 경로는 데이터에서 채우고, **정답은 비워 둔다** (사람이 DART 뷰어에서 확인해 적는다).

    python -m eval.draft                       # 대상 회사의 최신 사업보고서로 후보 생성 → eval/questions.draft.jsonl
    python -m eval.draft --seed                # Phase 2 질문 4개(접수번호 해결)도 함께
    python -m eval.draft --corps 00688996 --max 20

어떻게 만드나
- 회사마다 가장 최신 사업보고서(정정본이면 최신 정정본)와 그 전 연도 사업보고서를 disclosure·document에서 고른다
- 본문 파일의 섹션 제목이 틀(TEMPLATES)의 정규식에 맞고 내용이 있으면, 그 회사·연도로 질문 문장을 채운다
- 지주 4곳은 틀 전부, 자회사는 회사마다 틀 2개(회사 코드로 정해지는 고정 선택)만 — 후보가 수백 개로 불어나지 않게
- 전 연도 보고서에는 "시점" 유형만 (같은 질문을 다른 연도로 물어 시점 구분을 잰다)
- "없음" 유형은 데이터에서 만들 수 없으므로 questions.jsonl에 손으로 적는다 (eval/README.md)

초안은 status "draft"라 실행에서 빠진다. 사람이 뷰어에서 정답을 확인해 answer를 채우고 status를 ready로 바꾼 뒤
questions.jsonl로 옮긴다. 정답을 확인할 수 없거나 질문이 어색하면 지운다.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from eval.questions import Question
from src.collectors.corp_code import HOLDING_CORPS
from src.loaders.duckdb_store import REPO_ROOT, connect

DEFAULT_OUT = REPO_ROOT / "eval" / "questions.draft.jsonl"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
SUBSIDIARY_TEMPLATES = 2


@dataclass(frozen=True)
class Template:
    key: str
    type: str
    title_re: str       # 섹션 제목 정규식 (본문 파일에서 찾음)
    question: str       # {corp} {year} 자리 채움
    answer_hint: str    # 뷰어에서 무엇을 확인해 적을지
    parent_re: str | None = None  # 제목이 섹션이 아니라 소제목일 때 대신 찾을 감싸는 섹션의 제목 정규식


TEMPLATES = [
    # 최대주주·직원 현황은 목차 항목이 아니라 소제목(ATOC="N")인 보고서가 대부분 (2026-11-25 실측: 81개 후보 중 0개) → 감싸는 섹션으로 대신
    Template("largest_holder", "정형표", r"최대주주 및 특수관계인의 주식\s*소유", "{year}년 말 기준 {corp}의 최대주주는 누구이고 지분율은 몇 %인가?", "최대주주 이름과 기말 지분율", parent_re=r"주주에 관한 사항"),
    Template("ceo", "정형표", r"^\d+\.\s*임원(?:\s*및 직원 등)?의 현황|^\d+\.\s*임원\s*현황", "{year}년 사업보고서 기준 {corp}의 대표이사는 누구인가?", "임원 현황 표의 대표이사 이름"),
    Template("dividend", "정형표", r"배당에 관한 사항", "{corp}의 {year}년 주당 현금배당금은 얼마인가?", "배당 표의 주당 현금배당금(보통주)"),
    Template("investments", "정형표", r"타법인\s*출자", "{year}년 말 기준 {corp}가 출자한 회사 중 기말 장부가액이 가장 큰 곳은 어디인가?", "타법인 출자현황 표에서 장부가액 최대 행"),
    Template("employees", "정형표", r"^\d+\.\s*직원 등의 현황|직원 현황", "{year}년 말 기준 {corp}의 직원 수는 몇 명인가?", "직원 현황 표의 합계", parent_re=r"임원 및 직원 등의 현황"),
    Template("auditor", "정형표", r"회계감사인의 명칭|감사인에 관한 사항|외부감사", "{corp}의 {year}년 재무제표를 감사한 외부감사인은 어디인가?", "회계감사인 명칭"),
    Template("business", "서술", r"^\d+\.\s*사업의 개요", "{corp}의 주요 사업 부문은 무엇인가?", "사업의 개요 첫 문단의 부문 나열"),
    Template("credit_risk", "서술", r"금융위험의 관리|위험관리", "{corp}는 신용위험을 어떻게 측정하고 관리하는가?", "신용위험 관리 문단 요지(2~3문장)"),
    Template("fair_value", "서술", r"중요한 회계정책", "{corp}가 금융상품의 공정가치를 측정할 때 쓰는 수준(레벨) 구분은 무엇인가?", "공정가치 수준 1·2·3 정의"),
    Template("history", "서술", r"회사의 연혁", "{corp}의 {year}년 중 주요 연혁(합병·사명 변경·자회사 신설)은 무엇인가?", "연혁 표에서 해당 연도 행"),
    Template("committees", "서술", r"이사회에 관한 사항|이사회 내 위원회", "{corp} 이사회 안에는 어떤 위원회가 있는가?", "이사회 내 위원회 목록"),
]
TIMEPOINT_KEYS = ("largest_holder", "ceo")  # 전 연도 보고서에 쓰는 틀

SEEDS = [  # Phase 2 시점 질의 5개 중 4개. Q5(정정 전후 차이)는 API가 최신 정정본만 줘서 비교 대상이 없어 뺌 (0005)
    {"id": "p2-q1", "type": "시점", "phase": 3, "question": "2023년 3월 31일 기준 신한은행의 최대주주는 누구이고 지분율은 몇 %인가?",
     "answer": "신한금융지주회사 100%", "corp_name": "신한은행", "report_nm": "분기보고서 (2023.03)",
     "evidence_section": "최대주주 및 특수관계인의 주식소유 현황", "source": "phase2-Q1", "status": "draft",
     "note": "정답은 Phase 2 정형 데이터로 확인함. 뷰어에서 섹션 경로만 확인하고 ready로"},
    {"id": "p2-q2", "type": "시점", "phase": 3, "question": "2022년 12월 31일 기준 하나은행의 사외이사는 누구인가?",
     "answer": None, "corp_name": "하나은행", "report_nm": "사업보고서 (2022.12)",
     "evidence_section": "임원 및 직원 등의 현황", "source": "phase2-Q2", "status": "draft",
     "note": "Phase 2에서 6명으로 확인. 이름은 뷰어에서 확인해 적는다"},
    {"id": "p2-q3", "type": "다중경로", "phase": 4, "question": "4대 금융지주의 임원 중 같은 지주의 자회사 임원을 겸하는 사람은 누구인가?",
     "answer": None, "corp_name": None, "report_nm": None, "evidence_section": "임원 및 직원 등의 현황", "source": "phase2-Q3", "status": "draft",
     "note": "근거는 지주 4곳 최신 사업보고서 + 자회사 보고서. 검색이 지주 보고서의 임원 섹션을 찾는지만 잰다"},
    {"id": "p2-q4", "type": "다중경로", "phase": 4, "question": "4대 금융지주의 최대주주는 설립 이후 어떻게 바뀌었는가?",
     "answer": None, "corp_name": None, "report_nm": None, "evidence_section": "최대주주 변동현황", "source": "phase2-Q4", "status": "draft",
     "note": "근거는 지주 4곳 최신 사업보고서의 최대주주 변동현황 섹션"},
]


def latest_reports(con: duckdb.DuckDBPyConnection, corp_codes: list[str] | None = None) -> list[dict]:
    """회사별 (최신 사업보고서, 그 전 연도 사업보고서). 원문이 파싱된 것(document_section 있음)만."""
    sql = """
        WITH annual AS (
            SELECT d.corp_code, t.corp_name, t.role, d.rcept_no, d.rcept_dt,
                   regexp_extract(d.report_nm, '\\((\\d{4})\\.12\\)', 1) AS year
            FROM disclosure d
            JOIN target_corp t USING (corp_code)
            WHERE regexp_matches(d.report_nm, '^(\\[[^\\]]*\\]\\s*)*사업보고서\\s*\\(\\d{4}\\.12\\)')
              AND EXISTS (SELECT 1 FROM document_section s WHERE s.rcept_no = d.rcept_no)
        ), ranked AS (
            SELECT *, row_number() OVER (PARTITION BY corp_code, year ORDER BY rcept_dt DESC, rcept_no DESC) AS rn_in_year
            FROM annual
        ), per_year AS (
            SELECT *, dense_rank() OVER (PARTITION BY corp_code ORDER BY year DESC) AS year_rank
            FROM ranked WHERE rn_in_year = 1
        )
        SELECT corp_code, corp_name, role, rcept_no, year, year_rank FROM per_year WHERE year_rank <= 2
    """
    params: list = []
    if corp_codes:
        sql += " AND corp_code IN (" + ",".join("?" * len(corp_codes)) + ")"
        params = list(corp_codes)
    rows = con.execute(sql + " ORDER BY role DESC, corp_name, year_rank", params).fetchall()
    return [dict(zip(("corp_code", "corp_name", "role", "rcept_no", "year", "year_rank"), r)) for r in rows]


def find_section(con: duckdb.DuckDBPyConnection, rcept_no: str, title_re: str, shallowest: bool = False) -> tuple[str, int] | None:
    """본문 파일에서 제목이 정규식에 맞고 내용이 있는 섹션의 (경로, 섹션 번호). 없으면 None.

    기본은 가장 깊은 섹션(가장 구체적인 항목). 감싸는 섹션을 찾을 때(shallowest)는 가장 얕은 것 — 소제목 내용은 그 섹션 자체에 있다.
    """
    order = "depth ASC" if shallowest else "depth DESC"
    row = con.execute(
        f"""
        SELECT path, section_id FROM document_section
        WHERE rcept_no = ? AND file_name = ? || '.xml' AND regexp_matches(title, ?) AND (char_count > 0 OR table_count > 0)
        ORDER BY {order}, section_id LIMIT 1
        """,
        [rcept_no, rcept_no, title_re],
    ).fetchone()
    return (row[0], row[1]) if row else None


def with_particle(corp: str, particle: str) -> str:
    """회사 이름 뒤 조사를 받침에 맞게. '동양생명보험' + '는' → '동양생명보험은'. 한글이 아닌 글자로 끝나면 그대로."""
    last = corp[-1]
    if not ("가" <= last <= "힣"):
        return corp + particle
    has_final = (ord(last) - ord("가")) % 28 != 0
    pairs = {"는": ("은", "는"), "가": ("이", "가"), "를": ("을", "를")}
    if particle not in pairs:
        return corp + particle
    return corp + (pairs[particle][0] if has_final else pairs[particle][1])


def fill_question(template: str, corp: str, year: str) -> str:
    """틀의 {corp}는/{corp}가/{corp}를 자리를 조사까지 맞춰 채운다."""
    for particle in ("는", "가", "를"):
        template = template.replace("{corp}" + particle, with_particle(corp, particle))
    return template.format(corp=corp, year=year)


def pick_templates(corp_code: str, role: str) -> list[Template]:
    if role == "holding":
        return TEMPLATES
    start = int(corp_code) % len(TEMPLATES)  # 회사마다 다른 틀이 고르게 걸리도록
    return [TEMPLATES[(start + i) % len(TEMPLATES)] for i in range(SUBSIDIARY_TEMPLATES)]


def draft_questions(con: duckdb.DuckDBPyConnection, corp_codes: list[str] | None = None, max_total: int | None = None) -> list[Question]:
    today = date.today().isoformat()
    out: list[Question] = []
    for rep in latest_reports(con, corp_codes):
        templates = pick_templates(rep["corp_code"], rep["role"])
        if rep["year_rank"] == 2:
            templates = [t for t in templates if t.key in TIMEPOINT_KEYS]
        for t in templates:
            found = find_section(con, rep["rcept_no"], t.title_re) or (t.parent_re and find_section(con, rep["rcept_no"], t.parent_re, shallowest=True))
            if not found:
                continue
            path, _ = found
            qtype = "시점" if rep["year_rank"] == 2 else t.type
            out.append(Question(
                id=f"d{len(out) + 1:03d}", question=fill_question(t.question, rep["corp_name"], rep["year"]),
                type=qtype, phase=3, status="draft", answer=None, evidence_rcept_no=[rep["rcept_no"]],
                evidence_section=path, source=f"draft-{today}/{t.key}",
                note=f"확인할 것: {t.answer_hint}. 뷰어: {VIEWER_URL.format(rcept_no=rep['rcept_no'])}",
            ))
            if max_total and len(out) >= max_total:
                return out
    return out


def seed_questions(con: duckdb.DuckDBPyConnection) -> list[Question]:
    """Phase 2 질문의 접수번호를 disclosure에서 찾아 채운다. 회사나 보고서가 없으면 그 질문은 비고에 적고 근거를 비워 둔다."""
    out = []
    for s in SEEDS:
        evidence, note = [], s["note"]
        if s["corp_name"]:
            row = con.execute(
                """
                SELECT d.rcept_no FROM disclosure d JOIN target_corp t USING (corp_code)
                WHERE t.corp_name = ? AND d.report_nm LIKE '%' || ? ORDER BY d.rcept_dt DESC LIMIT 1
                """,
                [s["corp_name"], s["report_nm"]],
            ).fetchone()
            if row:
                evidence = [row[0]]
            else:
                note += f" / 접수번호를 못 찾음: {s['corp_name']} {s['report_nm']}"
        else:  # 지주 4곳의 최신 사업보고서
            rows = con.execute(
                """
                SELECT rcept_no FROM (
                    SELECT rcept_no, row_number() OVER (PARTITION BY corp_code ORDER BY rcept_dt DESC, rcept_no DESC) AS rn
                    FROM disclosure WHERE corp_code IN (SELECT UNNEST(?)) AND regexp_matches(report_nm, '^(\\[[^\\]]*\\]\\s*)*사업보고서\\s*\\(\\d{4}\\.12\\)')
                ) WHERE rn = 1 ORDER BY rcept_no
                """,
                [list(HOLDING_CORPS)],
            ).fetchall()
            evidence = [r[0] for r in rows]
        out.append(Question(id=s["id"], question=s["question"], type=s["type"], phase=s["phase"], status=s["status"],
                            answer=s["answer"], evidence_rcept_no=evidence, evidence_section=s["evidence_section"],
                            source=s["source"], note=note))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="평가 질문 후보 초안 생성 (정답은 비움)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--corps", nargs="*", help="corp_code로 회사 제한")
    ap.add_argument("--max", type=int, help="후보 수 상한")
    ap.add_argument("--seed", action="store_true", help="Phase 2 질문 4개도 접수번호를 채워 넣는다")
    args = ap.parse_args(argv)
    with connect(read_only=True) as con:
        questions = (seed_questions(con) if args.seed else []) + draft_questions(con, args.corps, args.max)
    args.out.write_text("".join(json.dumps(q.to_dict(), ensure_ascii=False) + "\n" for q in questions), encoding="utf-8")
    by_type = {}
    for q in questions:
        by_type[q.type] = by_type.get(q.type, 0) + 1
    print(f"후보 {len(questions)}개 → {args.out.relative_to(REPO_ROOT)}  유형별 {by_type}")
    print("다음: 뷰어에서 정답을 확인해 answer를 채우고 status를 ready로 바꾼 뒤 eval/questions.jsonl로 옮긴다 (eval/README.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
