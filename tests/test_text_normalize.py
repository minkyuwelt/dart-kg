"""0004 전처리 함수 (src/graph/text_normalize.py). 실험 스크립트의 규칙이 그대로 옮겨졌는지 확인한다."""

import os
import unittest
from unittest import mock

from src.graph.client import settings, wait_until_ready
from src.graph.text_normalize import fulltext_query, normalize_for_fulltext, section_terms, section_vocabulary


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
        self.assertEqual(fulltext_query("대표이사 변경"), '"대표이사" OR "변경"')  # 낱말끼리 OR (2026-11-26, 문장 질문에서 AND는 0개)
        self.assertEqual(fulltext_query("   "), "")

    def test_sentence_question_keeps_content_words_and_drops_question_words(self):
        # d033 진단(2026-11-26): AND + 조사 붙은 낱말 + "무엇인가?" 때문에 전문검색이 0개였다
        q = fulltext_query("케이비국민카드의 2025년 중 주요 연혁은 무엇인가?")
        self.assertEqual(q, '("케이비국민카드의" OR "케이비국민카드") OR ("2025 년" OR 2025*) OR "주요" OR ("연혁은" OR "연혁")')
        self.assertNotIn("무엇인가", q)
        self.assertNotIn("AND", q)

    def test_particle_stripping_keeps_original_and_needs_two_chars_left(self):
        self.assertEqual(fulltext_query("우리은행의 최대주주는?"), '("우리은행의" OR "우리은행") OR ("최대주주는" OR "최대주주")')
        self.assertEqual(fulltext_query("회사에서는"), '("회사에서는" OR "회사")')   # 긴 조사 먼저
        self.assertEqual(fulltext_query("배당"), '"배당"')                           # 조사 아님 (뗀 뒤 1글자)
        self.assertEqual(fulltext_query("KB금융의"), '("KB 금융의" OR "KB 금융")')
        self.assertEqual(fulltext_query("누구인가?"), "")                              # 물음말만이면 빈 질의

    def test_section_words_are_required_and_others_optional(self):
        # 2026-11-26: 전부 OR은 너무 느슨했다(서술 섹션 hit@5 0.857→0.429). 섹션 이름 낱말만 필수(+), 나머지는 점수만
        vocab = section_vocabulary(["I. 회사의 개요 > 2. 회사의 연혁", "VII. 주주에 관한 사항 > 1. 배당에 관한 사항", "VIII. 임원 및 직원 등에 관한 사항"])
        self.assertTrue({"회사의", "회사", "연혁", "주주에", "주주", "배당에", "배당", "임원", "직원"} <= vocab)
        self.assertFalse({"관한", "사항", "개요"} & vocab)                        # 섹션을 가리키지 못하는 낱말은 사전에서 뺀다
        # "주요"는 목차("주요 제품 및 서비스")에 있어도 수식어라 사전에서 뺀다 — 들어가면 "(연혁) AND (주요)"가 되어 연혁 절 조각이 빠짓다 (d033)
        v2 = section_vocabulary(["II. 사업의 내용 > 2. 주요 제품 및 서비스", "I. 회사의 개요 > 2. 회사의 연혁", "VI. 임원 및 직원 등의 현황"])
        self.assertNotIn("주요", v2); self.assertNotIn("현황", v2); self.assertIn("연혁", v2); self.assertIn("임원", v2); self.assertIn("제품", v2)
        q2 = fulltext_query("케이비국민카드의 2025년 중 주요 연혁은 무엇인가?", v2)
        self.assertTrue(q2.startswith('("연혁은" OR "연혁") OR'))
        q = fulltext_query("케이비국민카드의 2025년 중 주요 연혁은 무엇인가?", vocab)
        R = '("연혁은" OR "연혁")'
        self.assertEqual(q, f'{R} OR ({R} AND ("케이비국민카드의" OR "케이비국민카드")) OR ({R} AND ("2025 년" OR 2025*)) OR ({R} AND "주요")')
        self.assertNotIn("+", q)   # 빈 낱말·+ 문법은 파서 기본 연산자에 따라 뜻이 달라져 쓰지 않는다
        # 섹션 낱말을 품은 낱말("배당금은" ⊃ "배당")도 필수 — 섹션 이름은 "배당에 관한 사항"이지만 질문은 "배당금"이라고 쓴다
        B = '("현금배당금은" OR "현금배당금")'
        self.assertEqual(fulltext_query("우리은행의 2025년 주당 현금배당금은?", vocab),
                         f'{B} OR ({B} AND ("우리은행의" OR "우리은행")) OR ({B} AND ("2025 년" OR 2025*)) OR ({B} AND "주당")')
        self.assertEqual(fulltext_query("배당 임원", vocab), '("배당" AND "임원")')   # 필수만 있으면 AND로 묶음
        self.assertEqual(section_terms("케이비국민카드의 2025년 중 주요 연혁은 무엇인가?", vocab), ["연혁은", "연혁"])
        self.assertEqual(section_terms("우리은행의 2025년 주당 현금배당금은?", vocab), ["현금배당금은", "현금배당금"])
        # 섹션 낱말이 없는 질문은 빈 질의 → 전문검색 건너뜀(벡터만)
        self.assertEqual(fulltext_query("동양생명보험은 신용위험을 어떻게 측정하고 관리하는가?", vocab), "")
        # 사전이 없으면(None) 낱말끼리 OR
        self.assertEqual(fulltext_query("대표이사 변경", None), '"대표이사" OR "변경"')

    def test_year_word_also_matches_dotted_dates(self):
        # d033: 연혁 조각은 "2025.02.18"로만 적혀 "2025 년" 구절에 안 걸렸다 → 연도 낱말은 앞부분 일치도 함께 (2026-11-26)
        self.assertEqual(fulltext_query("2025년 연혁"), '("2025 년" OR 2025*) OR "연혁"')
        self.assertEqual(fulltext_query("2024년말 기준"), '("2024 년말" OR 2024*) OR "기준"')
        self.assertEqual(fulltext_query("2025년의 배당"), '("2025 년의" OR "2025 년" OR 2025*) OR "배당"')   # 조사 뗀 꼴 + 앞부분 일치
        self.assertEqual(fulltext_query("1,400만 돌파"), '"1,400 만" OR "돌파"')      # 연도가 아닌 숫자는 그대로
        self.assertEqual(fulltext_query("2025.02.18 연혁"), '"2025.02.18" OR "연혁"')  # 날짜 그대로 물으면 구절 그대로

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
