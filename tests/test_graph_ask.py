"""그래프를 답변까지 잇는 부분 테스트: LLM 템플릿 고르기(picker), ask()의 관계표 전달, API /ask의 graph 응답. 외부 호출 없음.

확인하는 것
1) picker: 번호 → 템플릿 + 규칙으로 채운 빈칸. 0·범위 밖·깨진 JSON·회사 없음 → None. 규칙이 고른 질문에는 부르지 않는다
2) ask(): 그래프 관계표가 조각 뒤 마지막 번호로, 잘리지 않고 LLM에 간다. 인용되면 graph_cited
3) ask(): 조각이 없어도 관계표가 있으면 LLM을 부른다. 그래프 질의가 실패하면(Neo4jError) RAG만 쓰고 이유를 남긴다
4) API /ask: graph 칸(실행 기록·관계·파생 값·DART 링크), picker는 use_llm일 때만
"""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from eval.metrics import Hit
from src.api.main import create_app
from src.graph import picker as P
from src.graph.query import GraphRetriever
from src.graph.router import CorpMention, route
from src.loaders.duckdb_store import connect
from src.rag.ask import ask
from tests.test_graph_query import TemplateDriver, node, owns
from tests.test_rag_ask import FakeLLM

KBH, KBBANK = "00000001", "00000011"
TODAY = date(2026, 11, 25)
KB = [CorpMention(KBH, "KB금융지주", "KB금융지주")]


def descendants_driver(n_rows=1):
    kbh = node(KBH, "KB금융지주")
    paths = [{"nodes": [kbh, node(f"c{i}", f"자회사{i}")], "rels": [owns(KBH, 100.0, "20260301000003", "investment", f"i{i}")]} for i in range(n_rows)]
    return TemplateDriver({
        "descendants": paths,
        "evidence_chunks": [{"rcept_no": "20260301000003", "file_name": "f", "section_id": 1, "path": "타법인출자 현황", "text": "출자 표 조각", "seq": 0}],
    })


class Base:
    name = "neo4j-hybrid-fake"

    def __init__(self, hits=None):
        self.hits = hits if hits is not None else [Hit("20260301000009", "f", 2, "배당", 1.0, "배당 조각", "배당 조각")]

    def search(self, question, k):
        return self.hits[:k]


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
        with connect() as con:
            con.execute("INSERT INTO corp VALUES (?, 'KB금융지주', NULL, NULL, now()), (?, 'KB국민은행', NULL, NULL, now())", [KBH, KBBANK])
            con.execute("INSERT INTO target_corp VALUES (?, 'KB금융지주', 'holding', NULL, NULL, NULL, '2025', now())", [KBH])
            con.execute("INSERT INTO target_corp VALUES (?, 'KB국민은행', 'subsidiary', ?, 100.0, NULL, '2025', now())", [KBBANK, KBH])
        self.con = connect(read_only=True)
        self.addCleanup(self.con.close)


class PickerTest(DbTest):
    def test_choice_to_plan(self):
        calls = []
        pick = P.LLMPicker(complete=lambda ins, text, schema: calls.append((ins, text)) or '{"template": 2}')
        plan = pick("KB금융지주 밑에 있는 회사들은?", TODAY, KB)
        self.assertEqual(plan.steps[0].template, "descendants")
        self.assertEqual(plan.steps[0].params, {"corp": KBH, "as_of": "2026-11-25", "depth": 1, "min_pct": None})
        self.assertEqual(plan.rule, "LLM 고르기: descendants")
        self.assertIn("7. 한 회사의 그 시점", calls[0][0])   # 목록에 번호와 설명
        self.assertNotIn("evidence", calls[0][0])
        for step in plan.steps:
            P.TEMPLATES[step.template].bind(step.params)

    def test_refusals(self):
        for raw in ('{"template": 0}', '{"template": 9}', "not json", '{"x": 1}'):
            self.assertIsNone(P.LLMPicker(complete=lambda *a, r=raw: r)("질문", TODAY, KB), raw)
        self.assertIsNone(P.LLMPicker(complete=lambda *a: '{"template": 1}')("질문", TODAY, []))           # 회사가 필요한데 없음
        self.assertIsNone(P.LLMPicker(complete=lambda *a: '{"template": 3}')("질문", TODAY, KB))           # 경로는 회사 둘
        two = KB + [CorpMention(KBBANK, "KB국민은행", "KB국민은행")]
        plan = P.LLMPicker(complete=lambda *a: '{"template": 4}')("두 회사 사외이사", TODAY, two)
        self.assertEqual(plan.steps[0].params["corps"], [KBH, KBBANK])
        self.assertTrue(plan.steps[0].params["outside_only"])

    def test_only_called_when_rules_fail(self):
        calls = []
        pick = P.LLMPicker(complete=lambda *a: calls.append(1) or '{"template": 7}')
        route("2025년 12월 31일 기준 KB금융지주 아래 손자회사까지 포함하면 몇 곳?", self.con, today=TODAY, picker=pick)
        self.assertEqual(calls, [])
        plan = route("KB금융지주의 지배구조는?", self.con, today=TODAY, picker=pick)
        self.assertEqual(calls, [1])
        self.assertEqual(plan.steps[0].template, "facts_at")


