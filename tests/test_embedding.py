"""Phase 3 임베딩 검색 테스트 (0009). torch·sentence-transformers 없이 가짜 임베더·가짜 재순위로 흐름만 확인한다.

확인하는 것
1) embed_chunks: 전략의 조각을 임베딩해 chunk_embedding에 넣고, 다시 돌리면 0개(멱등), 조각 해시가 바뀐 것만 다시
2) DenseRetriever: 코사인 유사도 순서, 회사 필터, 재순위가 순서를 바꾼다
3) guess_corp_code: 질문 속 회사명 → corp_code (가장 긴 이름, "(주)"·"주식회사" 무시, 없으면 None)
4) make_retriever: dense:<모델>:<전략> 해석, 잘못된 이름은 SystemExit. 가짜 모델 주입
5) 가짜 임베더는 글자 두 글자 묶음(bigram)을 해시해 64차원으로 — 같은 낱말이 많으면 가깝다
"""

import hashlib
import math
import unittest

from eval.retrievers import make_retriever
from eval.retrievers.filters import guess_corp_code
from src.loaders.chunks import build_chunks
from src.loaders.duckdb_store import connect, upsert_disclosures
from src.rag.embedding import embed_chunks, embedding_stats, pending_chunks
from tests.helpers import TempDuckDBTestCase

WOORI = "20260313000815"
SHINHAN = "20260313000777"


class FakeEmbedder:
    name = "fake"
    dim = 64

    def encode(self, texts, batch_size=32, is_query=False):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for i in range(len(t) - 1):
                h = int(hashlib.md5(t[i:i + 2].encode()).hexdigest(), 16) % self.dim
                v[h] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class FakeReranker:
    """'배당'이 든 글을 맨 위로 올리는 가짜 재순위."""
    name = "fake"

    def score(self, query, texts):
        return [1.0 if "배당" in t else 0.0 for t in texts]


