"""8번 PR: DuckDB 조각·벡터 → Neo4j 적재(src/graph/load_chunks.py)와 Neo4j 검색기(eval/retrievers/neo4j.py).

Neo4j 없이 가짜 드라이버로 확인한다 — 어떤 Cypher가 어떤 행과 함께 실행되는지, 결과 레코드가 Hit로 바뀌는지.
실제 인덱스 동작(근사 검색 정확도, cjk 분석기)은 노트북에서 eval.run으로 잰다 (0009·0004).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.retrievers import make_retriever
from eval.retrievers.neo4j import rrf_fuse
from src.graph.load_chunks import CHUNK_MERGE, DOCUMENT_MERGE, build, chunk_rows, document_rows, pending_count, schema_statements
from src.loaders.chunks import build_chunks
from src.loaders.duckdb_store import connect, upsert_disclosures
from src.rag.embedding import embed_chunks
from tests.test_embedding import FakeEmbedder

WOORI = "20260313000815"
SHINHAN = "20260313000777"


class FakeResult:
    def __init__(self, records):
        self.records = records


class FakeDriver:
    """execute_query 호출을 기록하고, 질의 종류에 따라 정해진 레코드를 돌려준다."""

    def __init__(self, records=None):
        self.calls = []
        self.records = records or []

    def execute_query(self, query, database_=None, **params):
        self.calls.append((query.strip(), params))
        if isinstance(self.records, dict):  # {"vector": [...], "fulltext": [...]}
            kind = "vector" if "db.index.vector" in query else "fulltext" if "db.index.fulltext" in query else None
            return FakeResult(self.records.get(kind, []))
        return FakeResult(self.records if "queryNodes" in query else [])


class LoadChunksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
        self.con = connect()
        self.addCleanup(self.con.close)
        con = self.con
        con.execute("INSERT INTO target_corp VALUES ('00254045', '우리은행', 'subsidiary', '00000001', 100, NULL, '2025', now())")
        con.execute("INSERT INTO target_corp VALUES ('00149293', '신한은행', 'subsidiary', '00000002', 100, NULL, '2025', now())")
        upsert_disclosures(con, [
            {"rcept_no": WOORI, "corp_code": "00254045", "corp_name": "주식회사 우리은행", "stock_code": "", "corp_cls": "N",
             "report_nm": "사업보고서 (2025.12)", "flr_nm": "", "rcept_dt": "20260313", "rm": ""},
            {"rcept_no": SHINHAN, "corp_code": "00149293", "corp_name": "신한은행", "stock_code": "", "corp_cls": "N",
             "report_nm": "사업보고서 (2025.12)", "flr_nm": "", "rcept_dt": "20260313", "rm": ""},
        ])
        for rcept, sid, path, text in [
            (WOORI, 1, "VII. 주주에 관한 사항", "우리은행의 최대주주는 우리금융지주이며 지분율은 100%입니다."),
            (WOORI, 2, "III. 재무 > 6. 배당에 관한 사항", "KB국민은행과 달리 우리은행 주당 현금배당금은 보통주 500원입니다."),
            (SHINHAN, 1, "VII. 주주에 관한 사항", "신한은행의 최대주주는 신한금융지주회사이며 지분율은 100%입니다."),
        ]:
            con.execute(
                "INSERT INTO document_section VALUES (?, ? || '.xml', '11011', '사업보고서', ?, NULL, 1, NULL, NULL, NULL, ?, ?, false, true, ?, '[]', ?, 0)",
                [rcept, rcept, sid, path.split(" > ")[-1], path, text, len(text)],
            )
        build_chunks(con, [WOORI, SHINHAN], ["section_path"])
        # 신한은행 조각은 임베딩이 없는 상태로 둔다 → 건너뛰는지 확인
        embed_chunks(con, FakeEmbedder(), "section_path", log=lambda *_: None)
        con.execute("DELETE FROM chunk_embedding WHERE chunk_id LIKE ?", [f"%{SHINHAN}%"])

    def test_rows_from_duckdb(self):
        docs = document_rows(self.con, "section_path")
        self.assertEqual([d["rcept_no"] for d in docs], [SHINHAN, WOORI])
        self.assertEqual(docs[1]["corp_code"], "00254045")
        self.assertEqual(docs[1]["rcept_dt"], "2026-03-13")                    # Cypher date()에 넣을 수 있는 꼴
        batches = list(chunk_rows(self.con, "fake", "section_path", batch=1))
        self.assertEqual(len(batches), 2)                                       # 벡터 있는 우리은행 조각 2개만, 1개씩
        row = next(r for b in batches for r in b if "배당" in r["path"])
        self.assertEqual(len(row["embedding"]), 64)
        self.assertIn("KB 국민은행", row["text_ft"])                            # 전문검색용 글은 0004 전처리 (영문·한글 경계 공백)
        self.assertIn("KB국민은행", row["text"])                                # 보여 줄 글은 원문 그대로
        self.assertTrue(row["text_ft"].startswith("주식회사 우리은행 |"))         # text_ft는 꼬리표 포함(embed_text 기준)
        self.assertEqual(pending_count(self.con, "fake", "section_path"), (2, 1))

    def test_build_runs_schema_documents_and_chunk_batches(self):
        drv = FakeDriver()
        out = build(self.con, drv, model="kure-v3", strategy="section_path", batch=1, log=lambda *_: None)
        # 가짜 임베더 벡터는 model_name 'fake'로 저장되어 있어 kure-v3로는 0개 → 건너뛴 수에 잡힌다
        self.assertEqual((out["documents"], out["chunks"], out["skipped_no_embedding"]), (2, 0, 3))
        queries = [q for q, _ in drv.calls]
        self.assertEqual(queries[:5], [s.strip() for s in schema_statements(1024)])  # 제약·인덱스 먼저
        self.assertIn("vector.dimensions`: 1024", queries[3])
        self.assertIn("`fulltext.analyzer`: 'cjk'", queries[4])
        self.assertEqual(queries[5], DOCUMENT_MERGE.strip())
        self.assertEqual(len(drv.calls[5][1]["rows"]), 2)
        # 가짜 임베더 이름으로 돌리면 조각 2개가 1개씩 두 배치
        with mock.patch.dict("src.rag.embedding.MODELS", {"fake": mock.Mock(dim=64)}):
            drv2 = FakeDriver()
            out = build(self.con, drv2, model="fake", strategy="section_path", batch=1, log=lambda *_: None)
        self.assertEqual((out["chunks"], out["skipped_no_embedding"]), (2, 1))
        chunk_calls = [(q, p) for q, p in drv2.calls if q == CHUNK_MERGE.strip()]
        self.assertEqual([len(p["rows"]) for _, p in chunk_calls], [1, 1])
        self.assertEqual(chunk_calls[0][1]["rows"][0]["rcept_no"], WOORI)

    def test_neo4j_retrievers_build_queries_and_hits(self):
        records = [{"rcept_no": WOORI, "file_name": WOORI + ".xml", "section_id": 2, "path": "III. 재무 > 6. 배당에 관한 사항", "score": 0.9, "text": "배당 글"}]
        drv = FakeDriver(records)
        r = make_retriever("neo4j:vector:kure-v3", self.con, embedder=FakeEmbedder(), driver=drv)
        self.assertEqual(r.name, "neo4j-vector-fake")
        hits = r.search("우리은행 배당", 5)
        self.assertEqual((hits[0].rcept_no, hits[0].section_id, hits[0].score), (WOORI, 2, 0.9))
        q, p = drv.calls[-1]
        self.assertIn("db.index.vector.queryNodes('chunk_embedding'", q)
        self.assertEqual((p["n"], p["k"], p["corp"]), (5, 5, None))             # 필터 없으면 후보 = k
        self.assertEqual(len(p["vec"]), 64)
        # 회사 필터: 회사명이 있으면 corp_code를 넘기고 후보를 k×20으로
        rc = make_retriever("neo4j:vector:kure-v3", self.con, corp_filter=True, embedder=FakeEmbedder(), driver=drv)
        rc.search("우리은행의 최대주주는?", 5)
        self.assertEqual((drv.calls[-1][1]["corp"], drv.calls[-1][1]["n"]), ("00254045", 100))
        rc.search("최대주주는?", 5)
        self.assertEqual((drv.calls[-1][1]["corp"], drv.calls[-1][1]["n"]), (None, 5))
        # 전문검색: 검색어는 fulltext_query (따옴표 안 전처리)
        ft = make_retriever("neo4j:fulltext", self.con, driver=drv)
        self.assertEqual(ft.name, "neo4j-fulltext")
        ft.search("KB금융 대표이사", 3)
        q, p = drv.calls[-1]
        self.assertIn("db.index.fulltext.queryNodes('chunk_text'", q)
        self.assertEqual(p["q"], '"KB 금융" AND "대표이사"')
        self.assertEqual(ft.search("   ", 3), [])                               # 낱말이 없으면 질의하지 않음
        for bad in ("neo4j:vector", "neo4j:nope", "neo4j:vector:nope"):
            with self.assertRaises(SystemExit):
                make_retriever(bad, self.con, driver=drv, embedder=None if bad.endswith("nope") else FakeEmbedder())

    def test_rrf_and_hybrid_retriever(self):
        def rec(rcept, sid, text):
            return {"rcept_no": rcept, "file_name": rcept + ".xml", "section_id": sid, "path": f"p{sid}", "score": 1.0, "text": text}
        a, b, c, d = rec(WOORI, 1, "a"), rec(WOORI, 2, "b"), rec(SHINHAN, 1, "c"), rec(SHINHAN, 2, "배당 d")
        # 벡터 순위 a,b,c / 전문검색 순위 c,d,a → a와 c는 두 목록 모두에 있어 위로. a: 1/61+1/63, c: 1/63+1/61 (동점), b: 1/62, d: 1/62
        fused = rrf_fuse([[a, b, c], [c, d, a]])
        self.assertEqual([r["text"] for r, _ in fused][:2].__len__(), 2)
        self.assertEqual({fused[0][0]["text"], fused[1][0]["text"]}, {"a", "c"})
        self.assertAlmostEqual(fused[0][1], 1 / 61 + 1 / 63)
        self.assertEqual({fused[2][0]["text"], fused[3][0]["text"]}, {"b", "배당 d"})
        self.assertEqual(rrf_fuse([[a], []])[0][1], 1 / 61)                          # 한 목록이 비어도 됨

        drv = FakeDriver({"vector": [a, b, c], "fulltext": [c, d, a]})
        r = make_retriever("neo4j:hybrid:kure-v3", self.con, embedder=FakeEmbedder(), driver=drv)
        self.assertEqual(r.name, "neo4j-hybrid-fake")
        hits = r.search("우리은행 배당", 3)
        self.assertEqual(len(hits), 3)
        self.assertEqual({hits[0].snippet, hits[1].snippet}, {"a", "c"})
        q_vec, p_vec = drv.calls[-2]
        q_ft, p_ft = drv.calls[-1]
        self.assertIn("db.index.vector", q_vec); self.assertIn("db.index.fulltext", q_ft)
        self.assertEqual((p_vec["k"], p_vec["n"], p_ft["k"], p_ft["q"]), (20, 20, 20, '"우리은행" AND "배당"'))   # 양쪽 20개씩 가져와 합침
        # 회사 필터 → 후보 20×20, corp 전달
        rc = make_retriever("neo4j:hybrid:kure-v3", self.con, corp_filter=True, embedder=FakeEmbedder(), driver=drv)
        rc.search("신한은행 배당", 2)
        self.assertEqual((drv.calls[-1][1]["corp"], drv.calls[-1][1]["n"]), ("00149293", 400))
        # 재순위는 하이브리드에만: '배당'이 든 d가 맨 위로
        from tests.test_embedding import FakeReranker
        rr = make_retriever("neo4j:hybrid:kure-v3", self.con, embedder=FakeEmbedder(), driver=drv, reranker=FakeReranker())
        self.assertEqual(rr.name, "neo4j-hybrid-fake-rerank_fake")
        self.assertEqual(rr.search("배당", 1)[0].snippet, "배당 d")
        with self.assertRaises(SystemExit):
            make_retriever("neo4j:vector:kure-v3", self.con, embedder=FakeEmbedder(), driver=drv, reranker=FakeReranker())


if __name__ == "__main__":
    unittest.main()
