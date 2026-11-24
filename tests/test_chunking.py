"""Phase 3 청킹 테스트 (0008 초안). DART API·임베딩 모델 없이 작은 섹션으로 확인한다.

확인하는 것
1) pack: 문단을 한도 안팎으로 묶고, 긴 문단은 문장 끝에서 자르고, 겹침은 앞 묶음 마지막 문단 하나
2) 표 → 문장: 머리행 여러 줄을 열 이름으로 합치고 "열이름: 값", 병합 복제 칸은 한 번만, 머리행 없는 표는 값만
3) 전략: section은 섹션 울타리·표 따로, section_path는 embed_text에만 꼬리표, fixed는 섹션 무시 고정 길이
4) DB: build_chunks 멱등(두 번 돌려도 수 같음), 본문 파일만/첨부 포함, --from-eval 접수번호, stats
5) 검색: chunk:<전략> 검색기가 평가 지표에 그대로 들어간다
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.metrics import hit_at_k
from eval.retrievers import make_retriever
from src.loaders.chunking import Chunk, DocMeta, SectionData, chunk_sections, pack, split_long, table_header_names, table_to_lines
from src.loaders.chunks import build_chunks, chunk_stats, eval_rcept_nos, parsed_rcept_nos
from src.loaders.duckdb_store import connect, upsert_disclosures

META = DocMeta("20260313000815", "20260313000815.xml", "00254045", "우리은행", "사업보고서 (2025.12)")
EXEC_TABLE = {"caption": None, "unit": None, "header_rows": 2,
              "rows": [["성명", "성별", "소유주식수", "소유주식수"], ["성명", "성별", "의결권 있는 주식", "의결권 없는 주식"],
                       ["정진완", "남", "-", "-"], ["양현근", "남", "100", "-"]]}


class PackTest(unittest.TestCase):
    def test_short_paragraphs_packed_up_to_limit_with_one_paragraph_overlap(self):
        paras = ["가" * 300, "나" * 300, "다" * 300, "라" * 300]
        groups = pack(paras, 1000, overlap=1)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0], "\n".join(["가" * 300, "나" * 300, "다" * 300]))
        self.assertEqual(groups[1], "\n".join(["다" * 300, "라" * 300]))  # 앞 묶음 마지막 문단을 겹침

    def test_long_paragraph_is_not_overlapped_and_no_group_made_of_overlap_only(self):
        # 600자 문단은 한도의 1/3(333자)보다 길어 겹치지 않는다. 겹쳤다면 둘째 조각이 1,200자가 됐을 것
        self.assertEqual(pack(["가" * 600, "나" * 600], 1000, overlap=1), ["가" * 600, "나" * 600])
        self.assertEqual(pack(["가" * 300, "나" * 300, "다" * 300, "라" * 100, "마" * 50], 1000, overlap=1)[1], "\n".join(["다" * 300, "라" * 100, "마" * 50]))

    def test_long_paragraph_split_at_sentence_end(self):
        text = "첫 문장입니다. " * 50 + "마지막 문장입니다."
        parts = split_long(text.strip(), 200)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 200 for p in parts))
        self.assertTrue(all(p.endswith("입니다.") for p in parts))
        self.assertEqual(split_long("짧다", 200), ["짧다"])
        self.assertEqual(split_long("가" * 450, 200), ["가" * 200, "가" * 200, "가" * 50])  # 문장 끝 없음 → 그냥 자름


class TableToLinesTest(unittest.TestCase):
    def test_multi_row_header_becomes_column_names_and_rows_become_sentences(self):
        self.assertEqual(table_header_names(EXEC_TABLE), ["성명", "성별", "소유주식수 의결권 있는 주식", "소유주식수 의결권 없는 주식"])
        head, lines = table_to_lines(EXEC_TABLE)
        self.assertEqual(head, "[표]")
        self.assertEqual(lines[0], "성명: 정진완 | 성별: 남 | 소유주식수 의결권 있는 주식: -")  # 옆 칸과 같은 "-"는 한 번
        self.assertEqual(lines[1], "성명: 양현근 | 성별: 남 | 소유주식수 의결권 있는 주식: 100 | 소유주식수 의결권 없는 주식: -")

    def test_caption_unit_and_headerless_table(self):
        t = {"caption": "2025.12.31", "unit": "(단위 : 백만원)", "header_rows": 0, "rows": [["자산총계", "27,394,372"], ["", ""]]}
        head, lines = table_to_lines(t)
        self.assertEqual(head, "[표] 2025.12.31 (단위 : 백만원)")
        self.assertEqual(lines, ["자산총계 | 27,394,372"])  # 빈 행은 뺌


class StrategyTest(unittest.TestCase):
    def sections(self):
        return [
            SectionData(1, "VII. 주주에 관한 사항", "VII. 주주에 관한 사항", False, "최대주주는 우리금융지주입니다.\n" + "설명. " * 300, []),
            SectionData(2, "VIII. 임원 > 1. 임원 현황", "1. 임원 현황", False, "가. 임원 현황", [EXEC_TABLE]),
            SectionData(3, "III. 재무 > 3. 연결재무제표 주석 > 1. 개요", "1. 개요", True, "주석 글.", []),
        ]

    def test_section_strategy_keeps_section_boundary_and_separates_tables(self):
        chunks = chunk_sections(META, self.sections(), "section", max_chars=1000)
        kinds = [(c.section_id, c.kind) for c in chunks]
        self.assertEqual(kinds[:2], [(1, "text"), (1, "text")])          # 섹션 1은 길어서 둘로
        self.assertIn((2, "text"), kinds); self.assertIn((2, "table"), kinds)
        self.assertEqual(kinds[-1], (3, "text"))
        self.assertTrue(all(c.section_id == 1 or c.char_count <= 1000 for c in chunks))
        table = next(c for c in chunks if c.kind == "table")
        self.assertTrue(table.text.startswith("[표]\n성명: 정진완"))
        self.assertEqual(table.table_index, 0)
        self.assertEqual([c.seq for c in chunks], list(range(len(chunks))))
        self.assertEqual(chunks[0].embed_text, chunks[0].text)          # section은 꼬리표 없음
        self.assertTrue(chunks[-1].is_note)

    def test_section_path_strategy_prefixes_embed_text_only(self):
        chunks = chunk_sections(META, self.sections(), "section_path")
        c = chunks[0]
        self.assertEqual(c.embed_text, "우리은행 | 사업보고서 (2025.12) | VII. 주주에 관한 사항\n" + c.text)
        self.assertFalse(c.text.startswith("우리은행 |"))
        self.assertNotEqual(c.text_hash(), chunk_sections(META, self.sections(), "section")[0].text_hash())

    def test_fixed_strategy_ignores_sections(self):
        chunks = chunk_sections(META, self.sections(), "fixed", max_chars=500)
        self.assertTrue(all(c.char_count <= 500 for c in chunks))
        self.assertTrue(all(c.kind == "text" for c in chunks))
        joined = "\n".join(c.text for c in chunks)
        self.assertIn("성명: 정진완", joined)                           # 표도 문장으로 섞여 들어감
        self.assertEqual(chunks[0].section_id, 1)
        # 섹션 경계를 무시하므로 임원 표와 주석 글이 한 조각에 섞인다. 조각의 섹션은 시작 위치의 섹션
        mixed = next(c for c in chunks if "성명: 정진완" in c.text)
        self.assertIn("주석 글.", mixed.text)
        self.assertEqual([c.section_id for c in chunks], sorted(c.section_id for c in chunks))

    def test_unknown_strategy(self):
        with self.assertRaises(ValueError):
            chunk_sections(META, [], "semantic")


class BuildChunksTest(unittest.TestCase):
    RCEPT = "20260313000815"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
        self.con = connect()
        self.addCleanup(self.con.close)
        upsert_disclosures(self.con, [{"rcept_no": self.RCEPT, "corp_code": "00254045", "corp_name": "우리은행", "stock_code": "",
                                       "corp_cls": "N", "report_nm": "사업보고서 (2025.12)", "flr_nm": "우리은행", "rcept_dt": "20260313", "rm": ""}])
        self._insert(self.RCEPT + ".xml", 1, "VII. 주주에 관한 사항", "최대주주는 우리금융지주이며 지분율은 100%입니다.", [])
        self._insert(self.RCEPT + ".xml", 2, "VIII. 임원 > 1. 임원 현황", "가. 임원 현황", [EXEC_TABLE])
        self._insert(self.RCEPT + "_00760.xml", 1, "감사보고서", "감사 의견은 적정입니다.", [])

    def _insert(self, file_name, section_id, path, text, tables):
        title = path.split(" > ")[-1]
        self.con.execute(
            "INSERT INTO document_section VALUES (?, ?, '11011', '사업보고서', ?, NULL, 1, NULL, NULL, NULL, ?, ?, false, true, ?, ?, ?, ?)",
            [self.RCEPT, file_name, section_id, title, path, text, json.dumps(tables, ensure_ascii=False), len(text), len(tables)],
        )

    def test_build_is_idempotent_and_main_file_only_by_default(self):
        stats = build_chunks(self.con, [self.RCEPT], ["section", "section_path"])
        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["chunks"]["section"], 3)                 # 글 2 + 표 1
        self.assertEqual(stats["chunks"]["section"], stats["chunks"]["section_path"])
        again = build_chunks(self.con, [self.RCEPT], ["section"])
        self.assertEqual(again["chunks"]["section"], 3)
        n = self.con.execute("SELECT count(*) FROM chunk").fetchone()[0]
        self.assertEqual(n, 6)
        row = self.con.execute("SELECT chunk_id, corp_name, report_nm, kind FROM chunk WHERE strategy='section' ORDER BY seq").fetchall()
        self.assertEqual(row[0], (f"section:{self.RCEPT}:{self.RCEPT}.xml:0", "우리은행", "사업보고서 (2025.12)", "text"))
        self.assertEqual(row[2][3], "table")

    def test_attachments_and_missing_sections(self):
        stats = build_chunks(self.con, [self.RCEPT, "20990101000001"], ["fixed"], attachments=True)
        self.assertEqual(stats["files"], 2)
        self.assertEqual(stats["no_sections"], ["20990101000001"])
        self.assertEqual(self.con.execute("SELECT count(DISTINCT file_name) FROM chunk").fetchone()[0], 2)

    def test_stats_and_eval_and_parsed_rcept_nos(self):
        build_chunks(self.con, [self.RCEPT], ["section"])
        st = chunk_stats(self.con)
        self.assertEqual((st[0]["strategy"], st[0]["chunks"], st[0]["documents"], st[0]["tables"]), ("section", 3, 1, 1))
        q = Path(self.tmp.name) / "q.jsonl"
        q.write_text(json.dumps({"id": "d1", "question": "q", "type": "정형표", "phase": 3, "status": "ready", "answer": "a",
                                 "evidence_rcept_no": [self.RCEPT, "20250318001159"], "evidence_section": None, "source": "t", "note": ""},
                                ensure_ascii=False) + "\n" +
                     json.dumps({"id": "d2", "question": "q", "type": "정형표", "phase": 3, "status": "draft", "answer": "",
                                 "evidence_rcept_no": ["20200101000001"], "evidence_section": None, "source": "t", "note": ""},
                                ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(eval_rcept_nos(q), ["20250318001159", self.RCEPT])  # draft 제외, 정렬
        self.assertEqual(parsed_rcept_nos(self.con, "11011"), [self.RCEPT])
        self.assertEqual(parsed_rcept_nos(self.con, "11013"), [])

    def test_chunk_retriever_scores_path_and_body(self):
        build_chunks(self.con, [self.RCEPT], ["section", "section_path"])
        r = make_retriever("chunk:section", self.con)
        hits = r.search("2025년 말 기준 우리은행의 최대주주는 누구인가?", 5)
        self.assertEqual(r.name, "chunk-section")
        self.assertEqual(hits[0].path, "VII. 주주에 관한 사항")
        self.assertEqual(hit_at_k(hits, [self.RCEPT], 5, "VII. 주주에 관한 사항"), 1)
        # section_path는 꼬리표(회사명·보고서)가 embed_text에 있어 회사명 낱말로도 점수를 얻는다
        r2 = make_retriever("chunk:section_path", self.con)
        self.assertTrue(all(h.score >= 1 for h in r2.search("우리은행 감사", 5)))
        with self.assertRaises(SystemExit):
            make_retriever("chunk:semantic", self.con)


if __name__ == "__main__":
    unittest.main()
