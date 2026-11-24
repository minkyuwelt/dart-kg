"""Phase 3 원문 파서 테스트. 실제 DART XML(dart4.xsd) 구조를 본뜬 작은 XML로 확인한다 (개인정보 없음, API 없음).

확인하는 것
1) SECTION-n 중첩 → 깊이·경로·부모. 섹션 태그 없는 TITLE ATOC="Y"는 하위 섹션으로
2) 문단: 서식 태그 벗김, 공백 정리, 빈 문단 제외. 소제목(ATOC="N")은 문단이자 표 캡션
3) 표: THEAD 행 수, TE 글자, TU 단위, COLSPAN/ROWSPAN 값 복제
4) 주석 섹션 표시, 첨부 파일의 문서 종류
5) rebuild_sections: ZIP 전체 재생성, 어느 파서 단계로 성공했는지 기록, 다시 돌려도 행 수 같음
6) 파서 3단계 (0007): 정상 → etree, 그대로의 `<`·`&` → etree+escape(글자 보존), 닫히지 않은 태그 → lxml
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.loaders.document_parser import Table, escape_stray, explain_failure, is_prose_table, normalize_encoding, parse_document_xml, parse_table, parse_tree
from src.loaders.documents import raw_context, rebuild_sections, save_document, why_lxml
from src.loaders.duckdb_store import connect
from tests.test_documents import make_zip

MAIN_XML = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="dart4.xsd">
<DOCUMENT-NAME ACODE="11011">사업보고서</DOCUMENT-NAME>
<COMPANY-NAME AREGCIK="00688996">주식회사 KB금융지주</COMPANY-NAME>
<SUMMARY><EXTRACTION ACODE="IFRS_YN">Y</EXTRACTION></SUMMARY>
<BODY ATOCID="6">
<COVER>
<P USERMARK="F-18 ">   주식회사 KB금융지주</P>
<COVER-TITLE ATOC="Y" ATOCID="1">사 업 보 고 서</COVER-TITLE>
<P></P>
</COVER>
<SECTION-1 ACLASS="MANDATORY" AID="CEO-CERT">
<TITLE ATOC="Y" ATOCID="2">【 대표이사 등의 확인 】</TITLE>
<IMAGE><IMG WIDTH="1">확인.jpg</IMG><IMG-CAPTION ATOC="N">확인</IMG-CAPTION></IMAGE>
<PGBRK></PGBRK>
</SECTION-1>
<SECTION-1>
<TITLE ATOC="Y" ATOCID="3">I. 회사의 개요</TITLE>
<P></P>
<SECTION-2>
<TITLE ATOC="Y" AASSOCNOTE="D-0-1-1-0" ATOCID="4">1. 회사의 개요</TITLE>
<P>당사는   <SPAN ACLASS="BOLD">금융지주회사</SPAN>로서
 자회사를 지배합니다.</P>
<P>  </P>
<TITLE ATOC="N">주요 자회사</TITLE>
<TABLE-GROUP ACLASS="NORMAL">
<TU ALIGN="RIGHT">(단위 : 백만원)</TU>
<TABLE ACLASS="NORMAL" WIDTH="600">
<COLGROUP><COL WIDTH="200"/><COL WIDTH="200"/><COL WIDTH="200"/></COLGROUP>
<THEAD><TR ACOPY="Y"><TH ROWSPAN="2"><TE>회사명</TE></TH><TH COLSPAN="2"><TE>지분</TE></TH></TR>
<TR><TH><TE>주식수</TE></TH><TH><TE>지분율</TE></TH></TR></THEAD>
<TBODY>
<TR><TD><TE>KB국민은행</TE></TD><TD><TE>404,379,116</TE></TD><TD><TE>100.0</TE></TD></TR>
<TR><TD><TE>KB증권</TE></TD><TD><TE>298,620,424</TE></TD><TD><TE>100.0</TE></TD></TR>
</TBODY>
</TABLE>
</TABLE-GROUP>
</SECTION-2>
</SECTION-1>
<SECTION-1>
<TITLE ATOC="Y" ATOCID="10">III. 재무에 관한 사항</TITLE>
<SECTION-2>
<TITLE ATOC="Y" ATOCID="134">3. 연결재무제표 주석</TITLE>
<TITLE ATOC="Y" ATOCID="241">1. 지배기업의 개요 (연결)</TITLE>
<P>지배기업 설명.</P>
<TITLE ATOC="Y" ATOCID="242">2. 작성기준 (연결)</TITLE>
<P>작성기준 설명.</P>
</SECTION-2>
<SECTION-2>
<TITLE ATOC="Y" ATOCID="135">4. 별도재무제표</TITLE>
<P>별도 설명.</P>
</SECTION-2>
</SECTION-1>
</BODY>
</DOCUMENT>
"""

