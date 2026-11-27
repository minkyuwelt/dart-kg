"""검색 결과 한 조각(Hit). 검색기(src/rag/retrievers)·그래프 질의·답변·평가 지표가 모두 이 모양을 주고받는다."""

from __future__ import annotations

from dataclasses import dataclass

SNIPPET_CHARS = 120   # 화면·로그에 보이는 미리보기 길이


@dataclass(frozen=True)
class Hit:
    """검색 결과 한 조각. 조각이 어디서 왔는지(접수번호·파일·섹션)와 점수."""
    rcept_no: str
    file_name: str
    section_id: int
    path: str
    score: float
    snippet: str = ""
    text: str = ""          # 조각 전문 (LLM에 넘길 때 쓴다, 9b). 검색 점수 계산에는 쓰지 않는다

    @classmethod
    def from_record(cls, r, score: float) -> "Hit":
        """Neo4j 레코드·dict(rcept_no, file_name, section_id, path, text) + 점수 → Hit. 미리보기는 글 앞 SNIPPET_CHARS자."""
        text = r["text"] or ""
        return cls(r["rcept_no"], r["file_name"], r["section_id"], r["path"], float(score), text[:SNIPPET_CHARS], text)

    @classmethod
    def from_row(cls, row, score: float) -> "Hit":
        """DuckDB 행 (rcept_no, file_name, section_id, path, <점수>, text) + 점수 → Hit. 5번째 칸은 호출하는 쪽이 score로 넘긴다."""
        text = row[5] or ""
        return cls(row[0], row[1], row[2], row[3], float(score), text[:SNIPPET_CHARS], text)

    def as_record(self) -> dict:
        """from_record의 반대. RRF처럼 dict 목록을 받는 함수에 넘길 때."""
        return {"rcept_no": self.rcept_no, "file_name": self.file_name, "section_id": self.section_id, "path": self.path, "text": self.text}
