"""평가 세트 도구 테스트: 질문 형식, 지표, 키워드 기본선, 근거 섹션 찾기, 후보 초안. DART API를 호출하지 않는다."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.draft import TEMPLATES_BY_KEY, draft_questions, fill_question, next_draft_number, seed_questions, with_particle
from eval.locate import locate
from eval.metrics import Hit, hit_at_k, reciprocal_rank, score_question, summarize
from eval.questions import NO_ANSWER, QuestionFormatError, load_questions, parse_question
from eval.retrievers.keyword import KeywordRetriever, tokenize
from eval.review import line_id, pending, read_lines, review, section_preview
from eval.run import evaluate
from src.collectors.corp_code import HOLDING_CORPS
from src.loaders.duckdb_store import connect, upsert_disclosures
from tests.test_report_details import disclosure_row

KB = next(iter(HOLDING_CORPS))  # 지주 하나
SHINHAN_BANK = "00149293"
KB_2025, KB_2024, SHB_2023Q1 = "20260313001191", "20250314000111", "20230515000222"


def ready(**over) -> dict:
    base = {"id": "q001", "question": "2025년 말 기준 KB금융지주의 최대주주는?", "type": "정형표", "phase": 3, "status": "ready",
            "answer": "국민연금공단 8.4%", "evidence_rcept_no": [KB_2025], "evidence_section": "최대주주", "source": "test"}
    base.update(over)
    return base


class QuestionFormatTest(unittest.TestCase):
    def test_ready_requires_answer_and_evidence(self):
        self.assertEqual(parse_question(ready()).id, "q001")
        with self.assertRaises(QuestionFormatError):
            parse_question(ready(answer=None))
        with self.assertRaises(QuestionFormatError):
            parse_question(ready(evidence_rcept_no=[]))
        with self.assertRaises(QuestionFormatError):
            parse_question(ready(evidence_rcept_no=["123"]))  # 14자리 아님
        with self.assertRaises(QuestionFormatError):
            parse_question(ready(type="기타"))
        with self.assertRaises(QuestionFormatError):
            parse_question(ready(phase=5))

    def test_none_type_has_no_evidence_and_fixed_answer(self):
        self.assertTrue(parse_question(ready(type="없음", answer=NO_ANSWER, evidence_rcept_no=[])).is_ready)
        with self.assertRaises(QuestionFormatError):
            parse_question(ready(type="없음", answer=NO_ANSWER))  # 근거가 있으면 안 됨
        with self.assertRaises(QuestionFormatError):
            parse_question(ready(type="없음", answer="몰라요", evidence_rcept_no=[]))

    def test_draft_may_be_empty_and_file_skips_comments_and_duplicate_ids(self):
        self.assertFalse(parse_question(ready(status="draft", answer=None, evidence_rcept_no=[])).is_ready)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "q.jsonl"
            p.write_text("# 주석\n\n" + json.dumps(ready()) + "\n" + json.dumps(ready(id="q002", status="draft")) + "\n", encoding="utf-8")
            qs = load_questions(p)
            self.assertEqual([q.id for q in qs], ["q001", "q002"])
            p.write_text(json.dumps(ready()) + "\n" + json.dumps(ready()) + "\n", encoding="utf-8")
            with self.assertRaises(QuestionFormatError):
                load_questions(p)

    def test_committed_questions_file_is_valid(self):
        qs = load_questions()
        self.assertGreaterEqual(len(qs), 5)
        self.assertTrue(all(q.type == "없음" and q.answer == NO_ANSWER for q in qs if q.id.startswith("n")))


class MetricsTest(unittest.TestCase):
    HITS = [Hit("A", "A.xml", 1, "I. 개요 > 1. 회사의 개요", 5.0), Hit("B", "B.xml", 3, "VII. 주주 > 1. 최대주주", 4.0),
            Hit("B", "B.xml", 9, "VIII. 임원", 3.0)]

    def test_hit_and_rr_at_doc_and_section_level(self):
        self.assertEqual(hit_at_k(self.HITS, ["B"], 1), 0)
        self.assertEqual(hit_at_k(self.HITS, ["B"], 2), 1)
        self.assertEqual(reciprocal_rank(self.HITS, ["B"]), 0.5)
        self.assertEqual(reciprocal_rank(self.HITS, ["B"], "임원"), 1 / 3)  # 맞는 보고서, 엉뚱한 섹션은 건너뜀
        self.assertEqual(reciprocal_rank(self.HITS, ["Z"]), 0.0)
        s = score_question(self.HITS, ["B"], "임원", k=2)
        self.assertEqual((s["hit_doc"], s["hit_section"], s["first_rank_doc"]), (1, 0, 2))

    def test_summarize_averages(self):
        s = summarize([{"hit_doc": 1, "hit_section": 0, "rr_doc": 1.0, "rr_section": 0.0},
                       {"hit_doc": 0, "hit_section": 0, "rr_doc": 0.0, "rr_section": 0.0}])
        self.assertEqual((s["n"], s["hit_doc"], s["hit_section"], s["mrr_doc"]), (2, 0.5, 0.0, 0.5))
        self.assertEqual(summarize([])["n"], 0)


class TokenizeTest(unittest.TestCase):
    def test_strips_particles_units_and_question_words(self):
        self.assertEqual(tokenize("2023년 3월 31일 기준 신한은행의 최대주주는 누구인가?"), ["2023", "31", "신한은행", "최대주주"])
        self.assertEqual(tokenize("KB금융지주가 2025년에 출자한 회사는?"), ["KB금융지주", "2025", "출자한", "회사"])
        self.assertEqual(tokenize("은행"), ["은행"])  # 두 글자는 조사를 떼지 않음


def insert_sections(con, rows):
    for rcept_no, section_id, path, text, tables in rows:
        title = path.split(" > ")[-1]
        con.execute(
            "INSERT INTO document_section VALUES (?, ? || '.xml', '11011', '사업보고서', ?, NULL, 2, NULL, NULL, NULL, ?, ?, false, true, ?, ?, ?, ?)",
            [rcept_no, rcept_no, section_id, title, path, text, json.dumps(tables, ensure_ascii=False), len(text), len(tables)],
        )


class RetrieverAndDraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
        self.con = connect()
        self.addCleanup(self.con.close)
        con = self.con
        con.execute("INSERT INTO target_corp VALUES (?, 'KB금융지주', 'holding', NULL, NULL, NULL, '2025', now())", [KB])
        con.execute("INSERT INTO target_corp VALUES (?, '신한은행', 'subsidiary', '00382199', 100, NULL, '2025', now())", [SHINHAN_BANK])
        upsert_disclosures(con, [
            disclosure_row(KB_2025, "사업보고서 (2025.12)", "20260313", corp_code=KB),
            disclosure_row(KB_2024, "사업보고서 (2024.12)", "20250314", corp_code=KB),
            disclosure_row("20250320000001", "[기재정정]사업보고서 (2024.12)", "20250320", corp_code=KB),  # 파싱 안 됨 → 제외
            disclosure_row(SHB_2023Q1, "분기보고서 (2023.03)", "20230515", corp_code=SHINHAN_BANK),
        ])
        table = [{"caption": None, "unit": None, "header_rows": 1, "rows": [["주주", "지분율"], ["국민연금공단", "8.4"]]}]
        insert_sections(con, [
            (KB_2025, 1, "I. 회사의 개요 > 1. 회사의 개요", "KB금융지주는 금융지주회사입니다.", []),
            (KB_2025, 2, "VII. 주주에 관한 사항 > 1. 최대주주 및 특수관계인의 주식소유 현황", "", table),
            (KB_2025, 3, "VII. 주주에 관한 사항 > 2. 최대주주 변동현황", "최대주주 변동 없음", []),
            (KB_2025, 4, "VIII. 임원 및 직원 등에 관한 사항 > 1. 임원 및 직원 등의 현황", "대표이사 양종희", []),
            (KB_2024, 1, "VII. 주주에 관한 사항 > 1. 최대주주 및 특수관계인의 주식소유 현황", "2024년 최대주주 국민연금공단", []),
            (SHB_2023Q1, 1, "VII. 주주에 관한 사항", "최대주주 및 특수관계인의 주식소유 현황 (소제목)", []),  # 소제목만 있는 보고서
        ])

    def test_keyword_retriever_ranks_title_matches_first(self):
        hits = KeywordRetriever(self.con).search("2025년 말 기준 KB금융지주의 최대주주는?", k=3)
        # 제목에 '최대주주'가 든 섹션들이 '회사의 개요'보다 앞. 본문에도 낱말이 있는 둘(3점)이 제목만 맞는 2(2점)보다 앞, 같은 점수면 짧은 것 먼저
        self.assertEqual([(h.rcept_no, h.section_id) for h in hits], [(KB_2025, 3), (KB_2024, 1), (KB_2025, 2)])
        self.assertTrue(all(h.score > 0 for h in hits))
        self.assertEqual(KeywordRetriever(self.con).search("??", k=3), [])

    def test_evaluate_scores_by_type(self):
        qs = [parse_question(ready()), parse_question(ready(id="q002", type="서술", question="KB금융지주의 주요 사업은?",
                                                            answer="금융지주", evidence_section="회사의 개요")),
              parse_question(ready(id="q003", type="시점", question="2024년 말 기준 KB금융지주의 최대주주는?",
                                   answer="국민연금공단", evidence_rcept_no=[KB_2024]))]
        result = evaluate(qs, KeywordRetriever(self.con), k=5)
        self.assertEqual(result["overall"]["n"], 3)
        self.assertEqual(result["overall"]["hit_doc"], 1.0)
        self.assertEqual(set(result["by_type"]), {"정형표", "서술", "시점"})
        self.assertEqual(len(result["questions"][0]["top"]), min(5, len(result["questions"][0]["top"])))

    def test_locate_finds_text_and_table_cells(self):
        rows = locate(self.con, KB_2025, "국민연금공단")
        self.assertEqual([(r[1], r[3]) for r in rows], [(2, "표")])
        rows = locate(self.con, KB_2025, "양종희")
        self.assertEqual((rows[0][1], rows[0][3]), (4, "본문"))
        self.assertEqual(locate(self.con, KB_2025, "없는글자"), [])

    def test_draft_uses_latest_parsed_report_and_previous_year_for_timepoint(self):
        drafts = draft_questions(self.con)
        by_source = {q.source.split("/")[1]: q for q in drafts if q.evidence_rcept_no == [KB_2025]}
        # 틀 중 섹션이 있는 것만 ('회사의 개요'는 '사업의 개요'가 아님). 직원 수는 감싸는 '임원 및 직원 등의 현황'으로 걸림
        self.assertEqual(set(by_source), {"largest_holder", "ceo", "employees"})
        self.assertEqual(by_source["largest_holder"].question, "2025년 말 기준 KB금융지주의 최대주주는 누구이고 지분율은 몇 %인가?")
        self.assertIn("1. 최대주주 및 특수관계인의 주식소유 현황", by_source["largest_holder"].evidence_section)
        self.assertTrue(all(q.status == "draft" and q.answer is None for q in drafts))
        previous = [q for q in drafts if q.evidence_rcept_no == [KB_2024]]
        self.assertEqual([(q.type, q.question) for q in previous], [("시점", "2024년 말 기준 KB금융지주의 최대주주는 누구이고 지분율은 몇 %인가?")])
        self.assertEqual([q for q in drafts if q.evidence_rcept_no == ["20250320000001"]], [])  # 파싱 안 된 정정본은 제외
        self.assertEqual(len(draft_questions(self.con, max_total=2)), 2)

    def test_draft_second_round_templates_filter_cap_and_id_start(self):
        insert_sections(self.con, [
            (KB_2025, 7, "II. 사업의 내용 > 3. 영업의 현황 > 가. 자본적정성", "", [{"caption": None, "unit": "%", "header_rows": 1, "rows": [["구분", "2025"], ["총자본비율", "16.5"]]}]),
            (KB_2025, 8, "XI. 그 밖에 투자자 보호를 위하여 필요한 사항 > 3. 제재 등과 관련된 사항", "해당사항 없음", []),
            (KB_2025, 9, "III. 재무에 관한 사항 > 주석 > 4. 금융위험관리 > (3) 시장위험", "VaR로 측정한다", []),
        ])
        keys = ["capital_adequacy", "sanction", "market_risk", "solvency"]
        drafts = draft_questions(self.con, template_keys=keys, start=95)
        self.assertEqual([q.source.split("/")[1] for q in drafts], ["capital_adequacy", "sanction", "market_risk"])   # 지급여력(보험)은 KB에 없음
        self.assertEqual([q.id for q in drafts], ["d095", "d096", "d097"])                                              # 기존 d094 다음부터
        self.assertEqual(drafts[0].question, "2025년 말 기준 KB금융지주의 BIS 총자본비율은 몇 %인가?")
        self.assertEqual((drafts[0].type, drafts[1].type, drafts[2].type), ("정형표", "서술", "서술"))
        self.assertIn("가. 자본적정성", drafts[0].evidence_section)
        self.assertEqual(len(draft_questions(self.con, template_keys=keys, per_template=1, start=1)), 3)              # 틀마다 상한 1 → 같음(회사 1곳)
        self.assertEqual(len(draft_questions(self.con, template_keys=["ceo"], per_template=1)), 1)
        # 번호 시작: questions.jsonl의 d### 최대 다음. 주석·p2-q1 같은 다른 id는 무시
        qp = Path(self.tmp.name) / "q.jsonl"
        qp.write_text('# 주석\n{"id": "d012", "x": 1}\n{"id": "p2-q1"}\n{"id": "d094", "x": 2}\n', encoding="utf-8")
        self.assertEqual(next_draft_number(qp), 95)
        self.assertEqual(next_draft_number(Path(self.tmp.name) / "없음.jsonl"), 1)
        self.assertEqual(len(TEMPLATES_BY_KEY), 20)

    def test_draft_falls_back_to_parent_section_when_title_is_only_a_subheading(self):
        # 신한은행 분기보고서(사업보고서가 아님)는 초안 대상이 아니므로, 같은 꼴을 KB 2025에 넣어 확인
        self.con.execute("DELETE FROM document_section WHERE rcept_no = ? AND section_id = 2", [KB_2025])  # 최대주주 섹션 제거
        insert_sections(self.con, [(KB_2025, 6, "VII. 주주에 관한 사항", "최대주주 및 특수관계인의 주식소유 현황 (소제목) 국민연금공단 8.4%", [])])
        drafts = {q.source.split("/")[1]: q for q in draft_questions(self.con) if q.evidence_rcept_no == [KB_2025]}
        # 제목 정규식은 '2. 최대주주 변동현황'에 안 맞고, 감싸는 섹션 중 가장 얕은 'VII. 주주에 관한 사항'으로
        self.assertEqual(drafts["largest_holder"].evidence_section, "VII. 주주에 관한 사항")
        self.assertIn("1. 임원 및 직원 등의 현황", drafts["employees"].evidence_section)

    def test_seed_questions_resolve_rcept_no_from_disclosure(self):
        seeds = {q.id: q for q in seed_questions(self.con)}
        self.assertEqual(seeds["p2-q1"].evidence_rcept_no, [SHB_2023Q1])
        self.assertEqual(seeds["p2-q1"].answer, "신한금융지주회사 100%")
        self.assertEqual(seeds["p2-q2"].evidence_rcept_no, [])  # 하나은행이 대상에 없음 → 비고에 기록
        self.assertIn("접수번호를 못 찾음", seeds["p2-q2"].note)
        self.assertEqual(seeds["p2-q4"].evidence_rcept_no, [KB_2025])  # 지주의 최신 사업보고서
        self.assertTrue(all(q.status == "draft" for q in seeds.values()))


if __name__ == "__main__":
    unittest.main()


class ReviewTest(unittest.TestCase):
    """대화형 정답 입력 도구. input/print를 가짜로 바꿔 돌린다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
        self.con = connect()
        self.addCleanup(self.con.close)
        table = [{"caption": None, "unit": "(단위: 원)", "header_rows": 1, "rows": [["구분", "당기", "당기", "전기"], ["주당 현금배당금", "4,790", "4,790", "4,020"]]}]
        insert_sections(self.con, [
            (KB_2025, 1, "I. 회사의 개요 > 1. 회사의 개요", "KB금융지주는 금융지주회사입니다.\n둘째 문단", []),
            (KB_2025, 2, "VII. 주주에 관한 사항 > 1. 최대주주 및 특수관계인의 주식소유 현황", "", table),
        ])
        self.questions = Path(self.tmp.name) / "questions.jsonl"
        self.draft = Path(self.tmp.name) / "questions.draft.jsonl"
        none_q = ready(id="n001", type="없음", status="draft", answer=NO_ANSWER, evidence_rcept_no=[], evidence_section=None,
                       question="KB금융지주의 비트코인 보유 수량은?")
        self.questions.write_text("# 주석\n" + json.dumps(none_q, ensure_ascii=False) + "\n", encoding="utf-8")
        d1 = ready(id="d001", status="draft", answer=None, note="확인할 것: 최대주주. 뷰어: https://x")
        d2 = ready(id="d002", status="draft", answer=None, type="서술", question="KB금융지주의 주요 사업은?", evidence_section="회사의 개요")
        d3 = ready(id="d003", status="draft", answer=None, type="서술", question="어색한 질문")
        self.draft.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in (d1, d2, d3)), encoding="utf-8")

    def test_section_preview_shows_text_and_table_rows_without_duplicated_merged_cells(self):
        lines = section_preview(self.con, KB_2025, "1. 최대주주 및 특수관계인의 주식소유 현황")
        self.assertEqual(lines[0], "[VII. 주주에 관한 사항 > 1. 최대주주 및 특수관계인의 주식소유 현황]")
        self.assertIn("<표 1> (단위: 원)", lines)
        self.assertIn("  주당 현금배당금 | 4,790 | 4,020", lines)  # 병합 복제 칸(바로 옆이 같음) 하나로
        # 떨어져 있는 같은 값은 지우지 않는다: 기초 = 기말인 출자 행에서 기말 칸이 사라져 보였던 문제
        insert_sections(self.con, [(KB_2025, 77, "XII. 상세표 > 3. 타법인출자 현황(상세)", "",
                                    [{"caption": None, "unit": None, "header_rows": 0,
                                      "rows": [["국민은행", "100", "100", "14,821,721", "-", "-", "100", "14,821,721"]]}])])
        lines = section_preview(self.con, KB_2025, "타법인출자 현황(상세)")
        self.assertIn("  국민은행 | 100 | 14,821,721 | - | 100 | 14,821,721", lines)
        self.assertEqual(section_preview(self.con, KB_2025, "없는 섹션"), ["(파싱된 섹션이 없습니다. 뷰어로 확인하세요)"])
        self.assertEqual(section_preview(self.con, KB_2025, None), [])

    def test_pending_lists_drafts_from_both_files_and_review_loop_writes_answers(self):
        todo = pending(self.questions, self.draft, None, None)
        self.assertEqual([q.id for q in todo], ["n001", "d001", "d002", "d003"])
        answers = iter(["y", "국민연금공단 8.4%", "m", "", "d"])  # 없음 확인, 정답, 더 보기 후 건너뛰기, 삭제
        printed = []
        stats = review(self.con, self.questions, self.draft, todo, input_fn=lambda _: next(answers), print_fn=printed.append)
        self.assertEqual(stats, {"accepted": 2, "dropped": 1, "skipped": 1})
        qs = {q.id: q for q in load_questions(self.questions)}
        self.assertEqual(set(qs), {"n001", "d001"})
        self.assertTrue(qs["n001"].is_ready and qs["n001"].answer == NO_ANSWER)
        self.assertEqual((qs["d001"].status, qs["d001"].answer), ("ready", "국민연금공단 8.4%"))
        self.assertTrue(qs["d001"].note.startswith("review로 확인"))
        self.assertEqual([line_id(l) for l in read_lines(self.draft)], ["d002"])  # 저장·삭제된 줄은 빠지고 건너뛴 것만 남음
        self.assertTrue(any("KB금융지주는 금융지주회사입니다." in p for p in printed))  # 원문이 화면에 나옴
        self.assertEqual(pending(self.questions, self.draft, None, None)[0].id, "d002")  # 다음 실행은 남은 것부터

    def test_long_paragraph_is_wrapped_not_truncated_and_e_changes_evidence_section(self):
        long = "첫 문장입니다. " * 60 + "공정가치 서열체계 수준 1은 활성시장의 공시가격입니다."
        insert_sections(self.con, [(KB_2025, 9, "III. 재무 > 12-A. 금융자산과 금융부채의 공정가치", long, [])])
        lines = section_preview(self.con, KB_2025, "12-A. 금융자산과 금융부채의 공정가치")
        self.assertTrue(all(len(l) <= 320 for l in lines))
        self.assertIn("수준 1은 활성시장의 공시가격", "".join(lines))  # 끝까지 보인다
        todo = pending(self.questions, self.draft, None, "d002")
        answers = iter(["e 12-A. 금융자산과 금융부채의 공정가치", "3수준으로 나눈다"])
        printed = []
        review(self.con, self.questions, self.draft, todo, input_fn=lambda _: next(answers), print_fn=printed.append)
        [q] = [q for q in load_questions(self.questions) if q.id == "d002"]
        self.assertEqual((q.status, q.evidence_section), ("ready", "12-A. 금융자산과 금융부채의 공정가치"))
        self.assertTrue(any("수준 1은" in p for p in printed))  # 바뀐 섹션의 원문이 바로 뜬다

    def test_none_type_accepts_only_y_and_quit_stops(self):
        todo = pending(self.questions, self.draft, "없음", None)
        answers = iter(["있음", "q"])
        printed = []
        stats = review(self.con, self.questions, self.draft, todo, input_fn=lambda _: next(answers), print_fn=printed.append)
        self.assertEqual(stats["accepted"], 0)
        self.assertTrue(any("y(공시에 없음)만" in p for p in printed))

    def test_particles_follow_final_consonant(self):
        self.assertEqual(with_particle("동양생명보험", "는"), "동양생명보험은")
        self.assertEqual(with_particle("신한카드", "가"), "신한카드가")
        self.assertEqual(with_particle("하나증권", "가"), "하나증권이")
        self.assertEqual(with_particle("KB", "는"), "KB는")
        self.assertEqual(fill_question("{corp}는 {year}년 {corp}의 보고서", "우리은행", "2025"), "우리은행은 2025년 우리은행의 보고서")
