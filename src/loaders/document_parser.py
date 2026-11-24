"""DART 공시 원문 XML → 섹션 트리 (Phase 3 파서).

역할은 "컴퓨터용 XML을 사람이 읽는 보고서 구조(목차 → 문단과 표)로 바꿔 놓는 것"까지다.
조각으로 자르지 않고(청킹의 일), 표를 문장으로 바꾸지 않고(청킹 실험의 변수), 내용을 해석하지 않는다.

2026-11-24 KB금융 2025 사업보고서(rcept_no 20260313001191)에서 확인한 DART XML(dart4.xsd) 구조:
- ZIP 안에 본문 `{rcept_no}.xml`과 첨부 `{rcept_no}_{코드}.xml`(연결감사보고서 등). 모두 UTF-8
- `DOCUMENT > DOCUMENT-NAME(ACODE=문서 종류) / COMPANY-NAME / SUMMARY / BODY`
- 섹션은 `SECTION-1 > SECTION-2 > SECTION-3`로 중첩. 각 섹션의 첫 `TITLE ATOC="Y"`가 제목.
  단, 제목 174개 중 섹션 태그는 69개뿐이다. 재무제표 주석처럼 섹션 태그 없이 `TITLE ATOC="Y"`만 이어지는 곳이 많아,
  그런 제목은 "감싸는 섹션 깊이 + 1"의 하위 섹션으로 연다
- `ATOCID`는 유일하지 않고 순서대로도 아니다(하위가 1부터 다시 시작) → 섹션 ID는 파일 안 등장 순서. ATOCID는 참고 컬럼
- `AID`(예: CEO-CERT), `AASSOCNOTE`(예: D-0-1-1-0)는 항목 표준 코드로 보여 그대로 저장
- 표: `TABLE-GROUP > TABLE > THEAD/TBODY > TR > TD|TH > TE(글자)`, `TU`는 단위, `COL/COLGROUP`은 너비. 사업보고서 한 권에 표 4,809개
- 무시: IMAGE/IMG/IMG-CAPTION, PGBRK, COL/COLGROUP. 그 밖의 모르는 태그는 안으로 들어가 글자만 모은다

파서는 세 단계다 (docs/decisions/0007-xml-parser.md). 2026-11-24 노트북에서 사업보고서 187건을 내장 파서만으로
돌렸을 때 파일 458개 중 436개(95%)가 실패했다. 원인은 XML 규칙을 어긴 그대로의 `<`와 `&`
(본문 `<P><당기말></P>`, 표 안 `S&P`)였다. DART 원문은 이름만 XML이고 실제로는 HTML처럼 느슨하다.
  1. etree         내장 xml.etree.ElementTree 그대로 (엄격)
  2. etree+escape  엔티티가 아닌 `&` → `&amp;`, 태그 이름이 될 수 없는 글자가 따라오는 `<` → `&lt;`로 바꾼 뒤 1
                   (`<당기말>`을 글자로 살린다. lxml 복구만 쓰면 모르는 태그로 보고 글자를 버릴 수 있다)
  3. lxml          lxml.etree의 recover 모드. 닫히지 않은 태그 등 전처리로 못 잡는 것
어느 단계에서 성공했는지를 document_parse.parser에 남겨 단계별 건수를 알 수 있게 한다.
"글자 → 트리"는 parse_tree() 한 함수다.
"""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

try:
    from lxml import etree as lxml_etree
except ImportError:  # 설치 안 됐으면 3단계를 건너뛴다
    lxml_etree = None

SECTION_RE = re.compile(r"^SECTION-(\d+)$")
IGNORED_TAGS = {"IMAGE", "IMG", "IMG-CAPTION", "PGBRK", "COL", "COLGROUP", "SUMMARY", "EXTRACTION", "FORMULA-VERSION"}
WS_RE = re.compile(r"\s+")

# "재무제표 주석" 아래 섹션은 양이 많아(제목의 절반 이상) 청킹 실험에서 포함/제외 변수가 된다.
NOTE_PATH_RE = re.compile(r"재무제표\s*주석")


