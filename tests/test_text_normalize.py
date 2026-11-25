"""0004 전처리 함수 (src/graph/text_normalize.py). 실험 스크립트의 규칙이 그대로 옮겨졌는지 확인한다."""

import os
import unittest
from unittest import mock

from src.graph.client import settings, wait_until_ready
from src.graph.text_normalize import fulltext_query, normalize_for_fulltext


class TextNormalizeTest(unittest.TestCase):
    def test_boundary_and_middle_dot(self):
        self.assertEqual(normalize_for_fulltext("KB국민은행"), "KB 국민은행")
        self.assertEqual(normalize_for_fulltext("KB금융지주이며"), "KB 금융지주이며")
        self.assertEqual(normalize_for_fulltext("2025년 12월"), "2025 년 12 월")
        self.assertEqual(normalize_for_fulltext("임원ㆍ주요주주특정증권등소유상황보고서"), "임원 주요주주특정증권등소유상황보고서")
        self.assertEqual(normalize_for_fulltext("현금·현물배당결정"), "현금 현물배당결정")
        self.assertEqual(normalize_for_fulltext("  여러   공백  "), "여러 공백")
        self.assertEqual(normalize_for_fulltext("최대주주"), "최대주주")   # 바꿀 것이 없으면 그대로

    def test_fulltext_query_quotes_each_word_after_normalizing_inside(self):
        self.assertEqual(fulltext_query("KB금융"), '"KB 금융"')                    # 따옴표 안에서 전처리 — 밖이면 "KB" AND "금융"이 됨
        self.assertEqual(fulltext_query("대표이사 변경"), '"대표이사" AND "변경"')
        self.assertEqual(fulltext_query("  최대주주   지분율 "), '"최대주주" AND "지분율"')
        self.assertEqual(fulltext_query(""), "")
        self.assertEqual(fulltext_query('그는 "주주"다'), '"그는" AND "\\"주주\\"다"')  # 따옴표는 이스케이프

    def test_settings_requires_password_and_reads_port(self):
        with mock.patch.dict(os.environ, {"NEO4J_PASSWORD": "", "NEO4J_BOLT_PORT": ""}), mock.patch("src.graph.client.load_dotenv"):
            with self.assertRaises(SystemExit):
                settings()
        with mock.patch.dict(os.environ, {"NEO4J_PASSWORD": "pw", "NEO4J_BOLT_PORT": "27687"}), mock.patch("src.graph.client.load_dotenv"):
            s = settings()
        self.assertEqual((s.uri, s.user, s.password), ("bolt://127.0.0.1:27687", "neo4j", "pw"))

    def test_wait_until_ready_retries_then_gives_up(self):
        from neo4j.exceptions import ServiceUnavailable

        class Flaky:
            def __init__(self, fail_times):
                self.left = fail_times
                self.calls = 0

            def verify_connectivity(self):
                self.calls += 1
                if self.left > 0:
                    self.left -= 1
                    raise ServiceUnavailable("not yet")

        out = []
        with mock.patch("src.graph.client.time.sleep"):
            d = Flaky(2)
            wait_until_ready(d, timeout=10, log=lambda *a, **k: out.append(a[0] if a else ""))
            self.assertEqual(d.calls, 3)                                           # 두 번 실패 뒤 성공
            self.assertEqual(out[0], "Neo4j 준비 중")
            with mock.patch("src.graph.client.time.monotonic", side_effect=[0, 100, 100]):
                with self.assertRaises(SystemExit):
                    wait_until_ready(Flaky(5), timeout=90, log=lambda *a, **k: None)


if __name__ == "__main__":
    unittest.main()
