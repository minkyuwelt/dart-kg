
// 질문 한 페이지 (0013 "첫 화면"). 질문 → 답 + 근거 공시 + (그래프 질문이면) 관계표.
// 화면은 API 응답을 그대로 보여 준다. 답의 [n]과 오른쪽 근거 n이 같은 번호이고, 인용된 근거만 형광펜을 칠한다.
import { useEffect, useRef, useState } from "react";
import { ApiError, API_URL, ask, health, splitCitations, type AskResponse, type Health } from "./api";
import s from "./ask.module.css";

// 평가 세트(eval/questions.jsonl)의 실제 질문. 처음 온 사람이 "무엇을 물을 수 있나"를 보는 자리
const EXAMPLES = [
  "2024년 사업보고서 기준 에이비엘생명보험의 대표이사는 누구인가?",
  "2025년 말 기준 KB금융이 출자한 회사 중 기말 장부가액이 가장 큰 곳은 어디인가?",
  "신한은행의 현재 대표이사는 그동안 어느 계열사에서 어떤 직위를 거쳤나?",
  "2025년 12월 31일 기준 4대 금융지주 중 두 곳 이상에서 동시에 사외이사인 사람이 있는가?",
  "케이비국민카드의 2025년 중 주요 연혁은 무엇인가?",
];

// 그래프 관계 종류(0011 결정 2)의 화면 이름. 모르는 종류는 코드 그대로 보여 준다
const REL_LABEL: Record<string, string> = { OWNS: "출자", HOLDS_POSITION: "임원", FORMERLY: "옛 이름", FILED: "제출" };

