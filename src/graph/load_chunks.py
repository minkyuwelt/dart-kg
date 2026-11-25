"""DuckDB의 조각(chunk)·벡터(chunk_embedding)를 Neo4j에 넣는다 (Phase 3 8번 PR).

원칙 (CLAUDE.md): DuckDB가 원본, Neo4j는 "진열대". 여기 코드는 읽어서 넣기만 하고, `reset`으로 전부 지우고 `build`로 다시 채울 수 있어야 한다.

노드·관계
    (:Document {rcept_no, corp_code, corp_name, report_nm, rcept_dt})
    (:Chunk {chunk_id, rcept_no, file_name, seq, section_id, corp_code, path, is_note, kind, text, text_ft, embedding})
    (:Document)-[:HAS_CHUNK]->(:Chunk)
  text    = 사람에게 보여 줄 원문 조각(꼬리표 없음), text_ft = 0004 전처리를 거친 전문검색용(꼬리표 포함 embed_text 기준 — 회사명·섹션 경로도 글자로 찾히게)
  embedding = 채택 모델(0009: kure-v3, 1,024차원)의 벡터. 다른 모델 벡터는 DuckDB에만 남긴다
  corp_code를 양쪽에 두는 이유: Phase 4에서 (:Corp) 노드와 바로 잇기 위해

인덱스
    chunk_id 유일 제약 / rcept_no 유일 제약 / 벡터 인덱스 chunk_embedding(코사인) / 전문검색 인덱스 chunk_text(cjk, text_ft)

멱등성: MERGE라 다시 돌려도 중복이 없다. 벡터가 아직 없는 조각(임베딩 진행 중)은 건너뛰고 수를 보고한다.

CLI (노트북, Neo4j 컨테이너가 떠 있어야 함):
    python -m src.graph.load_chunks build                      # 전체 (모델 kure-v3, 전략 section_path)
    python -m src.graph.load_chunks build --rcept 2026031300xxxx   # 보고서 하나만
    python -m src.graph.load_chunks stats                      # DuckDB 수 ↔ Neo4j 수 대조
    python -m src.graph.load_chunks reset                      # 노드·인덱스 전부 지우고 인덱스만 다시 만든다 (그 뒤 build)
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Iterator

import duckdb

from src.graph.client import connect, settings
from src.graph.text_normalize import normalize_for_fulltext
from src.loaders.duckdb_store import connect as duck_connect
from src.rag.embedding import MODELS

DEFAULT_MODEL = "kure-v3"
DEFAULT_STRATEGY = "section_path"
VECTOR_INDEX = "chunk_embedding"
FULLTEXT_INDEX = "chunk_text"


def schema_statements(dim: int) -> list[str]:
    """제약·인덱스. IF NOT EXISTS라 몇 번 실행해도 된다. 벡터 차원은 채택 모델의 저장 차원(0009)."""
    return [
        "CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
        "CREATE CONSTRAINT document_rcept_no_unique IF NOT EXISTS FOR (d:Document) REQUIRE d.rcept_no IS UNIQUE",
        "CREATE INDEX chunk_corp_code IF NOT EXISTS FOR (c:Chunk) ON (c.corp_code)",
        f"CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS FOR (c:Chunk) ON (c.embedding) "
        f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dim}, `vector.similarity_function`: 'cosine'}}}}",
        f"CREATE FULLTEXT INDEX {FULLTEXT_INDEX} IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text_ft] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}}",
    ]


DOCUMENT_MERGE = """
UNWIND $rows AS row
MERGE (d:Document {rcept_no: row.rcept_no})
SET d.corp_code = row.corp_code, d.corp_name = row.corp_name, d.report_nm = row.report_nm, d.rcept_dt = date(row.rcept_dt)
"""

# 벡터는 SET으로 넣으면 실수 리스트로만 저장되어 인덱스가 못 쓴다 → setNodeVectorProperty로 넣는다
CHUNK_MERGE = """
UNWIND $rows AS row
MERGE (c:Chunk {chunk_id: row.chunk_id})
SET c.rcept_no = row.rcept_no, c.file_name = row.file_name, c.seq = row.seq, c.section_id = row.section_id,
    c.corp_code = row.corp_code, c.path = row.path, c.is_note = row.is_note, c.kind = row.kind,
    c.text = row.text, c.text_ft = row.text_ft
