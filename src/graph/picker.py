"""규칙이 템플릿을 못 고른 질문에서 LLM이 "템플릿 번호만" 고른다 (결정 0011 결정 4의 라우터 ②단계).

LLM이 하는 일은 고르기 하나뿐이다: 질문을 보고 템플릿 목록(1~7) 중 번호, 맞는 게 없으면 0.
- Cypher를 쓰지 않는다 (Text2Cypher 제외 이유는 0011 대안 비교).
- 빈칸(회사·날짜·깊이)은 LLM이 아니라 라우터 규칙이 채운다 (find_corps·parse_as_of). LLM이 회사 이름을 corp_code로 바꾸면 추측이 된다.
- 빈칸을 채울 수 없으면(질문에 회사가 없는데 회사가 필요한 템플릿 등) 그래프를 쓰지 않는다 → RAG만.

비용: 출력은 숫자 하나(상한 200토큰 — 추론형 모델은 생각하는 토큰도 상한에 들어가서 너무 낮추면 빈 답이 된다). 하루 호출 한도는 답변 LLM과 같은 기록(data/llm_calls.json)을 쓴다.
테스트는 complete 함수를 가짜로 바꿔 API를 부르지 않는다.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Callable

from src.graph.router import CorpMention, Plan, plan_for
from src.graph.templates import TEMPLATES

# T8(근거 조각)은 고를 대상이 아니다 — 모든 템플릿 뒤에 자동으로 붙는다
CHOICES = ["holder_chain", "descendants", "ownership_path", "shared_positions", "person_history", "corp_lineage", "facts_at"]

INSTRUCTIONS = """당신은 질문을 그래프 질의 템플릿에 배정하는 분류기입니다. 질의를 직접 쓰지 않고 번호만 고릅니다.
대상 데이터: 한국 금융지주와 자회사의 지분(출자·최대주주)·임원 관계, 회사 이름 이력.
템플릿:
{catalog}
0. 해당 없음 (공시 본문의 숫자·서술을 묻는 질문, 위 관계로 답할 수 없는 질문)
규칙: 확실하지 않으면 0을 고릅니다."""

SCHEMA = {
    "type": "object",
    "properties": {"template": {"type": "integer", "description": "템플릿 번호 0~7"}},
    "required": ["template"],
    "additionalProperties": False,
}


def catalog() -> str:
    return "\n".join(f"{i}. {TEMPLATES[n].title}" for i, n in enumerate(CHOICES, 1))


def parse_choice(raw: str) -> str | None:
    """모델 출력 → 템플릿 이름. 0·범위 밖·깨진 JSON은 None (그래프를 쓰지 않음)."""
    try:
        n = json.loads(raw).get("template")
    except (json.JSONDecodeError, AttributeError):
        return None
    if isinstance(n, int) and 1 <= n <= len(CHOICES):
        return CHOICES[n - 1]
    return None


class LLMPicker:
    """router.route(..., picker=LLMPicker())로 넣는다. complete(instructions, input, schema) -> JSON 문자열."""

    def __init__(self, complete: Callable[[str, str, dict], str] | None = None, api_key: str | None = None):
        self.complete = complete or _openai_complete(api_key)
        self.last_choice: str | None = None

    def __call__(self, question: str, as_of: date, mentions: list[CorpMention]) -> Plan | None:
        raw = self.complete(INSTRUCTIONS.format(catalog=catalog()), "질문: " + question, SCHEMA)
        self.last_choice = parse_choice(raw)
        if self.last_choice is None:
            return None
        return plan_for(self.last_choice, question, as_of, mentions, rule=f"LLM 고르기: {self.last_choice}")


def _openai_complete(api_key: str | None = None) -> Callable[[str, str, dict], str]:
    """답변 LLM과 같은 클라이언트·설정·하루 한도(src/rag/llm.py). api_key를 주면 .env 대신 그 키(API가 요청 헤더로 받은 보는 사람의 키, 0013)."""
    from src.rag.llm import DailyLimit, complete_json, open_client

    client, s = open_client(api_key)
    limit = DailyLimit(s["daily_limit"])

    def complete(instructions: str, text: str, schema: dict) -> str:
        limit.take()
        return complete_json(client, s["model"], instructions, text, schema, "template_choice", 200).output_text

    return complete