export default function App() {
  const [question, setQuestion] = useState("");
  const [useLlm, setUseLlm] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AskResponse | null>(null);
  const [focused, setFocused] = useState<number | null>(null);
  const [srv, setSrv] = useState<Health | null | "down">(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    const c = new AbortController();
    health(c.signal).then(setSrv).catch(() => setSrv("down"));
    return () => c.abort();
  }, []);

  async function submit(q: string) {
    const text = q.trim();
    if (text.length < 2 || loading) return;
    abort.current?.abort();
    const c = new AbortController();
    abort.current = c;
    setLoading(true);
    setError(null);
    setFocused(null);
    try {
      setResult(await ask(text, useLlm, c.signal));
    } catch (e) {
      if (c.signal.aborted) return;
      setResult(null);
      setError(e instanceof ApiError ? `${e.message} (${e.status})` : `API에 닿지 못했습니다. ${API_URL}에 서버가 떠 있는지 확인하세요.`);
    } finally {
      if (!c.signal.aborted) setLoading(false);
    }
  }

  // 답의 [n]: 조각 번호면 오른쪽 근거 n으로, 그래프 번호(조각 뒤 마지막)면 관계표로
  function jumpTo(n: number) {
    setFocused(n);
    const id = result?.graph && n === result.graph.n ? "graph" : `hit-${n}`;
    document.getElementById(id)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  const answer = result?.answer ?? null;
  const graph = result?.graph ?? null;

  return (
    <main className={s.page}>
      <header className={s.top}>
        <div>
          <h1 className={s.brand}>dart-kg</h1>
          <p className={s.tagline}>4대 금융지주와 자회사의 공시를 꿰어, 질문에 근거 공시와 함께 답합니다.</p>
        </div>
        <ServerStatus srv={srv} />
      </header>

      <form
        className={s.ask}
        onSubmit={(e) => {
          e.preventDefault();
          submit(question);
        }}
      >
        <label htmlFor="q" className={s.visuallyHidden}>질문</label>
        <textarea
          id="q"
          className={s.input}
          rows={2}
          maxLength={500}
          placeholder="예: 2023년 3월 시점 신한은행의 대표이사와 최대주주는?"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit(question);
            }
          }}
        />
        <div className={s.askRow}>
          <label className={s.toggle}>
            <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
            답 만들기 (끄면 근거 공시만 찾습니다)
          </label>
          <button type="submit" className={s.button} disabled={loading || question.trim().length < 2}>
            {loading ? "찾는 중" : "묻기"}
          </button>
        </div>
      </form>

      {!result && !loading && !error && (
        <section className={s.examples} aria-label="예시 질문">
          <p className={s.examplesLead}>이런 것을 물을 수 있습니다</p>
          <ul>
            {EXAMPLES.map((q) => (
              <li key={q}>
                <button type="button" className={s.exampleBtn} onClick={() => { setQuestion(q); submit(q); }}>
                  {q}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {error && (
        <p role="alert" className={s.error}>{error}</p>
      )}

      {loading && <p className={s.loading} aria-live="polite">공시를 찾고 있습니다. 답까지 만들면 10초쯤 걸립니다.</p>}

      {result && (
        <div className={s.result}>
          <section className={s.answerCol} aria-label="답">
            <h2 className={s.q}>{result.question}</h2>
            {answer ? (
              <div className={answer.not_found ? s.answerNotFound : s.answer}>
                {splitCitations(answer.text).map((part, i) =>
                  "cite" in part ? (
                    <button key={i} type="button" className={s.cite} onClick={() => jumpTo(part.cite)} title={`근거 ${part.cite} 보기`}>
                      {part.cite}
                    </button>
                  ) : (
                    <span key={i}>{part.text}</span>
                  ),
                )}
              </div>
            ) : (
              <p className={s.noAnswer}>답은 만들지 않았습니다. 오른쪽 근거 공시를 직접 읽어 보세요.</p>
            )}
            <p className={s.caveat}>
              답은 공시 원문 조각에서만 만들고, 수치는 공시 그대로입니다. 투자 판단용이 아닙니다.
              {answer && <> · {answer.model} · 토큰 {answer.input_tokens.toLocaleString()} / {answer.output_tokens.toLocaleString()}</>}
            </p>

            {graph && (
              <section id="graph" className={[s.graph, focused === graph.n ? s.hitFocused : ""].join(" ")} aria-label="관계표">
                <h3 className={s.h3}>
                  <span className={[s.hitN, graph.cited ? s.hitNCited : ""].join(" ")} aria-hidden="true">{graph.n}</span>
                  관계표 <span className={s.h3Sub}>그래프에서 찾은 {graph.relations.length}건{graph.cited ? " · 답이 인용함" : " · 답이 인용하지 않음"}</span>
                </h3>
                {graph.derived.length > 0 && (
                  <ul className={s.derived}>
                    {graph.derived.map((d, i) => (
                      <li key={i}><span className={s.derivedRule}>{d.rule}</span> {d.subject ? `${d.subject}: ` : ""}{d.value}</li>
                    ))}
                  </ul>
                )}
                {graph.relations.length > 0 && (
                  <div className={s.tableWrap}>
                    <table className={s.table}>
                      <thead>
                        <tr><th>주체</th><th>관계</th><th>대상</th><th>값</th><th>유효 기간</th><th>근거</th></tr>
                      </thead>
                      <tbody>
                        {graph.relations.map((r, i) => (
                          <tr key={i}>
                            <td>{r.src}</td><td className={s.nowrap}>{REL_LABEL[r.rel] ?? r.rel}</td><td>{r.dst}</td><td>{r.value}</td>
                            <td className={s.nowrap}>{r.valid_from ?? ""}{r.valid_to ? ` ~ ${r.valid_to}` : r.valid_from ? " ~" : ""}</td>
                            <td>{r.url ? <a href={r.url} target="_blank" rel="noreferrer">{r.source_rcept_no}</a> : ""}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <details className={s.trace}>
                  <summary>어떻게 찾았나</summary>
                  <pre>{JSON.stringify(graph.trace, null, 2)}</pre>
                </details>
              </section>
            )}
            {result.graph_error && <p className={s.graphError}>그래프 질의는 쓰지 못했습니다: {result.graph_error}</p>}
          </section>

          <aside className={s.hitsCol} aria-label="근거 공시">
            <h3 className={s.h3}>
              근거 공시 <span className={s.h3Sub}>{result.hits.length}개 중 인용 {result.hits.filter((h) => h.cited).length}개</span>
            </h3>
            <ol className={s.hits}>
              {result.hits.map((h) => (
                <li
                  key={h.n}
                  id={`hit-${h.n}`}
                  className={[s.hit, h.cited ? s.hitCited : "", focused === h.n ? s.hitFocused : ""].join(" ")}
                >
                  <span className={s.hitN} aria-hidden="true">{h.n}</span>
                  <div className={s.hitBody}>
                    <a className={s.hitLabel} href={h.url} target="_blank" rel="noreferrer">{h.label}</a>
                    <p className={s.hitPath}>{h.path}</p>
                    <p className={s.hitSnippet}>{h.snippet}</p>
                  </div>
                </li>
              ))}
            </ol>
            <p className={s.retriever}>검색 {result.retriever}</p>
          </aside>
        </div>
      )}
    </main>
  );
}

function ServerStatus({ srv }: { srv: Health | null | "down" }) {
  if (srv === null) return <p className={s.status}>서버 확인 중</p>;
  if (srv === "down") return <p className={`${s.status} ${s.statusBad}`}>서버에 닿지 못함 — {API_URL}</p>;
  const bad = srv.duckdb !== "ok" || (srv.neo4j !== "ok" && srv.neo4j !== "not used");
  return (
    <p className={`${s.status} ${bad ? s.statusBad : ""}`}>
      공시 {srv.documents_with_chunks?.toLocaleString() ?? "?"}건 색인
      {srv.llm_key ? "" : " · 답 만들기 꺼짐(LLM 키 없음)"}
      {srv.duckdb !== "ok" ? ` · DuckDB ${srv.duckdb}` : ""}
      {srv.neo4j !== "ok" && srv.neo4j !== "not used" ? ` · Neo4j ${srv.neo4j}` : ""}
    </p>
  );
}