WITH c, row
CALL db.create.setNodeVectorProperty(c, 'embedding', row.embedding)
WITH c, row
MATCH (d:Document {rcept_no: row.rcept_no})
MERGE (d)-[:HAS_CHUNK]->(c)
"""


def document_rows(con: duckdb.DuckDBPyConnection, strategy: str, rcept_nos: list[str] | None = None) -> list[dict]:
    """조각이 있는 보고서의 메타데이터 (disclosure에서)."""
    where = "d.rcept_no IN (SELECT DISTINCT rcept_no FROM chunk WHERE strategy = ?)"
    params: list = [strategy]
    if rcept_nos:
        where += " AND d.rcept_no IN (SELECT unnest(?::VARCHAR[]))"
        params.append(rcept_nos)
    rows = con.execute(
        f"SELECT d.rcept_no, d.corp_code, d.corp_name, d.report_nm, CAST(d.rcept_dt AS VARCHAR) FROM disclosure d WHERE {where} ORDER BY d.rcept_no",
        params,
    ).fetchall()
    return [{"rcept_no": r[0], "corp_code": r[1], "corp_name": r[2], "report_nm": r[3], "rcept_dt": r[4]} for r in rows]


def chunk_rows(con: duckdb.DuckDBPyConnection, model: str, strategy: str, rcept_nos: list[str] | None = None,
               batch: int = 1000) -> Iterator[list[dict]]:
    """벡터가 있는 조각을 batch개씩. text_ft는 여기서 만든다 (색인 전처리는 text_normalize 하나만 쓴다)."""
    where = "c.strategy = ? AND e.model_name = ? AND e.text_hash = c.text_hash"
    params: list = [strategy, model]
    if rcept_nos:
        where += " AND c.rcept_no IN (SELECT unnest(?::VARCHAR[]))"
        params.append(rcept_nos)
    cur = con.execute(
        f"""
        SELECT c.chunk_id, c.rcept_no, c.file_name, c.seq, c.section_id, c.corp_code, c.path, c.is_note, c.kind, c.text, c.embed_text, e.vector
        FROM chunk c JOIN chunk_embedding e ON e.chunk_id = c.chunk_id
        WHERE {where}
        ORDER BY c.rcept_no, c.file_name, c.seq
        """,
        params,
    )
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        yield [
            {"chunk_id": r[0], "rcept_no": r[1], "file_name": r[2], "seq": r[3], "section_id": r[4], "corp_code": r[5], "path": r[6],
             "is_note": bool(r[7]), "kind": r[8], "text": r[9], "text_ft": normalize_for_fulltext(r[10]), "embedding": [float(x) for x in r[11]]}
            for r in rows
        ]


def pending_count(con: duckdb.DuckDBPyConnection, model: str, strategy: str) -> tuple[int, int]:
    """(벡터 있는 조각 수, 벡터 없는 조각 수) — 없는 것은 Neo4j에 넣지 않고 보고만 한다."""
    have, missing = con.execute(
        """
        SELECT count(e.chunk_id), count(*) - count(e.chunk_id)
        FROM chunk c LEFT JOIN chunk_embedding e ON e.chunk_id = c.chunk_id AND e.model_name = ? AND e.text_hash = c.text_hash
        WHERE c.strategy = ?
        """,
        [model, strategy],
    ).fetchone()
    return have, missing


def ensure_schema(driver, dim: int, database: str = "neo4j") -> None:
    for stmt in schema_statements(dim):
        driver.execute_query(stmt, database_=database)


def build(con: duckdb.DuckDBPyConnection, driver, model: str = DEFAULT_MODEL, strategy: str = DEFAULT_STRATEGY,
          rcept_nos: list[str] | None = None, batch: int = 1000, database: str = "neo4j", log=print) -> dict:
    """DuckDB → Neo4j. 돌려주는 값: {"documents", "chunks", "skipped_no_embedding", "seconds"}."""
    if model not in MODELS:
        raise ValueError(f"모르는 임베딩 모델: {model}")
    t0 = time.perf_counter()
    ensure_schema(driver, MODELS[model].dim, database)
    docs = document_rows(con, strategy, rcept_nos)
    driver.execute_query(DOCUMENT_MERGE, rows=docs, database_=database)
    have, missing = pending_count(con, model, strategy)
    log(f"보고서 {len(docs)}개, 벡터 있는 조각 {have:,}개 (없는 조각 {missing:,}개는 건너뜀)")
    done = 0
    for rows in chunk_rows(con, model, strategy, rcept_nos, batch):
        driver.execute_query(CHUNK_MERGE, rows=rows, database_=database)
        done += len(rows)
        if done % (batch * 10) == 0 or done == have:
            log(f"  {done:,}/{have:,}  {time.perf_counter() - t0:,.0f}초")
    return {"documents": len(docs), "chunks": done, "skipped_no_embedding": missing, "seconds": round(time.perf_counter() - t0, 1)}


def neo4j_counts(driver, database: str = "neo4j") -> dict:
    rec = driver.execute_query(
        """
        OPTIONAL MATCH (d:Document) WITH count(d) AS docs
        OPTIONAL MATCH (c:Chunk) WITH docs, count(c) AS chunks
        OPTIONAL MATCH ()-[r:HAS_CHUNK]->() RETURN docs, chunks, count(r) AS rels
        """,
        database_=database,
    ).records[0]
    indexes = driver.execute_query("SHOW INDEXES YIELD name, type, state RETURN name, type, state", database_=database).records
    return {"documents": rec["docs"], "chunks": rec["chunks"], "rels": rec["rels"],
            "indexes": {r["name"]: f"{r['type']} {r['state']}" for r in indexes}}


def stats(con: duckdb.DuckDBPyConnection, driver, model: str = DEFAULT_MODEL, strategy: str = DEFAULT_STRATEGY, database: str = "neo4j") -> dict:
    have, missing = pending_count(con, model, strategy)
    docs = con.execute("SELECT count(DISTINCT rcept_no) FROM chunk WHERE strategy = ?", [strategy]).fetchone()[0]
    return {"duckdb": {"documents": docs, "chunks_with_embedding": have, "chunks_without_embedding": missing}, "neo4j": neo4j_counts(driver, database)}


def reset(driver, dim: int, database: str = "neo4j", log=print) -> None:
    """노드·관계를 1만 개씩 지우고(메모리 보호), 인덱스·제약을 지운 뒤 다시 만든다. IN TRANSACTIONS는 자동 커밋 세션에서만 된다."""
    with driver.session(database=database) as s:
        s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
        names = [r["name"] for r in s.run("SHOW INDEXES YIELD name, type WHERE type IN ['VECTOR', 'FULLTEXT', 'RANGE'] RETURN name")]
        for n in names:
            s.run(f"DROP INDEX {n} IF EXISTS").consume()
        cons = [r["name"] for r in s.run("SHOW CONSTRAINTS YIELD name RETURN name")]
        for n in cons:
            s.run(f"DROP CONSTRAINT {n} IF EXISTS").consume()
    log(f"지움: 노드 전부, 인덱스 {len(names)}개, 제약 {len(cons)}개")
    ensure_schema(driver, dim, database)
    log("인덱스·제약 다시 만듦")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DuckDB chunk → Neo4j")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--model", default=DEFAULT_MODEL)
    b.add_argument("--strategy", default=DEFAULT_STRATEGY)
    b.add_argument("--rcept", nargs="*", help="보고서 접수번호 (없으면 전체)")
    b.add_argument("--batch", type=int, default=1000)
    st = sub.add_parser("stats")
    st.add_argument("--model", default=DEFAULT_MODEL)
    st.add_argument("--strategy", default=DEFAULT_STRATEGY)
    r = sub.add_parser("reset")
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--yes", action="store_true", help="확인 없이 지운다")
    args = ap.parse_args(argv)

    s = settings()
    with connect(s) as driver:
        if args.cmd == "build":
            with duck_connect(read_only=True) as con:
                print(build(con, driver, args.model, args.strategy, args.rcept, args.batch, s.database))
        elif args.cmd == "stats":
            with duck_connect(read_only=True) as con:
                out = stats(con, driver, args.model, args.strategy, s.database)
            d, n = out["duckdb"], out["neo4j"]
            print(f"DuckDB  보고서 {d['documents']}  벡터 있는 조각 {d['chunks_with_embedding']:,}  없는 조각 {d['chunks_without_embedding']:,}")
            print(f"Neo4j   Document {n['documents']}  Chunk {n['chunks']:,}  HAS_CHUNK {n['rels']:,}")
            for name, state in n["indexes"].items():
                print(f"  인덱스 {name}: {state}")
            ok = d["documents"] == n["documents"] and d["chunks_with_embedding"] == n["chunks"] == n["rels"]
            print("일치" if ok else "불일치 — build를 다시 돌리면 빠진 것만 채워진다 (MERGE)")
        else:
            if not args.yes and input("Neo4j의 노드·인덱스를 전부 지웁니다. 계속? (yes/no) ").strip() != "yes":
                return 1
            reset(driver, MODELS[args.model].dim, s.database)
    return 0


if __name__ == "__main__":
    sys.exit(main())
