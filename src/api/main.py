"""FastAPI: 기존 Python 함수를 HTTP로 감싼다 (Phase 3 PR 11, 결정 0013). 새 로직은 없다.

    uvicorn src.api.main:app --port 8000        # 노트북. 그다음 http://127.0.0.1:8000/docs
    API_RETRIEVER=dense:kure-v3:section_path uvicorn ...   # Neo4j 없이 DuckDB 벡터 검색으로

엔드포인트
    GET  /health                                  DuckDB·Neo4j 연결, 임베딩 모델, LLM 키 유무
    POST /ask                                     질문 → 답·인용·근거 링크 (src/rag/ask). use_llm=false면 검색만
                                                  그래프 질문이면 graph(관계표·파생 값·실행 기록)도 (0011)
    GET  /changes?since=&until=&corp=             변화 리포트 (src/reports/changes) JSON
    GET  /corps                                   수집 대상 회사 목록
    GET  /corps/{corp_code}/executives?as_of=     그 날짜의 임원 (src/loaders/as_of)
    GET  /corps/{corp_code}/shareholders?as_of=   그 날짜의 최대주주

자원 관리
    - 임베딩 모델(kure-v3, 2GB)과 Neo4j 드라이버는 서버가 뜰 때 한 번 열어 state에 둔다 (lifespan)
    - DuckDB는 요청마다 읽기 전용으로 열고 바로 닫는다. 서버가 연결을 쥐고 있으면 Dagster의 쓰기가 막힌다 (CLAUDE.md 잠금 규칙)
    - LLM 클라이언트는 요청마다 만든다 (가볍고, 키가 없으면 503으로 알린다)
    - 테스트는 create_app(embedder=, driver=, llm_factory=, picker_factory=)에 가짜를 꽂는다
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Callable

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from eval.retrievers import make_retriever
from src.loaders.as_of import corp_page, executives_as_of, shareholders_as_of, target_corps
from src.loaders.duckdb_store import connect, is_lock_error
from src.rag.ask import VIEWER_URL, ask
from src.reports.changes import CHANGE_KINDS, PCT_THRESHOLD, build_report, summarize

DEFAULT_RETRIEVER = "graph:hybrid:kure-v3"   # 그래프 질문이 아니면 neo4j:hybrid:kure-v3와 같다 (0011)


DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]   # web/ 개발 서버 (vite.config.ts의 포트)


def cors_origins() -> list[str]:
    load_dotenv()
    raw = os.getenv("API_CORS_ORIGINS", "")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()] or DEFAULT_CORS_ORIGINS


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
    corp_code: str | None = None   # 공시를 낸 회사. 화면이 회사 페이지로 이어 준다
    corp_name: str | None = None
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


class GraphRelationOut(BaseModel):
    src: str
    src_corp_code: str | None = None   # 수집 대상 회사일 때만(회사 페이지가 있는 회사). 사람·해외 법인·펀드는 None
    rel: str
    dst: str
    dst_corp_code: str | None = None
    value: str
    valid_from: str | None
    valid_to: str | None
    source_rcept_no: str | None
    url: str | None


class GraphDerivedOut(BaseModel):
    rule: str
    subject: str | None
    value: str


class GraphOut(BaseModel):
    """그래프 템플릿을 썼을 때만. trace = 어떤 규칙·템플릿·빈칸으로 몇 개를 찾았나 (틀린 답을 따라가 보기 위해)."""
    n: int                      # LLM에 넘긴 번호 (조각 뒤 마지막). answer.citations에 있으면 인용된 것
    cited: bool
    trace: dict
    relations: list[GraphRelationOut]
    derived: list[GraphDerivedOut]


class AskResponse(BaseModel):
    question: str
    retriever: str
    answer: AnswerOut | None
    hits: list[HitOut]
    graph: GraphOut | None = None
    graph_error: str | None = None   # 그래프 질의가 실패해 공시 검색만 쓴 경우 그 이유


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


def create_app(embedder=None, driver=None, llm_factory: Callable | None = None, retriever_name: str | None = None,
               picker_factory: Callable | None = None) -> FastAPI:
    s = api_settings()
    retriever_name = retriever_name or s["retriever"]
    needs_neo4j = retriever_name.startswith(("neo4j:", "graph:"))
    needs_embedder = retriever_name.startswith(("neo4j:vector", "neo4j:hybrid", "dense:", "graph:hybrid"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 무거운 것은 여기서 한 번. 테스트가 가짜를 줬으면 그대로 쓴다
        if needs_embedder and app.state.embedder is None:
            from src.rag.embedding import LocalEmbedder
            model = retriever_name.split(":")[2] if retriever_name.startswith(("neo4j:", "graph:")) else retriever_name.split(":")[1]
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
    # 화면(web/, 다른 출처)에서 부를 수 있게. 허용 출처는 .env의 API_CORS_ORIGINS (쉼표로 여러 개). 비우면 노트북의 Vite 개발 서버만 (0013)
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins(), allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    def make_llm():
        if llm_factory is not None:
            return llm_factory()
        from src.rag.llm import OpenAILLM
        try:
            return OpenAILLM()
        except SystemExit as e:  # 키 없음
            raise HTTPException(503, str(e)) from e

    def make_picker():
        """규칙이 못 고른 질문에서 템플릿 번호만 고르는 LLM (0011). 답변 LLM을 쓸 때만 함께 쓴다."""
        if picker_factory is not None:
            return picker_factory()
        from src.graph.picker import LLMPicker
        try:
            return LLMPicker()
        except SystemExit as e:
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
                if llm is not None and hasattr(retriever, "picker"):
                    retriever.picker = make_picker()
                result = ask(req.question, retriever, llm, req.k, con)
            except SystemExit as e:
                raise HTTPException(500, str(e)) from e
            # 회사 페이지로 잇는 데 필요한 것: 조각의 공시를 낸 회사, 관계표의 회사가 수집 대상인지
            corp_of = dict(con.execute("SELECT rcept_no, corp_code FROM disclosure WHERE rcept_no IN (SELECT UNNEST(?))",
                                       [[h.rcept_no for h in result.hits]]).fetchall()) if result.hits else {}
            targets = dict(con.execute("SELECT corp_code, corp_name FROM target_corp").fetchall())
        cited = {n for n, _ in result.cited_hits}
        hits = [HitOut(n=i + 1, rcept_no=h.rcept_no, corp_code=corp_of.get(h.rcept_no), corp_name=targets.get(corp_of.get(h.rcept_no)),
                       path=h.path, label=result.labels[i], score=round(h.score, 4), snippet=h.snippet,
                       url=VIEWER_URL.format(rcept_no=h.rcept_no), cited=(i + 1) in cited) for i, h in enumerate(result.hits)]
        page_of = lambda code: code if code in targets else None   # noqa: E731 — 회사 페이지가 있는 회사만
        a = result.answer
        answer = AnswerOut(text=a.text, citations=a.citations, model=a.model, not_found=a.not_found,
                           input_tokens=a.input_tokens, output_tokens=a.output_tokens) if a else None
        graph = None
        if result.graph is not None:
            g = result.graph
            graph = GraphOut(
                n=result.graph_number, cited=result.graph_cited, trace=g.trace(),
                relations=[GraphRelationOut(src=r["src"], src_corp_code=page_of(r.get("src_corp_code")), rel=r["rel"],
                                            dst=r["dst"], dst_corp_code=page_of(r.get("dst_corp_code")), value=r["value"], valid_from=r["valid_from"],
                                            valid_to=r["valid_to"], source_rcept_no=r["source_rcept_no"],
                                            url=VIEWER_URL.format(rcept_no=r["source_rcept_no"]) if r["source_rcept_no"] else None)
                           for r in g.relations],
                derived=[GraphDerivedOut(rule=d["rule"], subject=d["subject"], value=d["value"]) for d in g.derived])
        return AskResponse(question=req.question, retriever=getattr(retriever, "name", retriever_name), answer=answer, hits=hits,
                           graph=graph, graph_error=result.graph_error)

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

    @app.get("/corps/{corp_code}")
    def get_corp(corp_code: str, as_of: date | None = None) -> dict:
        """회사 페이지 한 번에 (0013): 개요·최신 정기보고서, as_of 날짜의 임원·최대주주·출자, 지주 겸직 표의 자리(0014), 지분 추이.
        화면이 한 요청으로 받고 DuckDB도 한 번만 연다. 모든 표 행에 근거 공시 링크(url)를 붙이고, 생년월은 어디에도 없다."""
        as_of = as_of or date.today()
        with _open_duckdb() as con:
            page = corp_page(con, corp_code, as_of)
        if page is None:
            raise HTTPException(404, f"수집 대상에 없는 corp_code: {corp_code}")
        if page["corp"]["latest_report"]:
            _with_url(page["corp"]["latest_report"])
        for key in ("executives", "shareholders", "investments", "concurrent_positions", "stake_history"):
            page[key] = [_with_url(r) for r in page[key]]
        return page

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
