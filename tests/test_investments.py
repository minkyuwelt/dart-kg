"""출자 대상 이름 정규화·corp_code 연결 테스트. DART API를 호출하지 않는다.

예시 이름은 2025 사업보고서 타법인 출자현황 실제 응답에서 가져왔다.
"""

import unittest

from src.loaders.investments import normalize_name, resolve, to_hangul_letters, to_number


class NormalizeNameTest(unittest.TestCase):
    def test_removes_corp_marks_and_footnotes(self):
        self.assertEqual(normalize_name("㈜우리은행 "), ("우리은행", None))
        self.assertEqual(normalize_name("우리금융캐피탈㈜"), ("우리금융캐피탈", None))
        self.assertEqual(normalize_name("㈜우리투자증권 \n주4)"), ("우리투자증권", None))
        self.assertEqual(normalize_name("핀크(주3)"), ("핀크", None))

    def test_splits_share_class(self):
        self.assertEqual(normalize_name("하나캐피탈(보통주)"), ("하나캐피탈", "보통주"))
        self.assertEqual(normalize_name("하나캐피탈(우선주)"), ("하나캐피탈", "우선주"))
        self.assertEqual(normalize_name("주식회사 큐리오시스 보통주"), ("큐리오시스", "보통주"))
        self.assertEqual(normalize_name("대동주택(상환우)"), ("대동주택", "상환우"))
        self.assertEqual(normalize_name("한국디지털에셋(무의결권전환주)"), ("한국디지털에셋", "무의결권전환주"))


class ResolveTest(unittest.TestCase):
    index = {
        "우리은행": ["00254045"],
        "케이비증권": ["00164876"],
        "신한디에스": ["00664710"],
        "하나은행": ["00158909", "00156017", "00130897"],
    }

    def test_hangul_letters(self):
        self.assertEqual(to_hangul_letters("KB증권"), "케이비증권")
        self.assertEqual(to_hangul_letters("신한EZ손해보험"), "신한이지손해보험")

    def test_resolve_order(self):
        self.assertEqual(resolve("우리은행", self.index, {}), ("00254045", "exact"))
        self.assertEqual(resolve("KB증권", self.index, {}), ("00164876", "hangul"))
        self.assertEqual(resolve("신한DS", self.index, {}), ("00664710", "hangul"))

    def test_ambiguous_is_not_guessed(self):
        self.assertEqual(resolve("하나은행", self.index, {}), (None, "ambiguous"))
        self.assertEqual(resolve("하나은행", self.index, {"하나은행": "00158909"}), ("00158909", "override"))

    def test_unresolvable_override_and_not_found(self):
        self.assertEqual(resolve("SHC매니지먼트", self.index, {"SHC매니지먼트": None}), (None, "override_unresolvable"))
        self.assertEqual(resolve("신한베트남은행", self.index, {}), (None, "not_found"))


class ToNumberTest(unittest.TestCase):
    def test_parses_dart_number_strings(self):
        self.assertEqual(to_number("716,000,000"), 716000000.0)
        self.assertEqual(to_number("99.88"), 99.88)
        self.assertIsNone(to_number("-"))
        self.assertIsNone(to_number(""))
        self.assertEqual(to_number("-1,234"), -1234.0)


if __name__ == "__main__":
    unittest.main()