@dataclass
class Table:
    caption: str | None
    unit: str | None
    header_rows: int          # 앞에서 몇 행이 열 이름(THEAD)인가
    rows: list[list[str]]     # 2차원 배열. 병합 셀은 값을 복제해 채움

    def to_dict(self) -> dict:
        return {"caption": self.caption, "unit": self.unit, "header_rows": self.header_rows, "rows": self.rows}


@dataclass
class Section:
    section_id: int           # 파일 안 등장 순서 (1부터)
    depth: int                # SECTION-n의 n. 표지는 0, 섹션 태그 없는 제목은 감싸는 깊이 + 1
    title: str
    path: str                 # 상위 제목을 ' > '로 이은 경로
    parent_section_id: int | None
    atoc_id: str | None
    aid: str | None
    assoc_note: str | None
    explicit: bool            # SECTION 태그로 열렸는가 (False = TITLE만으로 연 하위 섹션)
    paragraphs: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)

    @property
    def is_note(self) -> bool:
        return bool(NOTE_PATH_RE.search(self.path))


@dataclass
class ParsedFile:
    file_name: str
    doc_kind: str | None       # DOCUMENT-NAME의 ACODE (예: 00761)
    doc_kind_name: str | None  # DOCUMENT-NAME 글자 (예: 연결감사보고서)
    company_name: str | None
    sections: list[Section]
    parser: str = "etree"      # 어느 단계에서 파싱됐나: etree / etree+escape / lxml


# 엔티티(&amp; &#60; &#x3C;)가 아닌 `&`
STRAY_AMP_RE = re.compile(rb"&(?!(?:[A-Za-z][A-Za-z0-9]*|#\d+|#x[0-9A-Fa-f]+);)")
# 태그 이름·닫는 태그·주석·선언(`<a` `</a` `<!` `<?`)이 아닌 `<` (예: `<당기말>`, `< 10%`)
STRAY_LT_RE = re.compile(rb"<(?![A-Za-z_/!?])")
# 속성값 안의 따옴표. 예: ENG="" KB Insurance Co., Ltd "" (2026-11-24 KB 사업보고서 143,619번째 줄).
# 값의 끝은 "따옴표 뒤에 태그 끝(> />) 또는 다음 속성(NAME=")이 오는 자리"로 본다. 그 전의 따옴표는 값의 일부다.
ATTR_VALUE_RE = re.compile(rb'(\s[A-Za-z_][A-Za-z0-9_\-]*=)"((?:[^<>"]|"(?!\s*(?:/?>|[A-Za-z_][A-Za-z0-9_\-]*=")))*)"')


def _fix_attr_quotes(m: re.Match) -> bytes:
    name, value = m.group(1), m.group(2)
    return name + b'"' + value.replace(b'"', b"&quot;") + b'"' if b'"' in value else m.group(0)


def escape_stray(data: bytes) -> bytes:
    """XML 규칙을 어긴 그대로의 `&`, `<`, 속성값 안 따옴표를 엔티티로 바꾼다. 정상 파일은 그대로다."""
    data = STRAY_LT_RE.sub(b"&lt;", STRAY_AMP_RE.sub(b"&amp;", data))
    return ATTR_VALUE_RE.sub(_fix_attr_quotes, data)


def parse_tree(data: bytes) -> tuple[ET.Element, str]:
    """바이트 → (요소 트리, 성공한 단계). 단계는 모듈 설명 참고. 셋 다 실패하면 마지막 예외를 올린다."""
    try:
        return ET.fromstring(data), "etree"
    except ET.ParseError as first:
        try:
            return ET.fromstring(escape_stray(data)), "etree+escape"
        except ET.ParseError as second:
            if lxml_etree is None:
                raise second from first
            parser = lxml_etree.XMLParser(recover=True, huge_tree=True)
            root = lxml_etree.fromstring(escape_stray(data), parser)
            if root is None:
                raise second from first
            return root, "lxml"


