"""문장 변화 리포트(src/reports/text_changes.py) 테스트. document_section에 두 해 보고서의 절을 직접 넣고 비교한다. API 호출 없음.

확인하는 것
1) 전년 같은 종류 보고서 찾기(정정본이면 최신 정정본, 반기는 반기와)
2) 숫자·날짜만 바뀐 문장은 세기만 하고, 글이 바뀐 문장만 전후로 나온다. 자리가 같은 짝은 '바뀜', 나머지는 '삭제'·'추가'
3) 기본 절 필터(위험·회계정책·경영진단)와 --section 바꾸기, 한쪽에만 있는 절, 첨부 파일은 보지 않음, 절 굵기가 다르면 상위 절로 묶어 견줌
4) markdown·요약, 첫 보고서는 비교 없음
"""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from src.loaders.duckdb_store import connect, upsert_disclosures
from src.reports.text_changes import (
    build_text_report, diff_sentences, mask, previous_report, render_markdown, report_text_changes, split_sentences, summarize,
)
from tests.test_report_details import disclosure_row

KB = "00688996"
R2024, R2025, R2025_FIX, H2025 = "20250314000001", "20260313000002", "20260401000003", "20250814000004"
RISK = "III. 재무에 관한 사항 > 7. 위험관리"
POLICY = "III. 재무에 관한 사항 > 3. 중요한 회계정책"
OVERVIEW = "I. 회사의 개요 > 1. 회사의 개요"

RISK_2024 = ("당사는 신용위험을 내부등급법으로 측정합니다. 2024년 12월 31일 현재 위험가중자산은 1,000억원입니다. "
             "시장위험은 VaR 모형으로 관리합니다. 유동성위험 한도는 이사회가 정합니다.")
RISK_2025 = ("당사는 신용위험을 내부등급법으로 측정합니다. 2025년 12월 31일 현재 위험가중자산은 1,200억원입니다. "
             "시장위험은 VaR 모형과 스트레스 테스트로 관리합니다. 유동성위험 한도는 이사회가 정합니다. "
             "기후위험은 2025년부터 별도 위원회가 점검합니다.")


class TextChangesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
        self.con = connect()
        self.addCleanup(self.con.close)
        upsert_disclosures(self.con, [
            disclosure_row(R2024, "사업보고서 (2024.12)", "20250314"),
            disclosure_row(H2025, "반기보고서 (2025.06)", "20250814"),
            disclosure_row(R2025, "사업보고서 (2025.12)", "20260313"),
            disclosure_row(R2025_FIX, "[기재정정]사업보고서 (2025.12)", "20260401"),
        ])
        self._section(R2024, 1, RISK, RISK_2024)
        self._section(R2024, 2, POLICY, "금융자산은 공정가치로 측정합니다. 대손충당금은 기대신용손실 모형을 씁니다.")
        self._section(R2024, 3, OVERVIEW, "당사는 2008년 설립된 금융지주회사입니다.")
        self._section(R2025, 1, RISK, RISK_2025)
        self._section(R2025, 2, POLICY, "금융자산은 공정가치로 측정합니다. 대손충당금은 기대신용손실 모형을 씁니다.")
        self._section(R2025, 3, OVERVIEW, "당사는 2008년 설립된 금융지주회사이며 자회사는 12개입니다.")
        self._section(R2025, 4, "III. 재무에 관한 사항 > 8. 위험관리 보완", "새로 생긴 절입니다. 운영위험을 따로 적습니다.")
        # 첨부(감사보고서)에 '위험' 절이 있어도 보지 않는다
        self._section(R2025, 1, RISK, "감사인이 본 위험 평가 문장입니다. 이 문장은 비교에 들어가면 안 됩니다.", file_name=f"{R2025}_00760.xml")

    def _section(self, rcept, sid, path, text, file_name=None):
        self.con.execute(
            "INSERT INTO document_section VALUES (?, ?, '11011', '사업보고서', ?, NULL, 1, NULL, NULL, NULL, ?, ?, false, true, ?, ?, ?, ?)",
            [rcept, file_name or f"{rcept}.xml", sid, path.split(" > ")[-1], path, text, json.dumps([]), len(text), 0],
        )

    def test_mask_and_split(self):
        self.assertEqual(mask("제17기(2025년 12월 31일 현재) 자산 1,234.5백만원"), "제#기(#년 #월 #일 현재) 자산 #백만원")
        self.assertEqual(split_sentences("가. 첫 문장입니다. 둘째 문장입니다!\n셋째 줄의 문장입니다"), ["첫 문장입니다.", "둘째 문장입니다!", "셋째 줄의 문장입니다"])

    def test_diff_separates_numbers_only_from_text(self):
        d = diff_sentences(split_sentences(RISK_2024), split_sentences(RISK_2025))
        self.assertEqual(d.status, "changed")
        self.assertEqual(d.numbers_only, 1)                      # 위험가중자산 금액·날짜만
        self.assertEqual(d.replaced, [("시장위험은 VaR 모형으로 관리합니다.", "시장위험은 VaR 모형과 스트레스 테스트로 관리합니다.")])
        self.assertEqual(d.added, ["기후위험은 2025년부터 별도 위원회가 점검합니다."])
        self.assertEqual(d.removed, [])
        same = diff_sentences(["자산은 100원입니다."], ["자산은 200원입니다."])
        self.assertEqual((same.status, same.numbers_only, same.text_changes), ("numbers_only", 1, 0))

    def test_previous_report_same_kind_prev_year_latest_revision(self):
        self.assertEqual(previous_report(self.con, KB, R2025)["rcept_no"], R2024)
        self.assertEqual(previous_report(self.con, KB, R2025_FIX)["rcept_no"], R2024)   # 정정본도 전년 사업보고서와
        self.assertIsNone(previous_report(self.con, KB, H2025))                          # 전년 반기 없음
        self.assertIsNone(previous_report(self.con, KB, R2024))

    def test_report_default_sections_and_attachments_ignored(self):
        tc = report_text_changes(self.con, R2025)
        self.assertEqual(tc.prev_rcept_no, R2024)
        paths = {s.path: s.status for s in tc.sections}
        self.assertEqual(paths, {RISK: "changed", POLICY: "same", "III. 재무에 관한 사항 > 8. 위험관리 보완": "added"})
        self.assertNotIn(OVERVIEW, paths)                       # 기본 필터 밖
        risk = next(s for s in tc.sections if s.path == RISK)
        self.assertNotIn("감사인이 본", " ".join(risk.added + risk.removed))
        self.assertEqual(summarize([tc]), {"reports": 1, "compared": 1, "sections_changed": 1, "text_changes": 2, "numbers_only": 1})

    def test_section_words_override_and_explicit_prev(self):
        tc = report_text_changes(self.con, R2025, section_words=("개요",))
        self.assertEqual([s.path for s in tc.sections], [OVERVIEW])
        self.assertEqual(tc.sections[0].replaced, [("당사는 2008년 설립된 금융지주회사입니다.", "당사는 2008년 설립된 금융지주회사이며 자회사는 12개입니다.")])
        fix = report_text_changes(self.con, R2025_FIX, prev_rcept_no=R2025)   # 정정본 ↔ 원본: 정정본 본문이 없어 모두 '삭제' 절
        self.assertEqual(fix.prev_rcept_no, R2025)
        self.assertTrue(all(s.status == "removed" for s in fix.sections))

    def test_sections_split_differently_are_merged_to_common_parent(self):
        # KB금융 2024·2025 실제 꼴: 전년은 'IV. 경영진단' 절 하나, 이번은 그 아래 소절 여럿. 상위 절로 묶어 견준다
        self._section(R2024, 9, "IV. 이사의 경영진단 및 분석의견", "예측정보에는 불확실성이 있습니다. 당기순이익은 5조원입니다. 유동성은 충분합니다.")
        self._section(R2025, 9, "IV. 이사의 경영진단 및 분석의견 > 1. 예측정보에 대한 주의사항", "예측정보에는 불확실성이 있습니다.")
        self._section(R2025, 10, "IV. 이사의 경영진단 및 분석의견 > 3. 재무상태 및 영업실적", "당기순이익은 5.5조원입니다.")
        self._section(R2025, 11, "IV. 이사의 경영진단 및 분석의견 > 4. 유동성 및 자금조달과 지출", "유동성은 충분하며 외화 조달도 늘렸습니다.")
        tc = report_text_changes(self.con, R2025, section_words=("경영진단",))
        self.assertEqual(len(tc.sections), 1)
        d = tc.sections[0]
        self.assertEqual((d.path, d.status, d.parts_before, d.parts_after, d.merged), ("IV. 이사의 경영진단 및 분석의견", "changed", 1, 3, True))
        self.assertEqual(d.numbers_only, 1)
        self.assertEqual(d.replaced, [("유동성은 충분합니다.", "유동성은 충분하며 외화 조달도 늘렸습니다.")])
        self.assertIn("전년 1절 ↔ 이번 3절을 묶어 견줌", render_markdown([tc]))

    def test_window_and_markdown(self):
        results = build_text_report(self.con, date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual([r.rcept_no for r in results], [R2025])        # 정정본은 본문이 없어 빠짐
        md = render_markdown(results, date(2026, 3, 1), date(2026, 3, 31))
        self.assertIn("| 바뀜 | 시장위험은 VaR 모형으로 관리합니다. | 시장위험은 VaR 모형과 스트레스 테스트로 관리합니다. |", md)
        self.assertIn("| 추가 |  | 기후위험은", md)
        self.assertIn("절 추가: III. 재무에 관한 사항 > 8. 위험관리 보완", md)
        self.assertIn("숫자만 바뀐 문장 1개", md)
        first = render_markdown([report_text_changes(self.con, R2024)])
        self.assertIn("전년 같은 종류의 보고서가 없어 비교하지 않음", first)
        self.assertNotIn(KB, md.replace("00688996", ""))  # 자리 표시 — corp_code는 본문에 안 쓴다


if __name__ == "__main__":
    unittest.main()
