"""9b: LLM 답변 생성 흐름. API를 부르지 않고 FakeLLM으로 확인한다.

1) build_user_prompt: 번호·꼬리표·본문(1,000자 자름)   2) parse_answer: 범위 밖 인용 버림, 깨진 JSON, 빈 답 → '찾지 못함'
3) ask: Hit.text를 조각으로, 인용 번호 → 근거 Hit, 뷰어 링크   4) DailyLimit: 한도에서 SystemExit   5) eval.run LLM 모드: 채점표 파일
"""

import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from eval.metrics import Hit
from eval.questions import Question
from src.rag.ask import VIEWER_URL, ask, format_result
from src.rag.llm import NOT_FOUND, Answer, DailyLimit, Passage, build_user_prompt, parse_answer


class FakeLLM:
    name = "fake-llm"

    def __init__(self, text, citations):
        self.text, self.citations, self.calls = text, citations, []

    def answer(self, question, passages, max_output_tokens=None):
        self.calls.append((question, passages))
        self.last_max_output_tokens = max_output_tokens
        return Answer(self.text, self.citations, self.name, 100, 20)


class FakeRetriever:
    name = "fake-retriever"

    def __init__(self, hits):
        self.hits = hits

    def search(self, question, k):
        return self.hits[:k]


HITS = [
    Hit("20260313000777", "20260313000777.xml", 2, "III. 재무 > 6. 배당에 관한 사항", 0.9, "배당 글…", "신한은행 | 사업보고서 (2025.12) | III. 재무 > 6. 배당\n주당 현금배당금: 보통주 1,190원"),
    Hit("20260313000815", "20260313000815.xml", 1, "VII. 주주에 관한 사항", 0.8, "주주 글…", "우리은행 최대주주는 우리금융지주 100%"),
]


class RagAskTest(unittest.TestCase):
    def test_prompt_and_parse(self):
        ps = [Passage(1, "신한은행 | 사업보고서 | 배당", "가" * 1200), Passage(2, "우리은행 | 사업보고서 | 주주", "짧은 글")]
        prompt = build_user_prompt("질문?", ps)
        self.assertTrue(prompt.startswith("질문: 질문?"))
        self.assertIn("[1] 신한은행 | 사업보고서 | 배당\n" + "가" * 1000 + " …", prompt)       # 1,000자에서 자름
        self.assertIn("[2] 우리은행 | 사업보고서 | 주주\n짧은 글", prompt)
        self.assertEqual(parse_answer('{"answer": "1,190원입니다 [1]", "citations": [1, 1, 7, "x"]}', 2), ("1,190원입니다 [1]", [1]))
        self.assertEqual(parse_answer('{"answer": "공시에서 찾지 못했습니다", "citations": [1]}', 2), (NOT_FOUND, []))    # 못 찾았다면 인용 없음
        self.assertEqual(parse_answer('{"answer": "", "citations": []}', 2), (NOT_FOUND, []))
        self.assertEqual(parse_answer("깨진 json", 2), ("깨진 json", []))

    def test_ask_builds_passages_and_maps_citations(self):
        llm = FakeLLM("신한은행의 주당 현금배당금은 보통주 1,190원입니다 [1]", [1])
        r = ask("신한은행 배당?", FakeRetriever(HITS), llm, k=5)
        self.assertEqual(len(llm.calls), 1)
        passages = llm.calls[0][1]
        self.assertEqual([p.number for p in passages], [1, 2])
        self.assertEqual(passages[0].text, HITS[0].text)                        # 조각 전문이 넘어간다 (snippet 아님)
        self.assertEqual(passages[0].label, "접수번호 20260313000777 | III. 재무 > 6. 배당에 관한 사항")   # con 없으면 접수번호로
        self.assertEqual([n for n, _ in r.cited_hits], [1])
        out = format_result(r)
        self.assertIn("답: 신한은행의 주당 현금배당금은 보통주 1,190원입니다 [1]", out)
        self.assertIn(VIEWER_URL.format(rcept_no="20260313000777"), out)
        self.assertIn("참고(검색은 됐지만 인용되지 않음):", out)                      # 2번 조각
        self.assertIn("모델 fake-llm, 토큰 입력 100 / 출력 20", out)
        # LLM 없음 → 검색 결과만
        r2 = ask("q", FakeRetriever(HITS), None, k=1)
        self.assertIsNone(r2.answer)
        self.assertIn("검색 결과 1개 (LLM 없음):", format_result(r2))
        # 검색 결과가 없으면 LLM을 부르지 않고 '찾지 못함'
        llm2 = FakeLLM("x", [])
        r3 = ask("q", FakeRetriever([]), llm2, k=5)
        self.assertEqual((r3.answer.text, llm2.calls), (NOT_FOUND, []))
        self.assertTrue(r3.answer.not_found)

    def test_daily_limit(self):
        with tempfile.TemporaryDirectory() as d:
            lim = DailyLimit(2, Path(d) / "calls.json")
            lim.take(); lim.take()
            self.assertEqual(lim.used_today(), 2)
            with self.assertRaises(SystemExit):
                lim.take()

    def test_eval_run_llm_mode_writes_grading_sheet(self):
        from eval import run as run_mod

        qs = [Question(id="d1", question="신한은행 배당?", type="정형표", phase=3, status="ready", answer="1,190원",
                       evidence_rcept_no=["20260313000777"], evidence_section="배당에 관한 사항", source="t", note=None),
              Question(id="n1", question="없는 질문?", type="없음", phase=3, status="ready", answer=NOT_FOUND,
                       evidence_rcept_no=[], evidence_section=None, source="t", note=None)]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(run_mod, "RESULTS_DIR", Path(d)), \
                mock.patch.object(run_mod, "connect") as con, mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(d) / "x.duckdb")}):
            con.return_value.__enter__.return_value = None
            args = Namespace(retriever="x", filter="corp", rerank=None, k=5, label="t", questions=Path("q.jsonl"))
            rc = run_mod.run_with_llm(args, qs, llm=FakeLLM("1,190원 [1]", [1]), retriever=FakeRetriever(HITS))
            self.assertEqual(rc, 0)
            files = sorted(p.name for p in Path(d).iterdir())
            self.assertEqual([f.split(".")[-1] for f in files], ["json", "md"])
            data = json.loads((Path(d) / files[0]).read_text(encoding="utf-8"))
            self.assertEqual((data["rows"][0]["hit_doc"], data["rows"][0]["citations"], data["rows"][1]["hit_doc"]), (True, [1], None))
            self.assertEqual(data["tokens"], {"input": 200, "output": 40})
            md = (Path(d) / files[1]).read_text(encoding="utf-8")
            self.assertIn("## d1 (정형표) 신한은행 배당?", md)
            self.assertIn("- 정답: 1,190원", md)
            self.assertIn("- 채점: ", md)


if __name__ == "__main__":
    unittest.main()
