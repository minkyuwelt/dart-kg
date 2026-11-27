"""LLM 답변 생성 (9b, 결정 0010 2절). "조각 목록 + 질문 → 답 + 인용 번호" 인터페이스 하나로 감싼다.

왜 이렇게 감싸나: 모델은 또 바뀐다(gpt-4o-mini → gpt-6-luna). 모델 이름은 .env의 LLM_MODEL에서 읽고, 호출 코드는 이 파일에만 있다.
테스트와 평가는 FakeLLM(같은 인터페이스)로 돌아 API를 부르지 않는다.

규칙 (CLAUDE.md "추측 없음", usecases D5)
- 답의 모든 문장은 넘겨준 조각에서 나온 것이어야 하고, 문장마다 조각 번호를 인용한다
- 조각에 없는 내용은 "공시에서 찾지 못했습니다"라고 쓰고 인용을 비운다 → 평가 세트 "없음" 유형의 정답
- 그래프 관계표가 "없음 —" 판정(겸직 0행, 대조 범위 포함)을 넘기면 "없음"으로 답한다(규칙 8). 그래프는 닫힌 세계(대상 회사의 임원 전부 적재)라 0행이
  정말 없음이고, 조각 검색은 열린 세계(상위 k개)라 못 찾은 것은 모름이다. 그래서 "없음"은 코드가 판정해 적고 LLM은 옮겨 쓰기만 한다 (2026-11-26, g10)
- 이웃 조각(ask.expand_neighbors, 0011)은 꼬리표로 구별되는데 LLM이 보조 자료로 여겨 그 안의 항목을 빠뜨렸다(d033 KB Finansia 85%, 0010 8차 두 판 모두) → 규칙 9: 같은 절의 이어지는 글이니 같은 무게로 쓴다
- 구조화 출력(JSON 스키마: answer, citations)으로 받아 프로그램이 인용 번호를 접수번호·링크로 바꾼다

비용 보호
- 질문당 출력 토큰 상한 LLM_MAX_OUTPUT_TOKENS(기본 2000. 1,000에서는 이웃 조각을 붙인 연혁 답 d033이 잘려 "찾지 못함"으로 떨어졌다, 2026-11-27 8차), 그래프 관계표가 붙은 질문은 LLM_MAX_OUTPUT_TOKENS_GRAPH(기본 4000. 2,000에서는 g05의 45곳 나열이 잘렸다). 조각은 k개(기본 5) × 최대 1,000자
  (2026-11-26 채점표: 상한 600에서 그래프 질문 g05·g12의 답이 잘려 "찾지 못함"으로 떨어짐. gpt-6-luna는 생각하는 토큰도 상한에 들어간다)
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
4. 숫자·이름·날짜는 조각에 적힌 그대로 씁니다(단위 포함). 표에서 '-'는 "해당 없음"이므로, 배당처럼 '-'로 적힌 항목은 "없음(공시에 '-'로 기재)"이라고 답합니다. 요약은 2~4문장, 한국어.
5. citations에는 실제로 인용한 조각 번호만 넣습니다.
6. "[그래프 관계표]" 조각은 공시의 관계 표(지분·임원)를 정리한 것입니다. 그 안의 "파생 값"은 규칙으로 계산한 값이므로 쓸 때 "계산값"이라고 밝힙니다. 유효 기간이 질문의 기준일과 맞는 행만 씁니다.
7. 질문에 기준일(예: "2024년 사업보고서 기준", "2024년 말")이 있고 관계표의 그 기준일에 유효한 행과 다른 조각의 내용이 다르면, 관계표의 행을 답으로 쓰고 조각의 값은 "보고서 제출 시점 기준으로는 …"이라고 덧붙입니다. 사업보고서의 임원 표는 제출일(다음 해 3월) 기준이라 기준일의 사람과 다를 수 있습니다.
8. "있는가?"처럼 존재를 묻는 질문에서 관계표의 파생 값에 "없음 — …" 판정이 있으면 answer를 "없음"으로 시작하고 그 판정의 대조 범위(기준일, 회사 수, 사람 수)를 그대로 옮겨 적은 뒤 관계표 조각을 인용합니다. "{not_found}"는 조각에 답이 없어 모를 때만 씁니다. "없음"(대조해 보니 없음)과 "찾지 못함"(모름)을 섞지 않습니다. 파생 값이 "판정 불가"이면 "{not_found}"로 답합니다. 대조 범위 파생 값에 "셈에서 빠짐"이 있으면 답 끝에 그 회사들은 임원 표가 없어 대조하지 못했다고 한 문장 덧붙입니다.
9. 꼬리표에 "(이웃: [n]의 다음 조각)" 또는 "(이웃: [n]의 앞 조각)"이 붙은 조각은 [n]과 같은 절의 이어지는 글입니다. 질문이 절 전체를 묻으면(연혁, 사업의 개요 등) 그 절의 조각 모두에서 해당 항목을 찾아 빠뜨리지 않고 쓰고, 절 안에 소절(지배회사·종속회사 등)이 나뉘어 있으면 소절 이름을 밝혀 함께 적습니다."""

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
    truncated: bool = False   # 출력 상한에 걸려 답이 잘림 (Responses API status 'incomplete'). 채점표에 표시한다

    @property
    def not_found(self) -> bool:
        return self.text.strip().startswith(NOT_FOUND)


