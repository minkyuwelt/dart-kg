"""LLM 답변 생성 (9b, 결정 0010 2절). "조각 목록 + 질문 → 답 + 인용 번호" 인터페이스 하나로 감싼다.

왜 이렇게 감싸나: 모델은 또 바뀐다(gpt-4o-mini → gpt-6-luna). 모델 이름은 .env의 LLM_MODEL에서 읽고, 호출 코드는 이 파일에만 있다.
테스트와 평가는 FakeLLM(같은 인터페이스)로 돌아 API를 부르지 않는다.

규칙 (CLAUDE.md "추측 없음", usecases D5)
- 답의 모든 문장은 넘겨준 조각에서 나온 것이어야 하고, 문장마다 조각 번호를 인용한다
- 조각에 없는 내용은 "공시에서 찾지 못했습니다"라고 쓰고 인용을 비운다 → 평가 세트 "없음" 유형의 정답
- 구조화 출력(JSON 스키마: answer, citations)으로 받아 프로그램이 인용 번호를 접수번호·링크로 바꾼다

비용 보호
- 질문당 출력 토큰 상한 LLM_MAX_OUTPUT_TOKENS(기본 600), 조각은 k개(기본 5) × 최대 1,000자
- 하루 호출 한도 LLM_DAILY_LIMIT(기본 200): data/llm_calls.json에 날짜별 횟수를 적고 넘으면 멈춘다. 평가 세트 한 바퀴(36~60개)가 하루 한도 안

API 키는 .env의 OPENAI_API_KEY (python-dotenv). 웹 환경에는 키가 없고 호출도 하지 않는다 — 노트북에서만.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from src.loaders.duckdb_store import REPO_ROOT

DEFAULT_MODEL = "gpt-6-luna"
NOT_FOUND = "공시에서 찾지 못했습니다"
CALLS_PATH = REPO_ROOT / "data" / "llm_calls.json"

SYSTEM_PROMPT = """당신은 금융지주 공시(사업보고서) 원문만 근거로 답하는 조사원입니다.
규칙:
1. 아래 번호가 붙은 조각에 적힌 내용만으로 답합니다. 조각에 없는 사실은 쓰지 않습니다.
2. 답의 문장마다 근거 조각 번호를 [1], [2]처럼 붙입니다. 여러 조각이면 [1][3].
3. 질문에 답할 내용이 조각에 없으면 answer를 정확히 "{not_found}"로 쓰고 citations를 빈 목록으로 둡니다. 추측하지 않습니다.
4. 숫자·이름·날짜는 조각에 적힌 그대로 씁니다(단위 포함). 요약은 2~4문장, 한국어.
5. citations에는 실제로 인용한 조각 번호만 넣습니다.
6. "[그래프 관계표]" 조각은 공시의 관계 표(지분·임원)를 정리한 것입니다. 그 안의 "파생 값"은 규칙으로 계산한 값이므로 쓸 때 "계산값"이라고 밝힙니다. 유효 기간이 질문의 기준일과 맞는 행만 씁니다."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "한국어 답. 문장마다 [번호] 인용. 없으면 '공시에서 찾지 못했습니다'"},
        "citations": {"type": "array", "items": {"type": "integer"}, "description": "인용한 조각 번호(1부터)"},
    },
    "required": ["answer", "citations"],
    "additionalProperties": False,
}


@dataclass
class Passage:
    """LLM에 넘기는 조각 하나. 번호는 1부터, 꼬리표(회사 | 보고서 | 섹션)는 label로 따로 준다."""
    number: int
    label: str
    text: str
    max_chars: int | None = None   # None이면 build_user_prompt의 기본값. 그래프 관계표는 길어서 따로 정한다 (0011)


@dataclass
class Answer:
    text: str
    citations: list[int]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def not_found(self) -> bool:
        return self.text.strip().startswith(NOT_FOUND)


class LLM(Protocol):
    name: str

    def answer(self, question: str, passages: list[Passage]) -> Answer: ...


