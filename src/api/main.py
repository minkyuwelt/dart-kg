"""FastAPI: 기존 Python 함수를 HTTP로 감싼다 (Phase 3 PR 11, 결정 0013). 새 로직은 없다.

    uvicorn src.api.main:app --port 8000        # 노트북. 그다음 http://127.0.0.1:8000/docs
    API_RETRIEVER=dense:kure-v3:section_path uvicorn ...   # Neo4j 없이 DuckDB 벡터 검색으로

엔드포인트
    GET  /health                                  DuckDB·Neo4j 연결, 임베딩 모델, LLM 키 유무
    POST /ask                                     질문 → 답·인용·근거 링크 (src/rag/ask). use_llm=false면 검색만
    GET  /changes?since=&until=&corp=             변화 리포트 (src/reports/changes) JSON
    GET  /corps                                   수집 대상 회사 목록
    GET  /corps/{corp_code}/executives?as_of=     그 날짜의 임원 (src/loaders/as_of)
    GET  /corps/{corp_code}/shareholders?as_of=   그 날짜의 최대주주

자원 관리
    - 임베딩 모델(kure-v3, 2GB)과 Neo4j 드라이버는 서버가 뜰 때 한 번 열어 state에 둔다 (lifespan)
    - DuckDB는 요청마다 읽기 전용으로 열고 바로 닫는다. 서버가 연결을 쥐고 있으면 Dagster의 쓰기가 막힌다 (CLAUDE.md 잠금 규칙)
    - LLM 클라이언트는 요청마다 만든다 (가볍고, 키가 없으면 503으로 알린다)
    - 테스트는 create_app(embedder=, driver=, llm_factory=)에 가짜를 꽂는다
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Callable

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from eval.retrievers import make_retriever
from src.loaders.as_of import executives_as_of, shareholders_as_of, target_corps
from src.loaders.duckdb_store import connect, is_lock_error
from src.rag.ask import VIEWER_URL, ask
from src.reports.changes import CHANGE_KINDS, PCT_THRESHOLD, build_report, summarize

DEFAULT_RETRIEVER = "neo4j:hybrid:kure-v3"


def api_settings() -> dict:
    load_dotenv()
    return {
        "retriever": os.getenv("API_RETRIEVER") or DEFAULT_RETRIEVER,
        "embed_device": os.getenv("API_EMBED_DEVICE") or None,   # 'cpu'로 두면 GPU 없는 서버 상황을 노트북에서 재 볼 수 있다 (0013)
    }


# ----- 요청·응답 모델 -----

class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    k: int = Field(5, ge=1, le=20)
    use_llm: bool = True
    corp_filter: bool = True  # 질문에 회사명이 있으면 그 회사 보고서로 좁힌다 (0009 3단계)


class HitOut(BaseModel):
    n: int
    rcept_no: str
    path: str
    label: str
    score: float
    snippet: str
    url: str
    cited: bool


class AnswerOut(BaseModel):
    text: str
    citations: list[int]
    model: str
    not_found: bool
    input_tokens: int
    output_tokens: int


class AskResponse(BaseModel):
    question: str
    retriever: str
    answer: AnswerOut | None
    hits: list[HitOut]


class ChangeOut(BaseModel):
    kind: str
    kind_label: str
    subject: str
    before: str
    after: str
    note: str


class ReportChangesOut(BaseModel):
    rcept_no: str
    corp_code: str
    corp_name: str
    report_nm: str
    rcept_dt: date
    stlm_dt: date | None
    prev_stlm_dt: date | None
    url: str
    changes: list[ChangeOut]


class ChangesResponse(BaseModel):
    since: date
    until: date
    pct_threshold: float
    summary: dict
    reports: list[ReportChangesOut]


# ----- 앱 -----

def _open_duckdb():
    try:
        return connect(read_only=True)
    except Exception as e:  # 잠금이면 "잠시 뒤 다시"를 503으로
        if is_lock_error(e):
            raise HTTPException(503, "DuckDB가 다른 프로세스(백필·Dagster)에 열려 있습니다. 잠시 뒤 다시 시도하세요.") from e
        raise


def _with_url(row: dict) -> dict:
    row["url"] = VIEWER_URL.format(rcept_no=row["source_rcept_no"])
    return row


def create_app(embedder=None, driver=None, llm_factory: Callable | None = None, retriever_name: str | None = None) -> FastAPI:
    s = api_settings()
    retriever_name = retriever_name or s["retriever"]
    needs_neo4j = retriever_name.startswith("neo4j:")
    needs_embedder = retriever_name.startswith(("neo4j:vector", "neo4j:hybrid", "dense:"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 무거운 것은 여기서 한 번. 테스트가 가짜를 줬으면 그대로 쓴다
        if needs_embedder and app.state.embedder is None:
            from src.rag.embedding import LocalEmbedder
            model = retriever_name.split(":")[2] if retriever_name.startswith("neo4j:") else retriever_name.split(":")[1]
            app.state.embedder = LocalEmbedder(model, device=s["embed_device"], fp16=s["embed_device"] != "cpu")
        if needs_neo4j and app.state.driver is None:
            from src.graph.client import connect as graph_connect
            app.state.driver = graph_connect()
        yield
        if needs_neo4j and app.state.driver is not None:
            app.state.driver.close()

    app = FastAPI(title="dart-kg API", version="0.1", lifespan=lifespan,
                  description="4대 금융지주·자회사 공시를 시간축 위에 꿰어 질문에 근거 공시와 함께 답하는 API. 답은 공시 원문 조각에서만 만들고, 수치는 공시 그대로이며 투자 판단용이 아니다.")
    app.state.embedder, app.state.driver = embedder, driver
    app.state.retriever_name = retriever_name

    def make_llm():
        if llm_factory is not None:
            return llm_factory()
        from src.rag.llm import OpenAILLM
        try:
            return OpenAILLM()
        except SystemExit as e:  # 키 없음
            raise HTTPException(503, str(e)) from e

    @app.get("/health")
    def health() -> dict:
        out = {"retriever": retriever_name, "embedder": getattr(app.state.embedder, "name", None), "duckdb": "ok", "neo4j": "not used",
               "llm_key": bool(os.getenv("OPENAI_API_KEY"))}
        try:
            with _open_duckdb() as con:
                out["documents_with_chunks"] = con.execute("SELECT count(DISTINCT rcept_no) FROM chunk").fetchone()[0]
        except HTTPException as e:
            out["duckdb"] = e.detail
        if needs_neo4j:
            try:
                app.state.driver.verify_connectivity()
                out["neo4j"] = "ok"
            except Exception as e:
                out["neo4j"] = f"연결 실패: {type(e).__name__}"
        return out

    @app.post("/ask", response_model=AskResponse)
    def post_ask(req: AskRequest) -> AskResponse:
        llm = make_llm() if req.use_llm else None
        with _open_duckdb() as con:
            try:
                retriever = make_retriever(retriever_name, con, corp_filter=req.corp_filter, embedder=app.state.embedder, driver=app.state.driver)
                result = ask(req.question, retriever, llm, req.k, con)
            except SystemExit as e:
                raise HTTPException(500, str(e)) from e
        cited = {n for n, _ in result.cited_hits}
        hits = [HitOut(n=i + 1, rcept_no=h.rcept_no, path=h.path, label=result.labels[i], score=round(h.score, 4), snippet=h.snippet,
                       url=VIEWER_URL.format(rcept_no=h.rcept_no), cited=(i + 1) in cited) for i, h in enumerate(result.hits)]
        a = result.answer
        answer = AnswerOut(text=a.text, citations=a.citations, model=a.model, not_found=a.not_found,
                           input_tokens=a.input_tokens, output_tokens=a.output_tokens) if a else None
        return AskResponse(question=req.question, retriever=getattr(retriever, "name", retriever_name), answer=answer, hits=hits)

    @app.get("/changes", response_model=ChangesResponse)
    def get_changes(since: date | None = None, until: date | None = None, days: int = Query(7, ge=1, le=366),
                    corp: list[str] | None = Query(None)) -> ChangesResponse:
        until = until or date.today()
        since = since or until - timedelta(days=days)
        if since > until:
            raise HTTPException(422, "since가 until보다 늦습니다")
        with _open_duckdb() as con:
            results = build_report(con, since, until, corp)
        reports = [ReportChangesOut(rcept_no=r.rcept_no, corp_code=r.corp_code, corp_name=r.corp_name, report_nm=r.report_nm, rcept_dt=r.rcept_dt,
                                    stlm_dt=r.stlm_dt, prev_stlm_dt=r.prev_stlm_dt, url=r.url,
                                    changes=[ChangeOut(kind=c.kind, kind_label=CHANGE_KINDS[c.kind], subject=c.subject, before=c.before, after=c.after, note=c.note)
                                             for c in r.changes]) for r in results]
        return ChangesResponse(since=since, until=until, pct_threshold=PCT_THRESHOLD, summary=summarize(results), reports=reports)

    @app.get("/corps")
    def get_corps() -> list[dict]:
        with _open_duckdb() as con:
            return target_corps(con)

    def _check_corp(con, corp_code: str) -> None:
        if con.execute("SELECT 1 FROM target_corp WHERE corp_code = ?", [corp_code]).fetchone() is None:
            raise HTTPException(404, f"수집 대상에 없는 corp_code: {corp_code}")

    @app.get("/corps/{corp_code}/executives")
    def get_executives(corp_code: str, as_of: date | None = None, outside_only: bool = False) -> dict:
        as_of = as_of or date.today()
        with _open_duckdb() as con:
            _check_corp(con, corp_code)
            rows = [_with_url(r) for r in executives_as_of(con, corp_code, as_of, outside_only)]
        return {"corp_code": corp_code, "as_of": as_of, "outside_only": outside_only, "count": len(rows), "executives": rows}

    @app.get("/corps/{corp_code}/shareholders")
    def get_shareholders(corp_code: str, as_of: date | None = None) -> dict:
        as_of = as_of or date.today()
        with _open_duckdb() as con:
            _check_corp(con, corp_code)
            rows = [_with_url(r) for r in shareholders_as_of(con, corp_code, as_of)]
        return {"corp_code": corp_code, "as_of": as_of, "count": len(rows), "shareholders": rows}

    return app


app = create_app()