def clean_text(elem: ET.Element) -> str:
    """요소 안 모든 글자를 모아 공백을 하나로 줄인다. 서식 태그(SPAN, TE 등)는 벗겨진다."""
    return WS_RE.sub(" ", "".join(elem.itertext())).strip()


def parse_table(table: ET.Element, caption: str | None, unit: str | None) -> Table:
    """TABLE → 2차원 배열. THEAD 행 수를 header_rows로. COLSPAN/ROWSPAN은 값 복제."""
    header_rows = 0
    row_elems: list[tuple[ET.Element, bool]] = []
    for child in table:
        if child.tag == "THEAD":
            row_elems += [(tr, True) for tr in child.iter("TR")]
        elif child.tag in ("TBODY", "TFOOT"):
            row_elems += [(tr, False) for tr in child.iter("TR")]
        elif child.tag == "TR":
            row_elems.append((child, False))
        elif child.tag == "TU" and unit is None:
            unit = clean_text(child) or None
    for tu in table.iter("TU"):
        if unit is None:
            unit = clean_text(tu) or None

    grid: list[list[str]] = []
    pending: dict[tuple[int, int], str] = {}  # (row, col) → ROWSPAN으로 내려온 값
    for r, (tr, is_header) in enumerate(row_elems):
        row: list[str] = []
        c = 0
        cells = [td for td in tr if isinstance(td.tag, str) and td.tag in ("TD", "TH")]
        for td in cells:
            while (r, c) in pending:
                row.append(pending.pop((r, c)))
                c += 1
            value = clean_text(td)
            colspan = _span(td, "COLSPAN")
            rowspan = _span(td, "ROWSPAN")
            for dc in range(colspan):
                row.append(value)
                for dr in range(1, rowspan):
                    pending[(r + dr, c + dc)] = value
            c += colspan
        while (r, c) in pending:
            row.append(pending.pop((r, c)))
            c += 1
        if is_header:
            header_rows += 1
        grid.append(row)
    return Table(caption=caption, unit=unit, header_rows=header_rows, rows=grid)


def _span(td: ET.Element, attr: str) -> int:
    try:
        return max(1, int(td.get(attr) or 1))
    except ValueError:
        return 1