def build_user_prompt(question: str, passages: list[Passage], max_chars: int = 1000) -> str:
    """조각마다 '[n] 꼬리표' 한 줄 + 본문(max_chars까지). 모델이 보는 것은 이 글이 전부다."""
    parts = []
    for p in passages:
        limit = p.max_chars or max_chars
        body = p.text if len(p.text) <= limit else p.text[:limit] + " …"
        parts.append(f"[{p.number}] {p.label}\n{body}")
    return "질문: " + question + "\n\n조각:\n\n" + "\n\n".join(parts)


def parse_answer(raw_json: str, n_passages: int) -> tuple[str, list[int]]:
    """모델 출력(JSON 문자열) → (답, 인용 번호). 범위 밖 번호·중복은 버린다. JSON이 깨지면 글 전체를 답으로, 인용은 비움."""
    try:
        data = json.loads(raw_json)
        text = str(data.get("answer", "")).strip()
        cits = []
        for c in data.get("citations", []) or []:
            if isinstance(c, int) and 1 <= c <= n_passages and c not in cits:
                cits.append(c)
    except (json.JSONDecodeError, AttributeError):
        text, cits = raw_json.strip(), []
    if not text:
        text = NOT_FOUND
    if text.startswith(NOT_FOUND):
        cits = []
    return text, cits


class DailyLimit:
    """하루 호출 횟수를 파일에 적어 한도를 넘지 않게 한다. 프로세스가 여럿이어도 파일 하나를 보므로 대충 맞는다(엄밀한 잠금은 아님)."""

    def __init__(self, limit: int, path: Path = CALLS_PATH):
        self.limit, self.path = limit, path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def used_today(self) -> int:
        return int(self._load().get(date.today().isoformat(), 0))

    def take(self) -> None:
        data = self._load()
        today = date.today().isoformat()
        used = int(data.get(today, 0))
        if used >= self.limit:
            raise SystemExit(f"오늘 LLM 호출 한도({self.limit}회)에 닿았습니다. .env의 LLM_DAILY_LIMIT을 올리거나 내일 다시 (기록: {self.path})")
        data = {today: used + 1}  # 지난 날짜는 버린다
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")


def settings() -> dict:
    load_dotenv()
    return {
        "model": os.getenv("LLM_MODEL") or DEFAULT_MODEL,
        "max_output_tokens": int(os.getenv("LLM_MAX_OUTPUT_TOKENS") or 600),
        "daily_limit": int(os.getenv("LLM_DAILY_LIMIT") or 200),
        "api_key": os.getenv("OPENAI_API_KEY") or "",
    }


class OpenAILLM:
    """OpenAI Responses API + JSON 스키마 구조화 출력. 패키지 import는 함수 안(웹·CI는 키가 없고 부르지 않는다)."""

    def __init__(self, model: str | None = None, max_output_tokens: int | None = None, daily_limit: DailyLimit | None = None, api_key: str | None = None):
        s = settings()
        self.name = model or s["model"]
        self.max_output_tokens = max_output_tokens or s["max_output_tokens"]
        self.daily_limit = daily_limit or DailyLimit(s["daily_limit"])
        key = api_key or s["api_key"]
        if not key:
            raise SystemExit("OPENAI_API_KEY가 .env에 없습니다. .env.example을 보고 적어 주세요 (노트북에서만 호출합니다)")
        from openai import OpenAI  # 노트북 전용 호출

        self.client = OpenAI(api_key=key)

    def answer(self, question: str, passages: list[Passage]) -> Answer:
        self.daily_limit.take()
        resp = self.client.responses.create(
            model=self.name,
            instructions=SYSTEM_PROMPT.format(not_found=NOT_FOUND),
            input=build_user_prompt(question, passages),
            max_output_tokens=self.max_output_tokens,
            text={"format": {"type": "json_schema", "name": "grounded_answer", "schema": ANSWER_SCHEMA, "strict": True}},
        )
        text, cits = parse_answer(resp.output_text, len(passages))
        usage = getattr(resp, "usage", None)
        return Answer(text, cits, self.name, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
