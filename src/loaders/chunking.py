"""섹션(document_section 행) → 조각(chunk). 임베딩 모델이 한 번에 읽을 크기로 자르고 꼬리표를 붙인다. (0008 초안)

왜 글자 수로 자르는가: 토큰 수는 임베딩 모델마다 다르고 모델은 6번 PR(0009)에서 정한다. 그래서 지금은 글자 수(기본 1,000자,
한국어 토큰으로 어림 500~600)로 자르고, 토큰 수는 모델이 정해진 뒤 그 토크나이저로 다시 잰다.

전략 (계획 phase3.md (B)의 ①②③)
- fixed:        섹션을 무시하고 파일 전체 글(표는 문장으로 편 것 포함)을 이어서 1,000자마다 자른다. 기본선.
- section:      섹션 하나가 조각의 울타리. 짧으면 통째로, 길면 문단 경계에서 1,000자 안팎으로 묶고 앞 조각의 마지막 문단을 겹친다.
                표는 글과 따로 조각으로 만든다 (표 하나가 길면 행 단위로 나눔). 표는 쪼개도 행은 쪼개지 않는다.
- section_path: section과 같은 조각에, 임베딩용 글(embed_text) 앞에 "회사명 | 보고서 | 섹션 경로"를 붙인다.
                짧은 조각이 문맥을 가지게 하려는 것. 저장되는 text는 section과 같다.

표를 문장으로 펴는 법 (table_to_lines): 머리행(header_rows)을 열 이름으로 삼아 행마다 "열이름: 값 | 열이름: 값"으로 쓴다.
머리행이 여러 줄이면 같은 열의 이름을 위에서 아래로 이어 붙인다 ("기말잔액 장부가액"). 머리행이 없으면 값만 " | "로 잇는다.
병합으로 복제된 칸(바로 옆과 같은 값)은 한 번만 쓴다. 캡션·단위는 표 조각의 첫 줄 "[표] 캡션 (단위)"에 둔다.

이 모듈은 DuckDB를 모른다. 입력은 SectionData, 출력은 Chunk 목록. DB 읽기·쓰기는 src/loaders/chunks.py.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

MAX_CHARS = 1000
STRATEGIES = ("fixed", "section", "section_path")
SENTENCE_END_RE = re.compile(r"(?<=[.다요음함됨임]\.)\s+|(?<=[.!?])\s+")


@dataclass(frozen=True)
class SectionData:
    """document_section 한 행 중 청킹에 필요한 것."""
    section_id: int
    path: str
    title: str
    is_note: bool
    text: str                 # 문단을 줄바꿈으로 이은 것
    tables: list[dict]        # [{caption, unit, header_rows, rows}]


@dataclass(frozen=True)
class DocMeta:
    rcept_no: str
    file_name: str
    corp_code: str | None
    corp_name: str | None
    report_nm: str | None


@dataclass
class Chunk:
    strategy: str
    seq: int                  # 파일 안 순서 (0부터)
    section_id: int           # 조각이 시작하는 섹션
    path: str
    is_note: bool
    kind: str                 # 'text' | 'table'
    text: str                 # 저장·표시용 본문
    embed_text: str           # 임베딩에 넣는 글 (section_path는 앞에 꼬리표)
    table_index: int | None = None  # 표 조각이면 섹션 안 몇 번째 표인지 (0부터)
    extra: dict = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return len(self.text)

    def text_hash(self) -> str:
        return hashlib.sha256(self.embed_text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 표 → 문장


def _dedupe_adjacent(cells: list[str]) -> list[str]:
    return [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]


def table_header_names(table: dict) -> list[str]:
    """머리행들을 열 이름 하나씩으로 합친다. 열 수는 가장 긴 행 기준."""
    rows = table.get("rows") or []
    header_rows = int(table.get("header_rows") or 0)
    width = max((len(r) for r in rows), default=0)
    names = []
    for c in range(width):
        parts: list[str] = []
        for r in rows[:header_rows]:
            v = r[c].strip() if c < len(r) and r[c] else ""
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        names.append(" ".join(parts))
    return names


def table_to_lines(table: dict) -> tuple[str, list[str]]:
    """표 → (첫 줄, 데이터 행 문장들). 첫 줄은 "[표] 캡션 (단위)". 머리행이 있으면 "열이름: 값", 없으면 값만."""
    rows = table.get("rows") or []
    header_rows = int(table.get("header_rows") or 0)
    head = "[표]"
    if table.get("caption"):
        head += f" {table['caption']}"
    if table.get("unit"):
        head += f" {table['unit']}"
    names = table_header_names(table) if header_rows else []
    lines = []
    for r in rows[header_rows:]:
        cells = [(c or "").strip() for c in r]
        if not any(cells):
            continue
        if names:
            parts = []
            prev = None
            for c, v in enumerate(cells):
                if not v or v == prev and c > 0:
                    prev = v
                    continue
                prev = v
                name = names[c] if c < len(names) else ""
                parts.append(f"{name}: {v}" if name and name != v else v)
            lines.append(" | ".join(parts))
        else:
            lines.append(" | ".join(_dedupe_adjacent([c for c in cells if c])))
    return head, lines


# ---------------------------------------------------------------- 글 자르기


def split_long(paragraph: str, max_chars: int) -> list[str]:
    """한 문단이 max_chars를 넘으면 문장 끝에서 자른다. 문장 끝이 없으면 그냥 자른다."""
    if len(paragraph) <= max_chars:
        return [paragraph]
    sentences = [s for s in SENTENCE_END_RE.split(paragraph) if s]
    out, cur = [], ""
    for s in sentences:
        while len(s) > max_chars:  # 문장 하나가 한도보다 길면 그냥 자른다
            if cur:
                out.append(cur); cur = ""
            out.append(s[:max_chars]); s = s[max_chars:]
        if cur and len(cur) + 1 + len(s) > max_chars:
            out.append(cur); cur = s
        else:
            cur = f"{cur} {s}".strip() if cur else s
    if cur:
        out.append(cur)
    return out


def pack(units: list[str], max_chars: int, overlap: int = 0) -> list[str]:
    """문단(또는 표 행) 목록을 max_chars 안팎으로 묶는다. overlap=1이면 앞 묶음의 마지막 단위를 다음 묶음 앞에 다시 넣는다.

    단위 하나가 한도를 넘으면 split_long으로 먼저 나눈다. 겹친 단위 때문에 한도를 조금 넘는 것은 허용하되(문맥이 더 중요),
    겹칠 단위가 한도의 1/3보다 길면 겹치지 않는다 — 긴 문단을 겹치면 조각이 거의 두 배가 되고 같은 글이 두 번 임베딩된다.
    """
    pieces: list[str] = []
    for u in units:
        pieces.extend(split_long(u, max_chars))
    groups: list[list[str]] = []
    cur: list[str] = []
    size = 0
    n_new = 0  # cur 안에서 겹침으로 가져온 것이 아닌 단위 수
    for p in pieces:
        if cur and size + 1 + len(p) > max_chars:
            groups.append(cur)
            carry = [c for c in cur[-overlap:] if len(c) <= max_chars // 3] if overlap else []
            cur = list(carry); size = sum(len(c) + 1 for c in cur); n_new = 0
        cur.append(p); size += len(p) + 1; n_new += 1
    if cur and n_new:  # 겹친 단위만 남은 묶음(새 내용 없음)은 만들지 않는다
        groups.append(cur)
    return ["\n".join(g) for g in groups]


# ---------------------------------------------------------------- 전략


def _tag(meta: DocMeta, path: str) -> str:
    bits = [b for b in (meta.corp_name, meta.report_nm) if b]
    return " | ".join(bits + [path])


def chunk_sections(meta: DocMeta, sections: list[SectionData], strategy: str, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """파일 하나의 섹션들 → 조각들. 섹션은 section_id 순으로 들어온다고 가정한다."""
    if strategy not in STRATEGIES:
        raise ValueError(f"모르는 전략: {strategy} (가능: {', '.join(STRATEGIES)})")
    if strategy == "fixed":
        return _chunk_fixed(sections, max_chars)
    chunks: list[Chunk] = []
    for s in sections:
        paras = [p for p in s.text.split("\n") if p.strip()]
        for body in pack(paras, max_chars, overlap=1):
            embed = f"{_tag(meta, s.path)}\n{body}" if strategy == "section_path" else body
            chunks.append(Chunk(strategy, len(chunks), s.section_id, s.path, s.is_note, "text", body, embed))
        for ti, t in enumerate(s.tables):
            head, lines = table_to_lines(t)
            if not lines:
                continue
            for body in pack(lines, max_chars - len(head) - 1, overlap=0):
                body = f"{head}\n{body}"
                embed = f"{_tag(meta, s.path)}\n{body}" if strategy == "section_path" else body
                chunks.append(Chunk(strategy, len(chunks), s.section_id, s.path, s.is_note, "table", body, embed, table_index=ti))
    return chunks


def _chunk_fixed(sections: list[SectionData], max_chars: int) -> list[Chunk]:
    """섹션 무시 고정 분할. 글과 표 문장을 문서 순서로 한 줄씩 이어 붙인 뒤 max_chars마다 자른다. 조각의 섹션은 시작 위치의 섹션."""
    stream: list[tuple[int, str, bool]] = []  # (section_id, line, is_note)
    for s in sections:
        for p in s.text.split("\n"):
            if p.strip():
                stream.append((s.section_id, p, s.is_note))
        for t in s.tables:
            head, lines = table_to_lines(t)
            for ln in [head] + lines if lines else []:
                stream.append((s.section_id, ln, s.is_note))
    path_of = {s.section_id: s.path for s in sections}
    chunks: list[Chunk] = []
    buf = ""
    start_sid: int | None = None
    start_note = False
    for sid, line, note in stream:
        pos = 0
        while pos < len(line):
            if start_sid is None:
                start_sid, start_note = sid, note
            room = max_chars - len(buf) - (1 if buf else 0)
            take = line[pos: pos + room]
            buf = f"{buf}\n{take}" if buf else take
            pos += len(take)
            if len(buf) >= max_chars:
                chunks.append(Chunk("fixed", len(chunks), start_sid, path_of[start_sid], start_note, "text", buf, buf))
                buf, start_sid = "", None
    if buf and start_sid is not None:
        chunks.append(Chunk("fixed", len(chunks), start_sid, path_of[start_sid], start_note, "text", buf, buf))
    return chunks