class LLM(Protocol):
    name: str

    def answer(self, question: str, passages: list[Passage], max_output_tokens: int | None = None) -> Answer: ...


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
        "max_output_tokens": int(os.getenv("LLM_MAX_OUTPUT_TOKENS") or 2000),
        "max_output_tokens_graph": int(os.getenv("LLM_MAX_OUTPUT_TOKENS_GRAPH") or 4000),
        "daily_limit": int(os.getenv("LLM_DAILY_LIMIT") or 200),
        "api_key": os.getenv("OPENAI_API_KEY") or "",
    }


class OpenAILLM:
    """OpenAI Responses API + JSON 스키마 구조화 출력. 패키지 import는 함수 안(웹·CI는 키가 없고 부르지 않는다)."""

    def __init__(self, model: str | None = None, max_output_tokens: int | None = None, daily_limit: DailyLimit | None = None, api_key: str | None = None):
        s = settings()
        self.name = model or s["model"]
        self.max_output_tokens = max_output_tokens or s["max_output_tokens"]
        self.max_output_tokens_graph = s["max_output_tokens_graph"]
        self.daily_limit = daily_limit or DailyLimit(s["daily_limit"])
        key = api_key or s["api_key"]
        if not key:
            raise SystemExit("OPENAI_API_KEY가 .env에 없습니다. .env.example을 보고 적어 주세요 (노트북에서만 호출합니다)")
        from openai import OpenAI  # 노트북 전용 호출

        self.client = OpenAI(api_key=key)

    def answer(self, question: str, passages: list[Passage], max_output_tokens: int | None = None) -> Answer:
        """max_output_tokens를 넘기면 그 상한으로 (ask가 그래프 관계표가 붙은 질문에 max_output_tokens_graph를 넘긴다)."""
        self.daily_limit.take()
        resp = self.client.responses.create(
            model=self.name,
            instructions=SYSTEM_PROMPT.format(not_found=NOT_FOUND),
            input=build_user_prompt(question, passages),
            max_output_tokens=max_output_tokens or self.max_output_tokens,
            text={"format": {"type": "json_schema", "name": "grounded_answer", "schema": ANSWER_SCHEMA, "strict": True}},
        )
        text, cits = parse_answer(resp.output_text, len(passages))
        usage = getattr(resp, "usage", None)
        truncated = getattr(resp, "status", None) == "incomplete"
        return Answer(text, cits, self.name, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0, truncated=truncated)