class _Walker:
    """BODY를 문서 순서로 걸으며 섹션을 열고 닫는다."""

    def __init__(self):
        self.sections: list[Section] = []
        self.stack: list[Section] = []

    def current(self) -> Section:
        if not self.stack:
            self.open(0, "(표지)", None, None, None, explicit=True)
        return self.stack[-1]

    def explicit_depth(self) -> int:
        for s in reversed(self.stack):
            if s.explicit:
                return s.depth
        return 0

    def open(self, depth: int, title: str, atoc_id, aid, assoc_note, explicit: bool) -> Section:
        while self.stack and self.stack[-1].depth >= depth:
            self.stack.pop()
        parent = self.stack[-1] if self.stack else None
        path = f"{parent.path} > {title}" if parent and parent.depth > 0 else title
        section = Section(len(self.sections) + 1, depth, title, path, parent.section_id if parent else None,
                          atoc_id, aid, assoc_note, explicit)
        self.sections.append(section)
        self.stack.append(section)
        return section

    def walk(self, elem: ET.Element, caption: str | None = None, unit: str | None = None) -> None:
        for child in elem:
            tag = child.tag
            if not isinstance(tag, str) or tag in IGNORED_TAGS:  # lxml은 주석·처리 지시도 자식으로 준다
                continue
            m = SECTION_RE.match(tag)
            if m:
                depth = int(m.group(1))
                title_el = next((t for t in child if t.tag == "TITLE"), None)
                title = clean_text(title_el) if title_el is not None else "(제목 없음)"
                self.open(depth, title, _attr(title_el, "ATOCID"), child.get("AID") or _attr(title_el, "AID"),
                          _attr(title_el, "AASSOCNOTE") or child.get("AASSOCNOTE"), explicit=True)
                self.walk(_without(child, title_el))
                while self.stack and self.stack[-1].depth >= depth:
                    self.stack.pop()
            elif tag in ("TITLE", "COVER-TITLE") and child.get("ATOC") == "Y":
                title = clean_text(child)
                if tag == "COVER-TITLE" and len(self.sections) <= 1 and (not self.stack or self.stack[-1].depth == 0):
                    # 표지 제목. 표지 문단이 먼저 나와 "(표지)" 섹션이 이미 열려 있으면 그 제목을 바꾼다
                    cover = self.current()
                    cover.title = cover.path = title
                    cover.atoc_id, cover.aid, cover.assoc_note = child.get("ATOCID"), child.get("AID"), child.get("AASSOCNOTE")
                else:
                    self.open(self.explicit_depth() + 1, title, child.get("ATOCID"), child.get("AID"),
                              child.get("AASSOCNOTE"), explicit=False)
            elif tag == "TITLE":  # 목차에 없는 소제목(ATOC="N")은 문단으로, 표 캡션 후보로
                text = clean_text(child)
                if text:
                    self.current().paragraphs.append(text)
                    caption = text
            elif tag == "P":
                text = clean_text(child)
                if text:
                    self.current().paragraphs.append(text)
            elif tag == "TU":
                unit = clean_text(child) or unit
            elif tag == "TABLE-GROUP":
                self.walk(child, caption, unit)
            elif tag == "TABLE":
                cells = [td for td in child.iter() if isinstance(td.tag, str) and td.tag in ("TD", "TH")]
                if len(cells) <= 1:
                    # 칸이 하나뿐인 표는 표가 아니라 글상자다. DART는 재무제표 주석 본문을 이렇게 쓴다
                    # (2026-11-24 확인: "1. 지배기업의 개요" 본문 전체가 TD 하나 안에 있었음). 문단으로 취급한다
                    for text in _box_paragraphs(cells[0]) if cells else []:
                        self.current().paragraphs.append(text)
                else:
                    self.current().tables.append(parse_table(child, caption, unit))
                caption = None
            else:
                self.walk(child, caption, unit)


def _box_paragraphs(cell: ET.Element) -> list[str]:
    """글상자(칸 하나짜리 표)의 칸 안 글자를 문단 목록으로. P가 있으면 P 단위로, 없으면 통째로 하나."""
    ps = [clean_text(p) for p in cell.iter("P")]
    ps = [p for p in ps if p]
    if ps:
        return ps
    text = clean_text(cell)
    return [text] if text else []


def _attr(elem: ET.Element | None, name: str) -> str | None:
    return elem.get(name) if elem is not None else None


def _without(elem: ET.Element, skip: ET.Element | None) -> list[ET.Element]:
    return [c for c in elem if c is not skip]


def parse_document_xml(file_name: str, data: bytes) -> ParsedFile:
    """원문 XML 한 파일 → ParsedFile. 깨진 XML이면 xml.etree.ElementTree.ParseError가 올라온다."""
    root, method = parse_tree(data)
    name_el = root.find("DOCUMENT-NAME")
    company_el = root.find("COMPANY-NAME")
    body = root.find("BODY")
    walker = _Walker()
    if body is not None:
        walker.walk(body)
    return ParsedFile(
        file_name=file_name,
        doc_kind=name_el.get("ACODE") if name_el is not None else None,
        doc_kind_name=clean_text(name_el) if name_el is not None else None,
        company_name=clean_text(company_el) if company_el is not None else None,
        sections=walker.sections,
        parser=method,
    )


def section_rows(rcept_no: str, parsed: ParsedFile) -> list[tuple]:
    """document_section 테이블 행. 컬럼 순서는 sql/schema.sql과 같다."""
    rows = []
    for s in parsed.sections:
        text = s.text
        rows.append((rcept_no, parsed.file_name, parsed.doc_kind, parsed.doc_kind_name, s.section_id, s.parent_section_id,
                     s.depth, s.atoc_id, s.aid, s.assoc_note, s.title, s.path, s.is_note, s.explicit, text,
                     json.dumps([t.to_dict() for t in s.tables], ensure_ascii=False), len(text), len(s.tables)))
    return rows
