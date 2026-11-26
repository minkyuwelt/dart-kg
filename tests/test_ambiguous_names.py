"""ambiguous 이름 후보 고르기·판정 (API 호출 없음)."""

import unittest

from src.loaders.ambiguous_names import AmbiguousName, Candidate, judge, override_line, pick_candidates


def _name(name, *cands):
    return AmbiguousName(name, rows=10, investors=1, candidates=[Candidate(*c) for c in cands])


class PickCandidatesTest(unittest.TestCase):
    def test_splits_by_number_of_listed_candidates(self):
        one = _name("기아", ("01664948", "기아", None, "2022-05-13"), ("00106641", "기아", "000270", "2026-07-09"))
        many = _name("SK", ("00181712", "SK", "034730", "2024-03-28"), ("00144155", "SK", "003600", "2017-06-30"))
        none = _name("삼원", ("00180917", "삼원", None, "2017-06-30"), ("01745155", "삼원", None, "2023-05-09"))
        picked_one, picked_many = pick_candidates([one, many, none])
        self.assertEqual([a.name for a in picked_one], ["기아"])
        self.assertEqual([a.name for a in picked_many], ["SK"])  # 모두 비상장인 삼원은 다루지 않는다


class JudgeTest(unittest.TestCase):
    def test_ok_only_when_listed_has_disclosures_and_others_have_none(self):
        a = _name("기아", ("01664948", "기아", None, "2022-05-13"), ("00106641", "기아", "000270", "2026-07-09"))
        a.candidates[0].recent_count, a.candidates[1].recent_count = 0, 12
        verdict, chosen = judge(a)
        self.assertEqual((verdict, chosen.corp_code), ("OK", "00106641"))

    def test_check_when_unlisted_candidate_also_discloses(self):
        a = _name("진흥기업", ("00892650", "진흥기업", None, "2026-03-31"), ("00150828", "진흥기업", "002780", "2024-03-28"))
        a.candidates[0].recent_count, a.candidates[1].recent_count = 2, 5
        self.assertEqual(judge(a)[0], "CHECK")

    def test_check_when_listed_candidate_is_dormant(self):
        # 상장코드가 남아 있어도 공시가 없으면(상장폐지 뒤) 고르지 않는다
        a = _name("에피밸리", ("00367923", "에피밸리", None, "2017-06-30"), ("00301486", "에피밸리", "068630", "2017-06-30"))
        a.candidates[0].recent_count, a.candidates[1].recent_count = 0, 0
        self.assertEqual(judge(a)[0], "CHECK")

    def test_check_before_counts_are_fetched(self):
        a = _name("기아", ("01664948", "기아", None, "2022-05-13"), ("00106641", "기아", "000270", "2026-07-09"))
        self.assertEqual(judge(a)[0], "CHECK")


class OverrideLineTest(unittest.TestCase):
    def test_line_has_name_code_and_reason_with_other_candidates(self):
        a = _name("기아", ("01664948", "기아", None, "2022-05-13"), ("00106641", "기아", "000270", "2026-07-09"))
        a.candidates[0].recent_count, a.candidates[1].recent_count = 0, 12
        line = override_line(a, a.candidates[1], "20260828", "20261126")
        self.assertTrue(line.startswith("기아,00106641,동명 2곳. 상장코드 000270. 최근 3개월(2026-08-28~2026-11-26) 공시 12건. "))
        self.assertIn("01664948은 0건(modify_date 2022-05-13)", line)
        self.assertEqual(line.count(","), 2)  # CSV 3칸: 근거 문장에 쉼표를 쓰지 않는다


if __name__ == "__main__":
    unittest.main()
