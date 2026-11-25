"""질문 후보를 한 개씩 보여 주고 정답을 받아 questions.jsonl에 넣는 대화형 도구.

    python -m eval.review                 # 후보(questions.draft.jsonl)와 questions.jsonl의 draft를 차례로
    python -m eval.review --type 정형표    # 유형 하나만
    python -m eval.review --id d012       # 특정 후보 하나

화면에는 질문·근거 섹션·뷰어 링크와 함께 **그 섹션의 원문(DuckDB document_section의 글과 표)**이 나온다.
대부분은 원문을 읽고 바로 정답을 칠 수 있고, 숫자가 미심쩍을 때만 뷰어를 연다.

입력:
  정답 글자 → 그 질문을 ready로 바꿔 questions.jsonl에 붙이고(후보 파일에서는 지움) 다음으로
  Enter     → 건너뛰기 (후보로 남음)
  m         → 원문 더 보기
  e <섹션>   → 근거 섹션 바꾸기 (틀이 짚은 섹션에 답이 없고 다른 섹션에 있을 때. locate로 찾은 경로의 일부를 붙인다)
  d         → 이 후보 삭제 (질문이 어색하거나 답이 없을 때)
  a         → 초안 답을 그대로 저장 (그래프 질문처럼 DuckDB에서 초안 답을 만든 후보만. 뷰어로 확인한 뒤에만 쓴다)
  q         → 종료
"없음" 유형은 원문이 없으므로 뷰어의 본문 검색으로 확인한 뒤 y를 치면 ready가 된다.

정답은 사람이 읽고 치는 것이고, 코드는 정답을 추측하지 않는다 (eval/README.md 규칙). 이 도구가 바꾸는 것은 보는 방식뿐이다.
JSON을 손으로 고치지 않으므로 따옴표·쉼표 실수가 생기지 않는다.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import duckdb

from eval.draft import DEFAULT_OUT as DEFAULT_DRAFT
from eval.questions import DEFAULT_QUESTIONS, NO_ANSWER, Question, load_questions
from src.loaders.duckdb_store import connect

PREVIEW_CHARS = 1500
PREVIEW_ROWS = 12
LINE = "-" * 70


def section_preview(con: duckdb.DuckDBPyConnection, rcept_no: str, section: str | None) -> list[str]:
    """근거 섹션의 원문을 화면용 줄 목록으로. 경로가 정확히 맞는 섹션이 없으면 경로에 그 글자가 든 섹션 중 첫 것.

    본문 파일(접수번호.xml)을 먼저 보고, 없으면 첨부도 본다. 글 다음에 표를 행 단위 '|'로 잇는다.
    """
    if not section:
        return []
    rows = con.execute(
        """
        SELECT path, text, tables FROM document_section
        WHERE rcept_no = ? AND (path = ? OR contains(path, ?)) AND (char_count > 0 OR table_count > 0)
        ORDER BY (path = ?) DESC, (file_name = ? || '.xml') DESC, depth DESC, section_id LIMIT 1
        """,
        [rcept_no, section, section, section, rcept_no],
    ).fetchall()
    if not rows:
        return ["(파싱된 섹션이 없습니다. 뷰어로 확인하세요)"]
    path, text, tables = rows[0]
    out = [f"[{path}]"]
    if text:
        for para in text.split("\n"):  # 긴 문단(주석 회계정책은 수천 자 한 덩어리)은 잘라서 여러 줄로 — 끊어서 숨기지 않는다
            out.extend(wrap(para))
    for i, t in enumerate(json.loads(tables), 1):
        out.append(f"<표 {i}>" + (f" {t['caption']}" if t.get("caption") else "") + (f" {t['unit']}" if t.get("unit") else ""))
        for row in t["rows"]:
            # 병합(COLSPAN/ROWSPAN)으로 복제된 칸은 바로 옆 칸과 같으므로 그때만 한 번으로 줄인다.
            # 행 전체에서 같은 값을 지우면 안 된다: 타법인출자 표처럼 기초와 기말이 같은 행은 기말 칸이 사라져 보였다 (2026-11-24)
            cells = [c for i, c in enumerate(row) if i == 0 or c != row[i - 1]]
            out.append("  " + " | ".join(c if c else "·" for c in cells))
    return out


WRAP_CHARS = 300


def first_preview(con: duckdb.DuckDBPyConnection, rcepts: list[str], section: str | None) -> list[str]:
    """근거 보고서를 순서대로 보며 그 섹션이 파싱된 첫 보고서의 원문. 여러 보고서가 근거인 질문(그래프 질문)에서
    첫 보고서가 파싱되지 않았거나(오래된 분기보고서) 그 섹션이 없을 때 다음 보고서를 본다. 어느 보고서인지 맨 앞에 적는다."""
    missing = ["(파싱된 섹션이 없습니다. 뷰어로 확인하세요)"]
    for r in rcepts:
        lines = section_preview(con, r, section)
        if lines and lines != missing:
            return ([f"(보고서 {r})"] if len(rcepts) > 1 else []) + lines
    return missing if section and rcepts else []


def wrap(text: str, width: int = WRAP_CHARS) -> list[str]:
    """글을 width 글자 안팎으로 자른다. 문장 끝(마침표 뒤 공백)에서 자르고, 없으면 그냥 자른다."""
    out, rest = [], text
    while len(rest) > width:
        cut = rest.rfind(". ", width // 2, width)
        cut = cut + 2 if cut > 0 else width
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def question_lines(q: Question, index: int, total: int) -> list[str]:
    out = [LINE, f"[{index}/{total}] {q.id}  {q.type}" + (f"  (phase {q.phase})" if q.phase != 3 else ""),
           f"질문: {q.question}"]
    if q.evidence_section:
        out.append(f"근거: {q.evidence_section}")
    for r in q.evidence_rcept_no:
        out.append(f"뷰어: https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r}")
    if q.note:
        out.append(f"메모: {q.note.split('뷰어:')[0].strip()}")
    if q.answer and not q.is_ready:
        out.append(f"초안 답 (DuckDB에서 만든 것, 미확인): {q.answer}")
    return out


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def line_id(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    try:
        return str(json.loads(s)["id"])
    except (json.JSONDecodeError, KeyError):
        return None


def accept(q: Question, answer: str, questions_path: Path, draft_path: Path) -> None:
    """정답을 받은 질문을 ready로 questions.jsonl에 넣는다. 후보 파일에 있던 줄은 지우고, questions.jsonl에 이미 있던 줄은 바꾼다."""
    q.answer = answer
    q.status = "ready"
    q.note = f"review로 확인 {date.today().isoformat()}" + (f" / {q.note}" if q.note and "뷰어:" not in q.note else "")
    new_line = json.dumps(q.to_dict(), ensure_ascii=False)
    lines = read_lines(questions_path)
    if any(line_id(l) == q.id for l in lines):
        lines = [new_line if line_id(l) == q.id else l for l in lines]
    else:
        lines.append(new_line)
    write_lines(questions_path, lines)
    if draft_path.exists():
        write_lines(draft_path, [l for l in read_lines(draft_path) if line_id(l) != q.id])


def drop(q: Question, draft_path: Path) -> None:
    if draft_path.exists():
        write_lines(draft_path, [l for l in read_lines(draft_path) if line_id(l) != q.id])


def pending(questions_path: Path, draft_path: Path, only_type: str | None, only_id: str | None) -> list[Question]:
    """아직 정답이 없는 질문: questions.jsonl의 draft + 후보 파일 중 questions.jsonl에 없는 것."""
    existing = load_questions(questions_path) if questions_path.exists() else []
    done_ids = {q.id for q in existing if q.is_ready}
    done_questions = {q.question for q in existing if q.is_ready}
    todo = [q for q in existing if not q.is_ready]
    if draft_path.exists():
        seen = {q.id for q in existing}
        todo += [q for q in load_questions(draft_path) if q.id not in seen and q.question not in done_questions]
    todo = [q for q in todo if q.id not in done_ids]
    if only_type:
        todo = [q for q in todo if q.type == only_type]
    if only_id:
        todo = [q for q in todo if q.id == only_id]
    return todo


def review(con, questions_path: Path, draft_path: Path, todo: list[Question], input_fn=input, print_fn=print) -> dict:
    """대화 루프. 테스트할 수 있게 input/print를 바꿔 넣을 수 있다. 돌려주는 값: {"accepted": n, "dropped": n, "skipped": n}."""
    stats = {"accepted": 0, "dropped": 0, "skipped": 0}
    total = len(todo)
    for i, q in enumerate(todo, 1):
        for line in question_lines(q, i, total):
            print_fn(line)
        if q.type == "없음":
            print_fn("(없음 유형) 뷰어의 본문 검색으로 정말 없는지 확인한 뒤 y를 치세요.")
            preview, shown = [], 0
        else:
            preview = first_preview(con, q.evidence_rcept_no, q.evidence_section)
            shown = _show(preview, 0, print_fn)
        while True:
            try:
                hint = ", a=초안 답 저장" if q.answer else ""
                raw = input_fn(f"정답 (Enter=건너뛰기, m=더 보기, e <섹션>=근거 바꾸기, d=삭제{hint}, q=종료): ").strip()
            except EOFError:
                raw = "q"
            if raw == "q":
                print_fn(f"\n저장됨 {stats['accepted']}개, 삭제 {stats['dropped']}개, 건너뜀 {stats['skipped']}개. 남은 후보는 다음에 이어서.")
                return stats
            if raw.startswith("e ") or raw == "e":
                part = raw[2:].strip()
                if not part:
                    print_fn("예: e 12-A. 금융자산과 금융부채의 공정가치   (섹션 경로의 일부. locate로 찾은 경로를 붙이면 됨)")
                    continue
                q.evidence_section = part
                preview = first_preview(con, q.evidence_rcept_no, part)
                print_fn(f"근거 섹션을 바꿨습니다: {part}")
                shown = _show(preview, 0, print_fn)
                continue
            if raw == "m":
                if shown >= len(preview):
                    print_fn("(원문 끝)")
                else:
                    shown = _show(preview, shown, print_fn)
                continue
            if raw == "a":
                if not q.answer:
                    print_fn("초안 답이 없는 후보입니다. 정답을 직접 쳐 주세요.")
                    continue
                raw = q.answer
            if raw == "":
                stats["skipped"] += 1
            elif raw == "d":
                drop(q, draft_path)
                stats["dropped"] += 1
                print_fn("삭제했습니다.")
            else:
                answer = NO_ANSWER if q.type == "없음" and raw.lower() == "y" else raw
                if q.type == "없음" and answer != NO_ANSWER:
                    print_fn(f"없음 유형의 정답은 y({NO_ANSWER})만 됩니다. 공시에 있으면 d로 지우세요.")
                    continue
                accept(q, answer, questions_path, draft_path)
                stats["accepted"] += 1
                print_fn(f"저장: {q.id} → ready")
            break
    print_fn(f"\n끝. 저장됨 {stats['accepted']}개, 삭제 {stats['dropped']}개, 건너뜀 {stats['skipped']}개.")
    return stats


def _show(preview: list[str], start: int, print_fn) -> int:
    """원문을 PREVIEW_CHARS 글자 또는 PREVIEW_ROWS 줄만큼 보여 주고, 다음 시작 위치를 돌려준다."""
    if not preview:
        return 0
    if start == 0:
        print_fn("--- 원문 ---")
    chars, i = 0, start
    while i < len(preview) and chars < PREVIEW_CHARS and i - start < PREVIEW_ROWS:
        line = preview[i]
        print_fn(line)
        chars += len(line)
        i += 1
    if i < len(preview):
        print_fn(f"… ({len(preview) - i}줄 더. m)")
    else:
        print_fn("--- 끝 ---")
    return i


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="질문 후보를 보고 정답을 쳐서 questions.jsonl에 넣는다")
    ap.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    ap.add_argument("--draft", type=Path, default=DEFAULT_DRAFT)
    ap.add_argument("--type", help="유형 하나만 (정형표/서술/시점/없음/다중경로)")
    ap.add_argument("--id", help="후보 하나만")
    args = ap.parse_args(argv)
    todo = pending(args.questions, args.draft, args.type, args.id)
    if not todo:
        print("정답이 비어 있는 질문이 없습니다. 후보를 더 만들려면 python -m eval.draft --seed")
        return 0
    by_type = {}
    for q in todo:
        by_type[q.type] = by_type.get(q.type, 0) + 1
    print(f"정답을 기다리는 질문 {len(todo)}개 {by_type}")
    with connect(read_only=True) as con:
        review(con, args.questions, args.draft, todo)
    ready = sum(q.is_ready for q in load_questions(args.questions))
    print(f"questions.jsonl의 ready 질문: {ready}개. 점수를 보려면 python -m eval.run --retrieval-only --label baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