class AskGraphTest(DbTest):
    Q = "2025년 12월 31일 기준 KB금융지주 아래 손자회사까지 포함하면 몇 곳인가?"

    def test_graph_table_is_last_passage_and_not_truncated(self):
        r = GraphRetriever(descendants_driver(40), self.con, base=Base(), today=TODAY)
        llm = FakeLLM("40곳입니다 [3].", [3])
        result = ask(self.Q, r, llm, k=2)
        passages = llm.calls[0][1]
        self.assertEqual(len(result.hits), 2)
        self.assertEqual(passages[-1].number, 3)
        self.assertTrue(passages[-1].text.startswith("[그래프 관계표]"))
        self.assertGreater(len(passages[-1].text), 1000)   # 조각 기본 상한(1,000자)보다 길어도
        from src.rag.llm import build_user_prompt
        self.assertIn("자회사39", build_user_prompt(self.Q, passages))
        self.assertTrue(result.graph_cited)
        self.assertEqual(result.cited_hits, [])           # 관계표 번호는 조각 목록에 없다

    def test_graph_answer_uses_larger_output_cap(self):
        # 관계표가 붙은 답은 길어서 LLM의 그래프 상한을 넘긴다 (2026-11-26 g05·g12가 600에서 잘림). 관계표가 없으면 기본 상한(None → LLM 기본)
        r = GraphRetriever(descendants_driver(3), self.con, base=Base(), today=TODAY)
        llm = FakeLLM("3곳입니다 [3].", [3])
        llm.max_output_tokens_graph = 2000
        ask(self.Q, r, llm, k=2)
        self.assertEqual(llm.last_max_output_tokens, 2000)
        ask("신한은행의 배당 성향은?", r, llm, k=2)      # 그래프 질문이 아님 → 관계표 없음
        self.assertIsNone(llm.last_max_output_tokens)

    def test_graph_only_answer_when_no_chunks(self):
        r = GraphRetriever(descendants_driver(1), self.con, base=Base([]), today=TODAY)
        d = r.driver.answers
        d["evidence_chunks"] = []
        llm = FakeLLM("1곳입니다 [1].", [1])
        result = ask(self.Q, r, llm, k=3)
        self.assertEqual(result.hits, [])
        self.assertEqual(len(llm.calls), 1)               # 조각은 없지만 관계표가 있으니 LLM을 부른다
        self.assertTrue(result.graph_cited)

    def test_graph_none_verdict_is_passed_even_without_relations(self):
        # 겸직 0행: 관계 행은 없지만 파생 값('없음' 판정)이 있으니 관계표를 LLM에 넘긴다. 프롬프트 규칙 8이 "없음"으로 답하게 한다
        from src.rag.llm import SYSTEM_PROMPT
        driver = TemplateDriver({"shared_positions": [], "position_scope": [{"corp_key": KBH, "corp_name": "KB금융지주", "persons": 7}]})
        r = GraphRetriever(driver, self.con, base=Base([]), today=TODAY)
        llm = FakeLLM("없음 — 기준일 2025-12-31 회사 1곳의 사외이사 7명 중 두 곳 이상에 있는 사람 0명 [1].", [1])
        result = ask("2025년 12월 31일 기준 4대 금융지주 중 두 곳 이상에서 동시에 사외이사인 사람이 있는가?", r, llm, k=3)
        self.assertEqual(len(llm.calls), 1)
        self.assertIn("없음 — 기준일 2025-12-31", llm.calls[0][1][-1].text)
        self.assertTrue(result.graph_cited)
        self.assertFalse(result.answer.not_found)
        self.assertIn('"없음"(대조해 보니 없음)과 "찾지 못함"(모름)을 섞지 않습니다', SYSTEM_PROMPT)

    def test_neo4j_error_falls_back_to_rag(self):
        from neo4j.exceptions import CypherSyntaxError

        class Broken:
            def execute_query(self, *a, **k):
                raise CypherSyntaxError("Invalid input")

        r = GraphRetriever(Broken(), self.con, base=Base(), today=TODAY)
        result = ask(self.Q, r, None, k=3)
        self.assertEqual([h.text for h in result.hits], ["배당 조각"])
        self.assertIsNone(result.graph)
        self.assertIn("CypherSyntaxError", result.graph_error)


class ApiGraphTest(DbTest):
    def make_client(self, picker_calls):
        llm = FakeLLM("1곳입니다 [2].", [2])
        driver = descendants_driver(1)
        driver.verify_connectivity = lambda: None
        driver.close = lambda: None
        app = create_app(driver=driver, llm_factory=lambda: llm, retriever_name="graph:only",
                         picker_factory=lambda: (picker_calls.append(1), P.LLMPicker(complete=lambda *a: '{"template": 0}'))[1])
        return self.enterContext(TestClient(app))

    def test_ask_returns_graph(self):
        calls = []
        client = self.make_client(calls)
        body = client.post("/ask", json={"question": AskGraphTest.Q, "use_llm": False}).json()
        self.assertEqual(calls, [])                        # 검색만이면 LLM 고르기도 안 씀
        g = body["graph"]
        self.assertEqual(g["trace"]["steps"][0]["template"], "descendants")
        self.assertEqual(g["relations"][0]["dst"], "자회사0")
        self.assertTrue(g["relations"][0]["url"].endswith("20260301000003"))
        self.assertIn("1단계 1곳", g["derived"][0]["value"])   # 합계 줄이 맨 앞 (LLM 표에서 잘리지 않게)
        self.assertEqual(g["n"], 2)
        self.assertFalse(g["cited"])

        body = client.post("/ask", json={"question": AskGraphTest.Q, "use_llm": True}).json()
        self.assertEqual(calls, [1])
        self.assertTrue(body["graph"]["cited"])
        self.assertEqual(body["answer"]["citations"], [2])

    def test_non_graph_question(self):
        client = self.make_client([])
        body = client.post("/ask", json={"question": "KB금융지주의 배당 정책은?", "use_llm": False}).json()
        self.assertIsNone(body["graph"])
        self.assertEqual(body["hits"], [])
        self.assertEqual(client.get("/health").json()["neo4j"], "ok")


if __name__ == "__main__":
    unittest.main()
