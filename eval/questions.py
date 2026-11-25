"""평가 질문 파일(eval/questions.jsonl) 읽기와 형식 검사.

한 줄에 질문 하나(JSON). 칸:
- id              "q001"처럼 고유. 결과 파일에서 질문을 가리키는 열쇠
- question        자연어 질문
- type            정형표 / 서술 / 시점 / 없음 / 다중경로  (유형별 점수를 따로 보기 위해)
- phase           3 (RAG만으로 답할 수 있음) / 4 (그래프가 필요함. 검색이 근거 공시를 찾는지만 잰다)
- status          ready (사람이 뷰어에서 정답·근거를 확인함) / draft (초안. 실행에서 건너뜀)
- answer          정답. type이 "없음"이면 "공시에 없음"
- evidence_rcept_no   근거 공시 접수번호 목록. "없음" 유형은 빈 목록
- evidence_section    (선택) 근거 섹션 경로 또는 그 일부. 예: "VII. 주주에 관한 사항 > 1. 최대주주". 섹션 기준 hit@k에 쓴다
- source          누가 어떻게 만들었나 (예: phase2-Q1, draft-2026-11-25, 사람)
- note            (선택) 메모
- graph_kind      (선택, Phase 4) 그래프 질문의 갈래: 추론 / 계층 / 의미 / 통합 (0011 결정 1). 갈래별 점수를 따로 본다

정답과 접수번호는 **사람이 DART 뷰어에서 확인한 것만** 적는다. 추측으로 적지 않는다 (계획 4절).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from src.loaders.duckdb_store import REPO_ROOT

DEFAULT_QUESTIONS = REPO_ROOT / "eval" / "questions.jsonl"
TYPES = ("정형표", "서술", "시점", "없음", "다중경로")
STATUSES = ("ready", "draft")
GRAPH_KINDS = ("추론", "계층", "의미", "통합")
NO_ANSWER = "공시에 없음"
RCEPT_NO_LEN = 14


class QuestionFormatError(ValueError):
    pass


@dataclass
class Question:
    id: str
    question: str
    type: str
    phase: int
    status: str
    answer: str | None
    evidence_rcept_no: list[str] = field(default_factory=list)
    evidence_section: str | None = None
    source: str | None = None
    note: str | None = None
    graph_kind: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict:
        d = {"id": self.id, "question": self.question, "type": self.type, "phase": self.phase, "status": self.status,
             "answer": self.answer, "evidence_rcept_no": self.evidence_rcept_no, "evidence_section": self.evidence_section,
             "source": self.source}
        if self.note:
            d["note"] = self.note
        if self.graph_kind:
            d["graph_kind"] = self.graph_kind
        return d


def parse_question(raw: dict, where: str = "") -> Question:
    """dict 하나 → Question. 형식이 틀리면 QuestionFormatError (어느 줄인지 where에)."""
    def bad(msg: str):
        return QuestionFormatError(f"{where}{msg}")

    for key in ("id", "question", "type", "phase", "status"):
        if key not in raw:
            raise bad(f"'{key}' 칸이 없습니다")
    if raw["type"] not in TYPES:
        raise bad(f"type은 {TYPES} 중 하나여야 합니다: {raw['type']!r}")
    if raw["status"] not in STATUSES:
        raise bad(f"status는 {STATUSES} 중 하나여야 합니다: {raw['status']!r}")
    if raw["phase"] not in (3, 4):
        raise bad(f"phase는 3 또는 4여야 합니다: {raw['phase']!r}")
    if raw.get("graph_kind") is not None and raw["graph_kind"] not in GRAPH_KINDS:
        raise bad(f"graph_kind는 {GRAPH_KINDS} 중 하나여야 합니다: {raw['graph_kind']!r}")
    evidence = raw.get("evidence_rcept_no") or []
    if not isinstance(evidence, list) or any(not (isinstance(r, str) and len(r) == RCEPT_NO_LEN and r.isdigit()) for r in evidence):
        raise bad(f"evidence_rcept_no는 14자리 숫자 문자열의 목록이어야 합니다: {evidence!r}")
    q = Question(id=str(raw["id"]), question=raw["question"], type=raw["type"], phase=raw["phase"], status=raw["status"],
                 answer=raw.get("answer"), evidence_rcept_no=evidence, evidence_section=raw.get("evidence_section"),
                 source=raw.get("source"), note=raw.get("note"), graph_kind=raw.get("graph_kind"))
    if q.is_ready:
        # ready는 사람이 확인했다는 뜻이므로 정답과 근거가 갖춰져야 한다
        if not q.answer:
            raise bad("status가 ready이면 answer가 있어야 합니다")
        if q.type == "없음":
            if q.evidence_rcept_no:
                raise bad("type이 '없음'이면 evidence_rcept_no는 비어 있어야 합니다")
            if q.answer != NO_ANSWER:
                raise bad(f"type이 '없음'이면 answer는 {NO_ANSWER!r}이어야 합니다")
        elif not q.evidence_rcept_no:
            raise bad("status가 ready이면 evidence_rcept_no가 하나 이상 있어야 합니다")
    return q


def load_questions(path: Path = DEFAULT_QUESTIONS) -> list[Question]:
    """파일 전체를 읽어 검사한다. 빈 줄과 `#`으로 시작하는 줄은 건너뛴다. id가 겹치면 오류."""
    questions, seen = [], set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        where = f"{path.name} {lineno}번째 줄: "
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise QuestionFormatError(f"{where}JSON이 아닙니다 ({e})") from e
        q = parse_question(raw, where)
        if q.id in seen:
            raise QuestionFormatError(f"{where}id가 겹칩니다: {q.id}")
        seen.add(q.id)
        questions.append(q)
    return questions


def dump_questions(questions: list[Question], path: Path) -> None:
    path.write_text("".join(json.dumps(q.to_dict(), ensure_ascii=False) + "\n" for q in questions), encoding="utf-8")