ATTACH_XML = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT>
<DOCUMENT-NAME ACODE="00761">연결감사보고서</DOCUMENT-NAME>
<COMPANY-NAME AREGCIK="00688996">주식회사 KB금융지주</COMPANY-NAME>
<BODY>
<COVER><COVER-TITLE ATOC="Y" ATOCID="1">감 사 보 고 서</COVER-TITLE></COVER>
<SECTION-1><TITLE ATOC="Y" ATOCID="2">독립된 감사인의 감사보고서</TITLE><P>감사 의견.</P></SECTION-1>
</BODY>
</DOCUMENT>
"""

BROKEN_XML = '<?xml version="1.0" encoding="utf-8"?><DOCUMENT><BODY><P>닫히지 않음</BODY>'.encode("utf-8")


class ParseDocumentXmlTest(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_document_xml("20260313001191.xml", MAIN_XML.encode("utf-8"))
        self.by_title = {s.title: s for s in self.parsed.sections}

    def test_document_metadata(self):
        self.assertEqual((self.parsed.doc_kind, self.parsed.doc_kind_name, self.parsed.company_name),
                         ("11011", "사업보고서", "주식회사 KB금융지주"))

    def test_sections_depth_path_parent(self):
        titles = [(s.depth, s.title, s.explicit) for s in self.parsed.sections]
        self.assertEqual(titles, [
            (0, "사 업 보 고 서", True),
            (1, "【 대표이사 등의 확인 】", True),
            (1, "I. 회사의 개요", True),
            (2, "1. 회사의 개요", True),
            (1, "III. 재무에 관한 사항", True),
            (2, "3. 연결재무제표 주석", True),
            (3, "1. 지배기업의 개요 (연결)", False),   # 섹션 태그 없는 제목 → 깊이 2+1
            (3, "2. 작성기준 (연결)", False),           # 같은 깊이의 앞 제목을 닫고 새로 열림
            (2, "4. 별도재무제표", True),
        ])
        overview = self.by_title["1. 회사의 개요"]
        self.assertEqual(overview.path, "I. 회사의 개요 > 1. 회사의 개요")
        self.assertEqual(overview.parent_section_id, self.by_title["I. 회사의 개요"].section_id)
        self.assertEqual((overview.atoc_id, overview.assoc_note), ("4", "D-0-1-1-0"))
        self.assertEqual(self.by_title["【 대표이사 등의 확인 】"].aid, "CEO-CERT")
        note = self.by_title["2. 작성기준 (연결)"]
        self.assertEqual(note.path, "III. 재무에 관한 사항 > 3. 연결재무제표 주석 > 2. 작성기준 (연결)")
        self.assertEqual(note.parent_section_id, self.by_title["3. 연결재무제표 주석"].section_id)
        self.assertEqual(note.paragraphs, ["작성기준 설명."])
        self.assertEqual(self.by_title["1. 지배기업의 개요 (연결)"].paragraphs, ["지배기업 설명."])

    def test_paragraph_cleaning_and_cover(self):
        overview = self.by_title["1. 회사의 개요"]
        self.assertEqual(overview.paragraphs, ["당사는 금융지주회사로서 자회사를 지배합니다.", "주요 자회사"])
        self.assertEqual(self.by_title["사 업 보 고 서"].paragraphs, ["주식회사 KB금융지주"])  # 표지 문단, 빈 P 제외
        self.assertEqual(self.by_title["【 대표이사 등의 확인 】"].paragraphs, [])  # 이미지만 있는 섹션

    def test_table_grid_header_unit_caption(self):
        [table] = self.by_title["1. 회사의 개요"].tables
        self.assertEqual(table.caption, "주요 자회사")
        self.assertEqual(table.unit, "(단위 : 백만원)")
        self.assertEqual(table.header_rows, 2)
        self.assertEqual(table.rows, [
            ["회사명", "지분", "지분"],          # COLSPAN 복제
            ["회사명", "주식수", "지분율"],      # ROWSPAN 복제
            ["KB국민은행", "404,379,116", "100.0"],
            ["KB증권", "298,620,424", "100.0"],
        ])

    def test_note_flag(self):
        self.assertTrue(self.by_title["1. 지배기업의 개요 (연결)"].is_note)
        self.assertTrue(self.by_title["3. 연결재무제표 주석"].is_note)
        self.assertFalse(self.by_title["4. 별도재무제표"].is_note)
        self.assertFalse(self.by_title["1. 회사의 개요"].is_note)

    def test_attachment_kind(self):
        att = parse_document_xml("20260313001191_00761.xml", ATTACH_XML.encode("utf-8"))
        self.assertEqual((att.doc_kind, att.doc_kind_name), ("00761", "연결감사보고서"))
        self.assertEqual([s.title for s in att.sections], ["감 사 보 고 서", "독립된 감사인의 감사보고서"])

    def test_rowspan_spanning_into_later_rows(self):
        table, _ = parse_tree(b"""<TABLE><TR><TD ROWSPAN="3"><TE>A</TE></TD><TD><TE>1</TE></TD></TR>
            <TR><TD><TE>2</TE></TD></TR><TR><TD><TE>3</TE></TD></TR></TABLE>""")
        self.assertEqual(parse_table(table, None, None).rows, [["A", "1"], ["A", "2"], ["A", "3"]])


LENIENT_XML = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT><DOCUMENT-NAME ACODE="11011">사업보고서</DOCUMENT-NAME><BODY>
<SECTION-1><TITLE ATOC="Y" ATOCID="1">주석</TITLE>
<P><당기말></P>
<P>신용등급은 S&P 기준이며 비율은 < 10% 입니다. 기호 &amp;는 그대로.</P>
<P><NICE신용평가>-장기 신용등급</P>
<TABLE><TR><TD><TE>S&P</TE></TD><TD><TE>Fitch</TE></TD></TR></TABLE>
<TABLE><TR><TH COLSPAN="8" ENG="" KB Insurance Co., Ltd "">㈜KB라이프생명보험</TH></TR></TABLE>
</SECTION-1>
<SECTION-1><TITLE ATOC="Y" ATOCID="2">3. 연결재무제표 주석</TITLE>
<TITLE ATOC="Y" ATOCID="241">1. 지배기업의 개요 (연결)</TITLE>
<TABLE ACLASS="NORMAL"><TBODY><TR><TD><P>지배기업은 2008년 설립되었습니다.</P><P>본점은 서울입니다.</P></TD></TR></TBODY></TABLE>
<TABLE><TR><TD><TE></TE></TD></TR></TABLE>
<TITLE ATOC="Y" ATOCID="242">3. 중요한 회계정책 (연결)</TITLE>
<TABLE><TBODY>
<TR><TD COLSPAN="3">다음은 연결재무제표 작성에 적용된 중요한 회계정책입니다. 이러한 정책은 일관되게 적용되었습니다.</TD></TR>
<TR><TD>―</TD><TD COLSPAN="2">수준 1 : 동일한 자산이나 부채에 대한 활성시장의 공시가격</TD></TR>
<TR><TD>―</TD><TD COLSPAN="2">수준 2 : 직접적으로 또는 간접적으로 관측 가능한 투입변수</TD></TR>
</TBODY></TABLE>
<TABLE><TBODY><TR><TD>자산총계</TD><TD>1,000</TD></TR><TR><TD>부채총계</TD><TD>800</TD></TR></TBODY></TABLE>
</SECTION-1></BODY></DOCUMENT>"""


