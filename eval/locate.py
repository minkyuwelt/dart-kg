"""근거 섹션 찾기. DART 뷰어에서 정답을 찾은 뒤, 그 글자가 든 섹션의 경로를 document_section에서 뽑아 준다.

    python -m eval.locate 20260313001191 "신한금융지주회사"
    python -m eval.locate 20260313001191 "최대주주" --file 20260313001191.xml

evidence_section을 손으로 베끼지 않게 하는 용도다. 출력된 경로를 questions.jsonl에 그대로 붙인다.
본문·표(JSON) 둘 다 찾는다. DART API 호출 없음.
"""

import argparse
import sys

import duckdb

from src.loaders.duckdb_store import connect


def locate(con: duckdb.DuckDBPyConnection, rcept_no: str, needle: str, file_name: str | None = None, limit: int = 20) -> list[tuple]:
    """(file_name, section_id, path, 어디서 찾았나, 앞뒤 글자) 목록. 본문에서 찾으면 그 주변, 표에서 찾으면 표 JSON 조각."""
    sql = """
        SELECT file_name, section_id, path,
               CASE WHEN contains(text, ?) THEN '본문' ELSE '표' END AS found_in,
               CASE WHEN contains(text, ?) THEN substr(text, greatest(1, position(? IN text) - 40), 120)
                    ELSE substr(CAST(tables AS VARCHAR), greatest(1, position(? IN CAST(tables AS VARCHAR)) - 40), 120) END AS context
        FROM document_section
        WHERE rcept_no = ? AND (contains(text, ?) OR contains(CAST(tables AS VARCHAR), ?))
    """
    params = [needle] * 4 + [rcept_no, needle, needle]
    if file_name:
        sql += " AND file_name = ?"
        params.append(file_name)
    sql += " ORDER BY file_name, section_id LIMIT ?"
    params.append(limit)
    return con.execute(sql, params).fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="근거 섹션 경로 찾기")
    ap.add_argument("rcept_no")
    ap.add_argument("needle", help="정답에 나오는 글자 (예: 회사명, 금액)")
    ap.add_argument("--file", help="ZIP 안 파일 이름으로 좁히기 (예: 20260313001191.xml)")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)
    with connect(read_only=True) as con:
        rows = locate(con, args.rcept_no, args.needle, args.file, args.limit)
    if not rows:
        print(f"{args.rcept_no}의 파싱된 섹션에 '{args.needle}'이(가) 없습니다. 먼저 python -m src.loaders.documents parse {args.rcept_no}")
        return 1
    for file_name, section_id, path, found_in, context in rows:
        print(f"[{file_name} #{section_id}] ({found_in}) {path}")
        print(f"    …{' '.join(context.split())}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
