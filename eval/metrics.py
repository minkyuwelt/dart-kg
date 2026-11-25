"""검색 지표. 질문 하나의 검색 결과(순위 있는 조각 목록)와 근거를 비교한다.

- hit@k (접수번호): 상위 k개 조각 중 근거 접수번호의 조각이 하나라도 있으면 1
- hit@k (섹션):     상위 k개 중 근거 접수번호이면서 경로에 evidence_section이 들어 있는 조각이 있으면 1.
                    evidence_section이 없는 질문은 접수번호 기준과 같다
- MRR:              첫 정답 조각의 순위 r에 대해 1/r. 없으면 0
"없음" 유형은 검색 지표를 매기지 않는다 (답은 LLM 단계에서 "모른다"고 해야 정답).
"""

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


def _matches(hit: Hit, evidence_rcept_no: list[str], evidence_section: str | None) -> bool:
    if hit.rcept_no not in evidence_rcept_no:
        return False
    return evidence_section is None or evidence_section in hit.path


def first_rank(hits: list[Hit], evidence_rcept_no: list[str], evidence_section: str | None = None) -> int | None:
    """첫 정답 조각의 순위(1부터). 없으면 None."""
    for i, h in enumerate(hits, 1):
        if _matches(h, evidence_rcept_no, evidence_section):
            return i
    return None


def hit_at_k(hits: list[Hit], evidence_rcept_no: list[str], k: int, evidence_section: str | None = None) -> int:
    r = first_rank(hits[:k], evidence_rcept_no, evidence_section)
    return 1 if r is not None else 0


def reciprocal_rank(hits: list[Hit], evidence_rcept_no: list[str], evidence_section: str | None = None) -> float:
    r = first_rank(hits, evidence_rcept_no, evidence_section)
    return 1.0 / r if r else 0.0


def score_question(hits: list[Hit], evidence_rcept_no: list[str], evidence_section: str | None, k: int) -> dict:
    """질문 하나의 지표 묶음."""
    return {
        "hit_doc": hit_at_k(hits, evidence_rcept_no, k),
        "hit_section": hit_at_k(hits, evidence_rcept_no, k, evidence_section),
        "rr_doc": reciprocal_rank(hits, evidence_rcept_no),
        "rr_section": reciprocal_rank(hits, evidence_rcept_no, evidence_section),
        "first_rank_doc": first_rank(hits, evidence_rcept_no),
    }


def summarize(per_question: list[dict]) -> dict:
    """질문별 지표 → 평균. 빈 목록이면 0."""
    n = len(per_question)
    if n == 0:
        return {"n": 0, "hit_doc": 0.0, "hit_section": 0.0, "mrr_doc": 0.0, "mrr_section": 0.0}
    return {
        "n": n,
        "hit_doc": round(sum(p["hit_doc"] for p in per_question) / n, 3),
        "hit_section": round(sum(p["hit_section"] for p in per_question) / n, 3),
        "mrr_doc": round(sum(p["rr_doc"] for p in per_question) / n, 3),
        "mrr_section": round(sum(p["rr_section"] for p in per_question) / n, 3),
    }