class LenientParsingTest(unittest.TestCase):
    """2026-11-24 노트북 실측: 내장 파서만으로는 458개 중 436개 실패 (`<당기말>`, `S&P`). 0007의 3단계."""

    def test_escape_stray_keeps_valid_markup(self):
        self.assertEqual(escape_stray(b"<P>a &amp; b &#60; &#x3C; <!-- c --></P>"), b"<P>a &amp; b &#60; &#x3C; <!-- c --></P>")
        self.assertEqual(escape_stray("<P>S&P <당기말> < 10%</P>".encode("utf-8")),
                         "<P>S&amp;P &lt;당기말&gt; &lt; 10%</P>".encode("utf-8").replace(b"&gt;", b">"))

    def test_clean_file_uses_etree(self):
        self.assertEqual(parse_document_xml("a.xml", MAIN_XML.encode("utf-8")).parser, "etree")

    def test_attr_quotes_fixed_but_normal_attrs_untouched(self):
        self.assertEqual(escape_stray(b'<TH WIDTH="951" COLSPAN="8" ENG="" KB Insurance Co., Ltd "">x</TH>'),
                         b'<TH WIDTH="951" COLSPAN="8" ENG="&quot; KB Insurance Co., Ltd &quot;">x</TH>')
        self.assertEqual(escape_stray(b'<TITLE ATOC="Y" ATOCID="2" ENG="I. Company Overview">t</TITLE>'),
                         b'<TITLE ATOC="Y" ATOCID="2" ENG="I. Company Overview">t</TITLE>')

    def test_tag_like_korean_text_is_escaped_but_real_tags_kept(self):
        # `<NICE신용평가>` `<KB금융지주>`: 영문 뒤에 한글이 붙어 태그처럼 보이지만 글자 (why-lxml 22개)
        self.assertEqual(escape_stray("<P><NICE신용평가>-장기</P>".encode("utf-8")), "<P>&lt;NICE신용평가>-장기</P>".encode("utf-8"))
        # 이름 뒤 공백이 와도 속성 이름이 아니면 글자 (why-lxml 3차: `<upside 시나리오>`)
        self.assertEqual(escape_stray('<TD COLSPAN="7"><upside 시나리오></TD>'.encode("utf-8")), '<TD COLSPAN="7">&lt;upside 시나리오></TD>'.encode("utf-8"))
        self.assertEqual(escape_stray(b'<SECTION-1><TD COLSPAN="2"/><BR/>\n<P>\n</P></SECTION-1>'),
                         b'<SECTION-1><TD COLSPAN="2"/><BR/>\n<P>\n</P></SECTION-1>')
        pf = parse_document_xml("b.xml", LENIENT_XML.encode("utf-8"))
        self.assertEqual(pf.parser, "etree+escape")
        self.assertIn("<NICE신용평가>-장기 신용등급", pf.sections[0].paragraphs)

    def test_cp949_file_is_transcoded(self):
        # 2022년 이전 사업보고서: 선언은 utf-8인데 바이트는 EUC-KR (why-lxml 98개)
        raw = MAIN_XML.encode("cp949")
        data, changed = normalize_encoding(raw)
        self.assertTrue(changed)
        self.assertEqual(data, MAIN_XML.encode("utf-8"))
        self.assertEqual(normalize_encoding(MAIN_XML.encode("utf-8")), (MAIN_XML.encode("utf-8"), False))
        pf = parse_document_xml("a.xml", raw)
        self.assertEqual(pf.parser, "etree+cp949")
        self.assertEqual(pf.company_name, parse_document_xml("a.xml", MAIN_XML.encode("utf-8")).company_name)

    def test_prose_table_becomes_paragraphs_but_data_table_stays(self):
        pf = parse_document_xml("b.xml", LENIENT_XML.encode("utf-8"))
        [policy] = [s for s in pf.sections if s.title.startswith("3. 중요한 회계정책")]
        self.assertEqual(policy.paragraphs, [
            "다음은 연결재무제표 작성에 적용된 중요한 회계정책입니다. 이러한 정책은 일관되게 적용되었습니다.",
            "― 수준 1 : 동일한 자산이나 부채에 대한 활성시장의 공시가격",
            "― 수준 2 : 직접적으로 또는 간접적으로 관측 가능한 투입변수",
        ])
        self.assertEqual([t.rows for t in policy.tables], [[["자산총계", "1,000"], ["부채총계", "800"]]])  # 숫자 표는 표로
        self.assertFalse(is_prose_table(Table(None, None, 1, [["항목", "설명"], ["가", "긴 설명 " * 10]])))  # 머리행이 있으면 표

    def test_stray_lt_and_amp_parsed_with_text_preserved(self):
        pf = parse_document_xml("b.xml", LENIENT_XML.encode("utf-8"))
        self.assertEqual(pf.parser, "etree+escape")
        [section] = [s for s in pf.sections if s.title == "주석"]
        self.assertEqual(section.paragraphs, ["<당기말>", "신용등급은 S&P 기준이며 비율은 < 10% 입니다. 기호 &는 그대로.",
                                              "<NICE신용평가>-장기 신용등급", "㈜KB라이프생명보험"])  # 칸 하나짜리 표(속성 따옴표 깨진 것)는 문단으로
        self.assertEqual([t.rows for t in section.tables], [[["S&P", "Fitch"]]])

    def test_single_cell_table_is_text_box(self):
        # DART는 재무제표 주석 본문을 칸 하나짜리 표 안에 넣는다 → 표가 아니라 문단으로
        pf = parse_document_xml("b.xml", LENIENT_XML.encode("utf-8"))
        [note] = [s for s in pf.sections if s.title.startswith("1. 지배기업")]
        self.assertEqual(note.paragraphs, ["지배기업은 2008년 설립되었습니다.", "본점은 서울입니다."])
        self.assertEqual(note.tables, [])  # 빈 글상자도 표로 세지 않음
        self.assertTrue(note.is_note)

    def test_unclosed_tag_falls_back_to_lxml(self):
        pf = parse_document_xml("c.xml", BROKEN_XML)
        self.assertEqual(pf.parser, "lxml")
        self.assertEqual([p for s in pf.sections for p in s.paragraphs], ["닫히지 않음"])

    def test_raw_context_shows_lines_around_match(self):
        found = raw_context(MAIN_XML.encode("cp949"), "회사의 개요", around=1)
        self.assertEqual(len(found), 3)
        self.assertIn("회사의 개요", found[1][1])
        self.assertEqual(raw_context(b"<A/>", "none"), [])

    def test_explain_failure_points_at_offending_line(self):
        self.assertIsNone(explain_failure(LENIENT_XML.encode("utf-8")))  # 2단계로 되는 파일은 원인 없음
        kind, lineno, snippet = explain_failure(BROKEN_XML)
        self.assertEqual((kind, lineno), ("mismatched tag", 1))
        self.assertIn("</BODY>", snippet)


class RebuildSectionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb"),
                                           "DOCUMENTS_PATH": str(Path(self.tmp.name) / "docs")})
        env.start()
        self.addCleanup(env.stop)

    def test_rebuild_writes_sections_and_records_failures_idempotently(self):
        good = make_zip({"20260313001191.xml": MAIN_XML.encode("utf-8"), "20260313001191_00761.xml": ATTACH_XML.encode("utf-8")})
        mixed = make_zip({"20250314000001.xml": ATTACH_XML.encode("utf-8"), "20250314000001_00001.xml": BROKEN_XML, "readme.txt": b"x"})
        with connect() as con:
            save_document(con, "20260313001191", good)
            save_document(con, "20250314000001", mixed)
            first = rebuild_sections(con)
            second = rebuild_sections(con)  # 다시 돌려도 같은 결과
            n_sections = con.execute("SELECT count(*) FROM document_section").fetchone()[0]
            parse_rows = con.execute("SELECT rcept_no, file_name, ok, doc_kind_name, section_count, table_count, parser FROM document_parse ORDER BY 1, 2").fetchall()
            note_count = con.execute("SELECT count(*) FROM document_section WHERE is_note").fetchone()[0]
            tables = con.execute("SELECT tables FROM document_section WHERE title = '1. 회사의 개요'").fetchone()[0]
            partial = rebuild_sections(con, ["20250314000001"])
            reasons = why_lxml(con)
        self.assertEqual(list(reasons), ["mismatched tag"])
        self.assertEqual([(r, f, line) for r, f, line, _ in reasons["mismatched tag"]], [("20250314000001", "20250314000001_00001.xml", 1)])
        self.assertEqual(first, second)
        self.assertEqual((first["documents"], first["files"], first["files_failed"], first["sections"], first["tables"]), (2, 4, 0, 9 + 2 + 2 + 1, 1))
        self.assertEqual(first["by_parser"], {"etree": 3, "lxml": 1})
        self.assertEqual(n_sections, 14)
        self.assertEqual(parse_rows, [
            ("20250314000001", "20250314000001.xml", True, "연결감사보고서", 2, 0, "etree"),
            ("20250314000001", "20250314000001_00001.xml", True, None, 1, 0, "lxml"),  # 닫히지 않은 태그 → lxml 복구
            ("20260313001191", "20260313001191.xml", True, "사업보고서", 9, 1, "etree"),
            ("20260313001191", "20260313001191_00761.xml", True, "연결감사보고서", 2, 0, "etree"),
        ])
        self.assertEqual(note_count, 3)
        self.assertIn('"header_rows": 2', tables)
        self.assertEqual((partial["documents"], partial["by_parser"]), (1, {"etree": 1, "lxml": 1}))
