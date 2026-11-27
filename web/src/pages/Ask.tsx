
// 질문 한 페이지 (0013 "첫 화면"). 질문 → 답 + 근거 공시 + (그래프 질문이면) 관계표.
// 화면은 API 응답을 그대로 보여 준다. 답의 [n]과 오른쪽 근거 n이 같은 번호이고, 인용된 근거만 형광펜을 칠한다.
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ApiError, API_URL, ask, corps, health, splitCitations, type AskResponse, type Corp } from "../api";
import { loadKey, saveKey } from "../key";
import { addToHistory, loadHistory, saveHistory, type Asked } from "../history";
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

// 질문 페이지 아래에는 최근 몇 개만. 전부는 이력 페이지(/history)
const RECENT = 5;

export default function Ask() {
  // 이력 페이지에서 "다시 보기"로 왔으면(location.state.restore = 질문) 그 결과를 처음부터 연 채 시작한다. API는 부르지 않는다
  const location = useLocation();
  const navigate = useNavigate();
  const restore = (location.state as { restore?: string } | null)?.restore;
  const [restored] = useState(() => (restore ? loadHistory().find((a) => a.response.question === restore)?.response ?? null : null));
  useEffect(() => {
    if (restore) navigate(location.pathname, { replace: true, state: null });   // 새로 고침해도 다시 열리지 않게 state를 비운다
  }, [restore, location.pathname, navigate]);
  const [question, setQuestion] = useState(restored?.question ?? "");
  const [useLlm, setUseLlm] = useState(true);
  // 서버에 키가 없으면(공개 서버) 보는 사람의 키가 있어야 답 만들기가 된다. 키는 브라우저에만 (key.ts)
  const [serverKey, setServerKey] = useState<boolean | null>(null);
  const [apiKey, setApiKey] = useState(() => loadKey());
  useEffect(() => { saveKey(apiKey); }, [apiKey]);
  useEffect(() => {
    const c = new AbortController();
    health(c.signal).then((h) => setServerKey(h.llm_key)).catch(() => setServerKey(null));
    return () => c.abort();
  }, []);
  const needKey = serverKey === false;
  const canLlm = !needKey || apiKey.trim().length > 0;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AskResponse | null>(restored);
  const [focused, setFocused] = useState<number | null>(null);
  const abort = useRef<AbortController | null>(null);
  // 물은 질문 이력(브라우저 저장). 결과도 함께 두어 다시 누르면 API를 다시 부르지 않는다
  const [history, setHistory] = useState<Asked[]>(() => loadHistory());
  useEffect(() => { saveHistory(history); }, [history]);
  // 시작 화면의 지주 지도용 회사 목록
  const [corpList, setCorpList] = useState<Corp[]>([]);
  useEffect(() => {
    const c = new AbortController();
    corps(c.signal).then(setCorpList).catch(() => setCorpList([]));
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
      const r = await ask(text, useLlm && canLlm, apiKey.trim() || undefined, c.signal);
      setResult(r);
      setHistory((h) => addToHistory(h, r));
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
    <main>
      <form
        className={`panel ${s.ask}`}
        onSubmit={(e) => {
          e.preventDefault();
          submit(question);
        }}
      >
        <label htmlFor="q" className="visuallyHidden">질문</label>
        <textarea
          id="q"
          className={s.input}
          rows={2}
          maxLength={500}
          placeholder="2023년 3월 시점 신한은행의 대표이사와 최대주주는?"
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
            <input type="checkbox" checked={useLlm && canLlm} disabled={!canLlm} onChange={(e) => setUseLlm(e.target.checked)} />
            답 만들기
          </label>
          {needKey && (
            <label className={s.keyField}>
              <span>OpenAI 키</span>
              <input type="password" autoComplete="off" spellCheck={false} placeholder="sk-… (이 브라우저에만 저장)" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
            </label>
          )}
          <button type="submit" className="btn" disabled={loading || question.trim().length < 2}>
            {loading ? "찾는 중" : "묻기"}
          </button>
        </div>
      </form>

      {!result && !loading && !error && corpList.length > 0 && <GroupMap corpList={corpList} />}

      {!result && !loading && !error && (
        <div className={s.lists}>
          <section className="panel" aria-label="예시 질문">
            <div className="panelHead"><h3>이런 것을 물을 수 있습니다</h3></div>
            <ul className={s.qList}>
              {EXAMPLES.map((q) => (
                <li key={q}>
                  <button type="button" className={s.qBtn} onClick={() => { setQuestion(q); submit(q); }}>{q}</button>
                </li>
              ))}
            </ul>
          </section>
          {history.length > 0 && (
            <section className="panel" aria-label="최근에 물은 것">
              <div className="panelHead">
                <h3>최근에 물은 것</h3>
                {history.length > RECENT && <Link to="/history" className={s.linkBtn}>전부 보기 {history.length}</Link>}
              </div>
              <ul className={s.qList}>
                {history.slice(0, RECENT).map(({ response: r }) => (
                  <li key={r.question}>
                    <button type="button" className={s.qBtn} onClick={() => { setQuestion(r.question); setResult(r); setError(null); }}>{r.question}</button>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}

      {error && <p role="alert" className="alert">{error}</p>}

      {loading && <p className={s.loading} aria-live="polite">공시를 찾고 있습니다. 답까지 만들면 10초쯤 걸립니다.</p>}

      {result && (
        <div className={s.result}>
          <section className={s.answerCol} aria-label="답">
            <div className={`panel ${s.answerPanel}`}>
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
                <p className={s.noAnswer}>답은 만들지 않았습니다. 근거 공시를 읽어 보세요.</p>
              )}
              {answer && <p className={s.caveat}>{answer.model} · 토큰 {answer.input_tokens.toLocaleString()} / {answer.output_tokens.toLocaleString()}</p>}
            </div>

            {graph && (
              <section id="graph" className={["panel", s.graph, focused === graph.n ? s.focusedPanel : ""].join(" ")} aria-label="관계표">
                <div className="panelHead">
                  <h3>
                    <span className={[s.hitN, graph.cited ? s.hitNCited : ""].join(" ")} aria-hidden="true">{graph.n}</span>
                    관계표 <span className="panelCount">{graph.relations.length}</span>
                  </h3>
                  {!graph.cited && <p className="panelNote">답이 인용하지 않음</p>}
                </div>
                {graph.derived.length > 0 && (
                  <ul className={s.derived}>
                    {graph.derived.map((d, i) => (
                      <li key={i}><span className="chip chipQuiet">{d.rule}</span> {d.subject ? `${d.subject}: ` : ""}{d.value}</li>
                    ))}
                  </ul>
                )}
                {graph.relations.length > 0 && (
                  <div className="tblWrap">
                    <table className={`tbl ${s.graphTable}`}>
                      <thead>
                        <tr><th>주체</th><th>관계</th><th>대상</th><th>값</th><th>유효 기간</th><th>근거</th></tr>
                      </thead>
                      <tbody>
                        {graph.relations.map((r, i) => (
                          <tr key={i}>
                            <td className="strong nowrap"><CorpName name={r.src} code={r.src_corp_code} /></td><td className="dim nowrap">{REL_LABEL[r.rel] ?? r.rel}</td><td className="nowrap"><CorpName name={r.dst} code={r.dst_corp_code} /></td><td className="nowrap">{r.value}</td>
                            <td className="dim"><span className="nowrap">{r.valid_from ?? ""}{r.valid_from ? " ~" : ""}</span>{r.valid_to ? <> <span className="nowrap">{r.valid_to}</span></> : null}</td>
                            <td>{r.url && r.source_rcept_no ? <a className="chip chipQuiet" href={r.url} target="_blank" rel="noreferrer" title={`접수번호 ${r.source_rcept_no}`}>{fmtDate(r.source_rcept_no)}</a> : ""}</td>
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

          <aside className={`panel ${s.hitsCol}`} aria-label="근거 공시">
            <div className="panelHead">
              <h3>근거 공시 <span className="panelCount">{result.hits.length}</span></h3>
              <p className="panelNote">인용 {result.hits.filter((h) => h.cited).length}</p>
            </div>
            <ol className={s.hits}>
              {result.hits.map((h) => (
                <li
                  key={h.n}
                  id={`hit-${h.n}`}
                  className={[s.hit, h.cited ? s.hitCited : "", focused === h.n ? s.hitFocused : ""].join(" ")}
                >
                  <span className={s.hitN} aria-hidden="true">{h.n}</span>
                  <div className={s.hitBody}>
                    <span className={s.hitLabel}>
                      {h.corp_code && h.corp_name && h.label.startsWith(h.corp_name + " | ") ? (
                        <><Link to={`/corps/${h.corp_code}`} title="회사 페이지">{h.corp_name}</Link> | <a href={h.url} target="_blank" rel="noreferrer">{h.label.slice(h.corp_name.length + 3)}</a></>
                      ) : (
                        <a href={h.url} target="_blank" rel="noreferrer">{h.label}</a>
                      )}
                    </span>
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

// 시작 화면: 4대 지주와 자회사 59곳. 지주 한 칸에 자회사 칩, 누르면 회사 페이지. "이 도구가 아는 회사"가 첫 화면에 보인다
function GroupMap({ corpList }: { corpList: Corp[] }) {
  const holdings = corpList.filter((c) => c.role === "holding");
  return (
    <section className={`panel ${s.map}`} aria-label="회사 지도">
      {holdings.map((h) => {
        const subs = corpList.filter((c) => c.parent_corp_code === h.corp_code);
        return (
          <div key={h.corp_code} className={s.group}>
            <Link to={`/corps/${h.corp_code}`} className={s.groupName}>{h.corp_name}<span className={s.groupN}>{subs.length}</span></Link>
            <div className={s.groupSubs}>
              {subs.map((c) => <Link key={c.corp_code} to={`/corps/${c.corp_code}`}>{c.corp_name}</Link>)}
            </div>
          </div>
        );
      })}
    </section>
  );
}

// 관계표의 회사 이름: 회사 페이지가 있으면 링크
function CorpName({ name, code }: { name: string; code: string | null }) {
  return code ? <Link to={`/corps/${code}`}>{name}</Link> : <>{name}</>;
}

// 접수번호 앞 8자리 = 접수일
function fmtDate(rcept: string): string {
  const d = rcept.slice(0, 8);
  return `${d.slice(0, 4)}.${d.slice(4, 6)}.${d.slice(6, 8)}`;
}
