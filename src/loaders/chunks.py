"""document_section → chunk 테이블. 자르는 규칙은 src/loaders/chunking.py, 여기는 DuckDB 읽기·쓰기와 CLI.

멱등성: build는 같은 (전략, 접수번호)의 조각을 지우고 다시 넣는다. 재실행해도 중복이 없다.
기본은 본문 파일({rcept_no}.xml)만 자른다. 첨부(감사보고서 등)는 --attachments를 주면 함께 자른다.
평가 세트 근거 보고서만 자르려면 --from-eval (eval/questions.jsonl의 evidence_rcept_no 모음, 2026-11-24 사용자 결정).

CLI (저장소 루트에서, API 호출 없음):
    python -m src.loaders.chunks build --strategy section --from-eval            # 평가 세트 근거 보고서를 section 전략으로
    python -m src.loaders.chunks build --strategy all --rcept 20260313000815    # 한 보고서를 세 전략 모두
    python -m src.loaders.chunks build --strategy section --kind 11011          # 파싱된 사업보고서 전부
    python -m src.loaders.chunks stats                                          # 전략별 조각 수·길이 분포·표/글 비율
    python -m src.loaders.chunks show 20260313000815 --strategy section --section "임원" --limit 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import duckdb

from src.loaders.chunking import MAX_CHARS, STRATEGIES, Chunk, DocMeta, SectionData, chunk_sections
from src.loaders.duckdb_store import REPO_ROOT, connect

DEFAULT_QUESTIONS = REPO_ROOT / "eval" / "questions.jsonl"


def load_sections(con: duckdb.DuckDBPyConnection, rcept_no: str, file_name: str) -> list[SectionData]:
    rows = con.execute(
        """SELECT section_id, path, title, is_note, text, tables FROM document_section
           WHERE rcept_no = ? AND file_name = ? AND (char_count > 0 OR table_count > 0) ORDER BY section_id""",
        [rcept_no, file_name],
    ).fetchall()
    return [SectionData(r[0], r[1], r[2], bool(r[3]), r[4] or "", json.loads(r[5]) if r[5] else []) for r in rows]


def doc_meta(con: duckdb.DuckDBPyConnection, rcept_no: str, file_name: str) -> DocMeta:
    row = con.execute("SELECT corp_code, corp_name, report_nm FROM disclosure WHERE rcept_no = ?", [rcept_no]).fetchone()
    corp_code, corp_name, report_nm = row if row else (None, None, None)
    return DocMeta(rcept_no, file_name, corp_code, corp_name, report_nm)


def files_of(con: duckdb.DuckDBPyConnection, rcept_no: str, attachments: bool) -> list[str]:
    if not attachments:
        main = f"{rcept_no}.xml"
        n = con.execute("SELECT count(*) FROM document_section WHERE rcept_no = ? AND file_name = ?", [rcept_no, main]).fetchone()[0]
        return [main] if n else []
    rows = con.execute("SELECT DISTINCT file_name FROM document_section WHERE rcept_no = ? ORDER BY file_name", [rcept_no]).fetchall()
    return [r[0] for r in rows]


def chunk_rows(meta: DocMeta, chunks: list[Chunk]) -> list[list]:
    return [[f"{c.strategy}:{meta.rcept_no}:{meta.file_name}:{c.seq}", c.strategy, meta.rcept_no, meta.file_name, c.seq, c.section_id,
             meta.corp_code, meta.corp_name, meta.report_nm, c.path, c.is_note, c.kind, c.table_index, c.text, c.embed_text,
             c.char_count, c.text_hash()] for c in chunks]


def build_chunks(con: duckdb.DuckDBPyConnection, rcept_nos: list[str], strategies: list[str], attachments: bool = False,
                 max_chars: int = MAX_CHARS) -> dict:
    """접수번호들을 전략별로 잘라 chunk 테이블에 넣는다. 돌려주는 값: {"documents", "files", "chunks": {전략: 수}, "no_sections": [...]}."""
    for s in strategies:
        if s not in STRATEGIES:
            raise ValueError(f"모르는 전략: {s}")
    stats = {"documents": len(rcept_nos), "files": 0, "chunks": {s: 0 for s in strategies}, "no_sections": []}
    con.begin()
    try:
        for rcept_no in rcept_nos:
            files = files_of(con, rcept_no, attachments)
            if not files:
                stats["no_sections"].append(rcept_no)
                continue
            for strategy in strategies:
                con.execute("DELETE FROM chunk WHERE strategy = ? AND rcept_no = ?", [strategy, rcept_no])
            for file_name in files:
                meta = doc_meta(con, rcept_no, file_name)
                sections = load_sections(con, rcept_no, file_name)
                stats["files"] += 1
                for strategy in strategies:
                    chunks = chunk_sections(meta, sections, strategy, max_chars)
                    rows = chunk_rows(meta, chunks)
                    if rows:
                        con.executemany("INSERT INTO chunk VALUES (" + ",".join("?" * 17) + ", now())", rows)
                    stats["chunks"][strategy] += len(rows)
        con.commit()
    except Exception:
        con.rollback()
        raise
    return stats


def eval_rcept_nos(path: Path = DEFAULT_QUESTIONS) -> list[str]:
    """평가 세트(ready 질문)의 근거 접수번호 모음. 정렬해서 돌려준다."""
    from eval.questions import load_questions
    out: set[str] = set()
    for q in load_questions(path):
        if q.is_ready:
            out.update(q.evidence_rcept_no or [])
    return sorted(out)


def parsed_rcept_nos(con: duckdb.DuckDBPyConnection, reprt_code: str | None) -> list[str]:
    """파싱된 보고서 접수번호. reprt_code(11011 사업보고서 등)를 주면 그 종류만 (disclosure.report_nm으로 판별)."""
    from src.loaders.periodic_reports import periodic_report_key
    rows = con.execute(
        """SELECT DISTINCT s.rcept_no, d.report_nm FROM document_section s LEFT JOIN disclosure d USING (rcept_no) ORDER BY s.rcept_no"""
    ).fetchall()
    out = []
    for rcept_no, report_nm in rows:
        if reprt_code:
            key = periodic_report_key(report_nm or "")
            if not key or key[1] != reprt_code:
                continue
        out.append(rcept_no)
    return out


def unchunked_rcept_nos(con: duckdb.DuckDBPyConnection, strategy: str) -> list[str]:
    """섹션(document_section)은 있지만 이 전략의 조각이 없는 보고서 접수번호. 새 원문만 자를 때 쓴다 (chunks 자산)."""
    rows = con.execute(
        """SELECT DISTINCT s.rcept_no FROM document_section s
           WHERE NOT EXISTS (SELECT 1 FROM chunk c WHERE c.rcept_no = s.rcept_no AND c.strategy = ?)
           ORDER BY s.rcept_no""",
        [strategy],
    ).fetchall()
    return [r[0] for r in rows]


def chunk_stats(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """SELECT strategy, count(*), count(DISTINCT rcept_no), sum(CASE WHEN kind = 'table' THEN 1 ELSE 0 END),
                  sum(CASE WHEN is_note THEN 1 ELSE 0 END), avg(char_count), min(char_count), max(char_count),
                  quantile_cont(char_count, 0.5), sum(char_count)
           FROM chunk GROUP BY strategy ORDER BY strategy"""
    ).fetchall()
    return [{"strategy": r[0], "chunks": r[1], "documents": r[2], "tables": r[3], "notes": r[4], "avg_chars": round(r[5] or 0),
             "min_chars": r[6], "max_chars": r[7], "median_chars": round(r[8] or 0), "total_chars": r[9]} for r in rows]


def print_stats(con: duckdb.DuckDBPyConnection) -> None:
    rows = chunk_stats(con)
    if not rows:
        print("조각이 없습니다. 먼저 build를 실행하세요.")
        return
    print(f"{'전략':<14}{'조각':>8}{'보고서':>6}{'표 조각':>8}{'주석 조각':>9}{'평균자':>7}{'중앙자':>7}{'최대자':>7}{'총 글자':>12}")
    for r in rows:
        print(f"{r['strategy']:<14}{r['chunks']:>8,}{r['documents']:>6}{r['tables']:>8,}{r['notes']:>9,}{r['avg_chars']:>7}"
              f"{r['median_chars']:>7}{r['max_chars']:>7}{r['total_chars']:>12,}")
    # 길이 분포: 100자 단위 막대 (전략별)
    for r in rows:
        hist = con.execute(
            """SELECT CAST(floor(char_count / 200) * 200 AS INTEGER) AS b, count(*) FROM chunk WHERE strategy = ? GROUP BY b ORDER BY b""",
            [r["strategy"]],
        ).fetchall()
        print(f"\n[{r['strategy']}] 길이 분포 (200자 단위)")
        top = max(n for _, n in hist) or 1
        for b, n in hist:
            print(f"  {b:>5}~{b + 199:<5} {n:>7,} {'#' * max(1, round(40 * n / top))}")


def print_chunks(con: duckdb.DuckDBPyConnection, rcept_no: str, strategy: str, section_part: str | None, limit: int) -> None:
    sql = "SELECT seq, kind, path, char_count, text FROM chunk WHERE rcept_no = ? AND strategy = ?"
    params: list = [rcept_no, strategy]
    if section_part:
        sql += " AND contains(path, ?)"
        params.append(section_part)
    rows = con.execute(sql + " ORDER BY seq LIMIT ?", params + [limit]).fetchall()
    if not rows:
        print("조각이 없습니다 (전략·접수번호·섹션 글자를 확인하세요)")
    for seq, kind, path, n, text in rows:
        print(f"\n=== #{seq} [{kind}] {path}  ({n}자)")
        print(text[:1200] + (" …" if len(text) > 1200 else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="document_section → chunk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--strategy", default="section", help="fixed / section / section_path / all")
    b.add_argument("--rcept", nargs="*", help="접수번호들")
    b.add_argument("--from-eval", action="store_true", help="평가 세트 근거 보고서만")
    b.add_argument("--kind", help="정기보고서 코드 (11011 사업보고서, 11012 반기, 11013 1분기, 11014 3분기). 파싱된 것 중 그 종류 전부")
    b.add_argument("--attachments", action="store_true", help="첨부 파일도 자른다 (기본은 본문만)")
    b.add_argument("--max-chars", type=int, default=MAX_CHARS)
    sub.add_parser("stats")
    s = sub.add_parser("show")
    s.add_argument("rcept_no")
    s.add_argument("--strategy", default="section")
    s.add_argument("--section", help="섹션 경로에 든 글자")
    s.add_argument("--limit", type=int, default=5)
    args = ap.parse_args(argv)

    if args.cmd == "build":
        strategies = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
        with connect() as con:
            if args.rcept:
                rcept_nos = args.rcept
            elif args.from_eval:
                rcept_nos = eval_rcept_nos()
            else:
                rcept_nos = parsed_rcept_nos(con, args.kind)
            print(f"대상 보고서 {len(rcept_nos)}개, 전략 {strategies}, 한도 {args.max_chars}자")
            stats = build_chunks(con, rcept_nos, strategies, args.attachments, args.max_chars)
        print(stats)
        if stats["no_sections"]:
            print(f"파싱된 섹션이 없는 접수번호 {len(stats['no_sections'])}개 (먼저 python -m src.loaders.documents parse): "
                  + ", ".join(stats["no_sections"][:5]))
        return 0
    with connect(read_only=True) as con:
        if args.cmd == "stats":
            print_stats(con)
        else:
            print_chunks(con, args.rcept_no, args.strategy, args.section, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
