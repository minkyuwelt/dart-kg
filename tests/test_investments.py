"""출자 대상 이름 정규화·corp_code 연결 테스트. DART API를 호출하지 않는다.

예시 이름은 2025 사업보고서 타법인 출자현황 실제 응답에서 가져왔다.
"""

import unittest

from src.loaders.investments import normalize_name, resolve, strip_bare_preferred, to_hangul_letters, to_number


class NormalizeNameTest(unittest.TestCase):
    def test_removes_corp_marks_and_footnotes(self):
        self.assertEqual(normalize_name("㈜우리은행 "), ("우리은행", None))
        self.assertEqual(normalize_name("우리금융캐피탈㈜"), ("우리금융캐피탈", None))
        self.assertEqual(normalize_name("㈜우리투자증권 \n주4)"), ("우리투자증권", None))
        self.assertEqual(normalize_name("ＰＯＳＣＯ"), ("POSCO", None))   # 전각 글자 → 보통 글자 (NFKC)
        self.assertEqual(normalize_name("핀크(주3)"), ("핀크", None))

    def test_splits_share_class(self):
        self.assertEqual(normalize_name("하나캐피탈(보통주)"), ("하나캐피탈", "보통주"))
        self.assertEqual(normalize_name("하나캐피탈(우선주)"), ("하나캐피탈", "우선주"))
        self.assertEqual(normalize_name("주식회사 큐리오시스 보통주"), ("큐리오시스", "보통주"))
        self.assertEqual(normalize_name("대동주택(상환우)"), ("대동주택", "상환우"))
        self.assertEqual(normalize_name("한국디지털에셋(무의결권전환주)"), ("한국디지털에셋", "무의결권전환주"))

    def test_same_company_in_different_share_class_spellings_becomes_one_name(self):
        # 2026-11-25 노트북 진단: 한 회사가 표기만 달라 여러 노드로 갈라졌다
        for raw in ("공부선배(상환전환우선주)", "공부선배_상환전환우선주", "공부선배상환전환우선주", "공부선배상환전환우선주(*7)"):
            self.assertEqual(normalize_name(raw), ("공부선배", "상환전환우선주"), raw)
        for raw in ("키움증권RCPS", "키움증권상환전환우선주비상장", "키움증권상환전환우선주(비상장(비상장)", "키움증권2우", "키움증권보통주"):
            self.assertEqual(normalize_name(raw)[0], "키움증권", raw)

    def test_share_class_spellings(self):
        cases = {
            "팀프레시_상환전환우선주": ("팀프레시", "상환전환우선주"),
            "현대차2우B": ("현대차", "2우B"),
            "더존비즈온1종RCPS": ("더존비즈온", "1종RCPS"),
            "콥틱제6종상환전환우선주식": ("콥틱", "제6종상환전환우선주식"),
            "케이뱅크은행무의결전환주": ("케이뱅크은행", "무의결전환주"),
            "젬나컴퍼니(기명식상환전환우선주)": ("젬나컴퍼니", "기명식상환전환우선주"),
            "딕스젠1우선주(전환상환)": ("딕스젠", "1우선주(전환상환)"),
            "페리지에어로스페이스RCPS2": ("페리지에어로스페이스", "RCPS"),
            "바이포엠상환전환우": ("바이포엠", "상환전환우"),
            "은평진관동피에프브이_제2종": ("은평진관동피에프브이", "제2종"),
            "하나오피스리츠제1종종류주인수": ("하나오피스리츠", "제1종종류주"),
            "대영디엘엠피에프브이(보통주/우선주)": ("대영디엘엠피에프브이", "보통주/우선주"),
            "*고피자전환우선주": ("고피자", "전환우선주"),
        }
        for raw, want in cases.items():
            self.assertEqual(normalize_name(raw), want, raw)

    def test_notes_and_cut_text_are_removed_but_not_kept_as_share_class(self):
        self.assertEqual(normalize_name("고덕강일1피에프브이_우선주(비상장)"), ("고덕강일1피에프브이", "우선주"))
        self.assertEqual(normalize_name("Noom,INC.(RCPS)(USD)"), ("Noom,INC.", "RCPS"))
        self.assertEqual(normalize_name("캡스톤우선주투자일반사모투자신탁(전문)"), ("캡스톤우선주투자일반사모투자신탁", None))
        self.assertEqual(normalize_name("딕스젠1우선주(전환상"), ("딕스젠", "1우선주"))      # 잘린 괄호
        self.assertEqual(normalize_name("와이디427피에프브이_1종종)"), ("와이디427피에프브이", "1종종"))
        self.assertEqual(normalize_name("주)에스디이코퍼레이션(상환전환우선주"), ("에스디이코퍼레이션", None))
        self.assertEqual(normalize_name("천안제이디씨피에프브이제2호_"), ("천안제이디씨피에프브이제2호", None))

    def test_keeps_names_that_only_look_like_share_class(self):
        # 펀드가 가진 주식을 괄호에 적은 것, 옛 이름, 중간의 '우선주'는 펀드·회사 이름의 일부
        for raw in ("KAI-더스퀘어신기술1호(이에이트RCPS)", "앤도버자산운용(구,인크리즈보통주)",
                    "에이치앤에스트러스트_우선주_대구신천동", "ABCCORPS", "HMCRCPS"):
            self.assertEqual(normalize_name(raw), (raw, None), raw)
        # 끝의 '우' 하나는 normalize_name이 떼지 않는다 (strip_bare_preferred가 회사 목록으로 확인)
        for raw in ("삼성전자우", "대우", "영우", "좋은만남선우"):
            self.assertEqual(normalize_name(raw), (raw, None), raw)


class BarePreferredTest(unittest.TestCase):
    index = {"삼성전자": ["00126380"], "대우": ["00000001"], "현대": ["00000002", "00000003"]}

    def test_strips_only_when_base_is_one_known_company(self):
        self.assertEqual(strip_bare_preferred("삼성전자우", self.index, {}), "삼성전자")
        self.assertIsNone(strip_bare_preferred("대우", self.index, {}))            # 너무 짧음 + 그 자체가 회사
        self.assertIsNone(strip_bare_preferred("좋은만남선우", self.index, {}))    # '좋은만남선'은 회사 목록에 없음
        self.assertIsNone(strip_bare_preferred("현대우", self.index, {}))          # 후보 2곳 → 추측하지 않음
        self.assertIsNone(strip_bare_preferred("삼성전자우", {**self.index, "삼성전자우": ["9"]}, {}))  # 이름 그대로 회사가 있으면 그대로


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
