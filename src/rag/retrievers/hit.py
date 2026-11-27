"""검색 결과 한 조각(Hit). 검색기(src/rag/retrievers)·그래프 질의·답변·평가 지표가 모두 이 모양을 주고받는다."""

from dataclasses import dataclass


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