class EmbeddingTest(TempDuckDBTestCase):
    def setUp(self):
        super().setUp()
        self.con = connect()
        self.addCleanup(self.con.close)
        con = self.con
        con.execute("INSERT INTO target_corp VALUES ('00254045', '우리은행', 'subsidiary', '00000001', 100, NULL, '2025', now())")
        con.execute("INSERT INTO target_corp VALUES ('00149293', '신한은행', 'subsidiary', '00000002', 100, NULL, '2025', now())")
        con.execute("INSERT INTO target_corp VALUES ('00000002', '신한지주', 'holding', NULL, NULL, NULL, '2025', now())")
        upsert_disclosures(con, [
            {"rcept_no": WOORI, "corp_code": "00254045", "corp_name": "주식회사 우리은행", "stock_code": "", "corp_cls": "N",
             "report_nm": "사업보고서 (2025.12)", "flr_nm": "", "rcept_dt": "20260313", "rm": ""},
            {"rcept_no": SHINHAN, "corp_code": "00149293", "corp_name": "신한은행", "stock_code": "", "corp_cls": "N",
             "report_nm": "사업보고서 (2025.12)", "flr_nm": "", "rcept_dt": "20260313", "rm": ""},
        ])
        self._section(WOORI, 1, "VII. 주주에 관한 사항", "우리은행의 최대주주는 우리금융지주이며 지분율은 100%입니다.")
        self._section(WOORI, 2, "III. 재무 > 6. 배당에 관한 사항", "우리은행 주당 현금배당금은 보통주 500원입니다.")
        self._section(SHINHAN, 1, "VII. 주주에 관한 사항", "신한은행의 최대주주는 신한금융지주회사이며 지분율은 100%입니다.")
        self._section(SHINHAN, 2, "III. 재무 > 6. 배당에 관한 사항", "신한은행 주당 현금배당금은 보통주 1,190원입니다.")
        build_chunks(con, [WOORI, SHINHAN], ["section", "section_path"])

    def _section(self, rcept_no, section_id, path, text):
        self.con.execute(
            "INSERT INTO document_section VALUES (?, ? || '.xml', '11011', '사업보고서', ?, NULL, 1, NULL, NULL, NULL, ?, ?, false, true, ?, '[]', ?, 0)",
            [rcept_no, rcept_no, section_id, path.split(" > ")[-1], path, text, len(text)],
        )

    def test_embed_chunks_is_idempotent_and_recomputes_changed_hash(self):
        emb = FakeEmbedder()
        stats = embed_chunks(self.con, emb, "section_path", batch_size=2, log=lambda *_: None)
        self.assertEqual((stats["pending"], stats["embedded"]), (4, 4))
        self.assertIsNotNone(stats["per_1000"])
        again = embed_chunks(self.con, emb, "section_path", log=lambda *_: None)
        self.assertEqual(again["embedded"], 0)
        self.assertEqual(self.con.execute("SELECT count(*), any_value(dim) FROM chunk_embedding").fetchone(), (4, 64))
        # 조각 글이 바뀌어 해시가 달라지면 그 조각만 다시
        self.con.execute("UPDATE chunk SET text_hash = 'changed' WHERE strategy = 'section_path' AND seq = 0 AND rcept_no = ?", [WOORI])
        self.assertEqual(len(pending_chunks(self.con, "fake", "section_path")), 1)
        self.assertEqual(embed_chunks(self.con, emb, "section_path", log=lambda *_: None)["embedded"], 1)
        st = embedding_stats(self.con)
        self.assertEqual((st[0]["model"], st[0]["strategy"], st[0]["embedded"], st[0]["documents"], st[0]["stale"]), ("fake", "section_path", 4, 2, 0))
        # 다른 전략은 아직 0개
        self.assertEqual(len(pending_chunks(self.con, "fake", "section")), 4)

    def test_dense_retriever_orders_by_similarity_and_filters_by_corp(self):
        emb = FakeEmbedder()
        embed_chunks(self.con, emb, "section_path", log=lambda *_: None)
        r = make_retriever("dense:kure-v3:section_path", self.con, embedder=emb)
        self.assertEqual(r.name, "dense-fake-section_path")
        hits = r.search("우리은행의 최대주주는 누구인가?", 4)
        self.assertEqual(len(hits), 4)
        self.assertEqual(hits[0].path, "VII. 주주에 관한 사항")                 # 최대주주 조각이 맨 위
        self.assertTrue(hits[0].score >= hits[1].score >= hits[2].score)
        self.assertEqual(hits[0].rcept_no, WOORI)                              # 꼬리표에 "우리은행"이 있어 우리은행 쪽이 더 가깝다
        # 회사 필터: 신한은행 질문 → 신한은행 조각만
        rf = make_retriever("dense:kure-v3:section_path", self.con, corp_filter=True, embedder=emb)
        self.assertEqual(rf.name, "dense-fake-section_path-corp")
        hits = rf.search("신한은행의 주당 현금배당금은 얼마인가?", 4)
        self.assertEqual({h.rcept_no for h in hits}, {SHINHAN})
        self.assertEqual(hits[0].path, "III. 재무 > 6. 배당에 관한 사항")
        # 회사명이 없는 질문은 필터 없이
        self.assertEqual(len(rf.search("최대주주는 누구인가?", 4)), 4)

    def test_rerank_changes_order(self):
        emb = FakeEmbedder()
        embed_chunks(self.con, emb, "section_path", log=lambda *_: None)
        r = make_retriever("dense:kure-v3:section_path", self.con, embedder=emb, reranker=FakeReranker())
        self.assertEqual(r.name, "dense-fake-section_path-rerank_fake")
        hits = r.search("우리은행의 최대주주는 누구인가?", 2)
        self.assertTrue(all("배당" in h.path for h in hits))                    # 가짜 재순위가 배당 조각을 올림
        self.assertEqual([h.score for h in hits], [1.0, 1.0])
        # 조각 키워드 검색에도 같은 재순위·필터가 붙는다
        rk = make_retriever("chunk:section_path", self.con, corp_filter=True, reranker=FakeReranker())
        self.assertEqual(rk.name, "chunk-section_path-corp-rerank_fake")
        hits = rk.search("우리은행의 최대주주는 누구인가?", 1)
        self.assertEqual((hits[0].rcept_no, "배당" in hits[0].path), (WOORI, True))

    def test_guess_corp_code(self):
        self.assertEqual(guess_corp_code(self.con, "2025년 말 기준 우리은행의 최대주주는?"), "00254045")
        self.assertEqual(guess_corp_code(self.con, "주식회사 신한은행의 배당"), "00149293")
        self.assertEqual(guess_corp_code(self.con, "신한지주가 출자한 회사"), "00000002")   # 신한은행보다 신한지주가 맞음 (둘 다 "신한"으로 시작)
        self.assertIsNone(guess_corp_code(self.con, "최대주주는 누구인가?"))

    def test_make_retriever_errors(self):
        with self.assertRaises(SystemExit):
            make_retriever("dense:kure-v3", self.con, embedder=FakeEmbedder())
        with self.assertRaises(SystemExit):
            make_retriever("dense:nope:section_path", self.con)
        with self.assertRaises(SystemExit):
            make_retriever("dense:kure-v3:semantic", self.con, embedder=FakeEmbedder())
        with self.assertRaises(SystemExit):
            make_retriever("keyword", self.con, corp_filter=True)


if __name__ == "__main__":
    unittest.main()
