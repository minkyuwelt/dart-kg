// 변화 리포트 (0012): 기간 안에 접수된 정기보고서가 최대주주·임원·출자 관계에 만든 변화. API 응답(/changes)을 보고서별 표로.
// 참고한 꼴: 공시 비교 도구들(BamSEC·SECSift)의 "바뀐 것만 남기고 나머지는 흐리게(blackline)", 변화마다 접수번호·기준일 꼬리표.
// 숫자는 공시 그대로이고, 지분율 변화는 API가 정한 문턱(pct_threshold)을 넘은 것만 온다.
// 두 보기: "관계"(임원·주주·출자 표의 변화, /changes)와 "문장"(전년 같은 보고서와 위험·회계정책·경영진단 절의 문장 비교, /changes/text — 0012 결정 4).
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, API_URL, changes, corps, textChanges, type ChangesResponse, type Corp, type ReportChanges, type SectionDiff, type TextChanges, type TextChangesResponse } from "../api";
import s from "./changes.module.css";

const DAY = 86_400_000;
const iso = (d: Date) => d.toISOString().slice(0, 10);
const today = () => iso(new Date());
const daysAgo = (n: number) => iso(new Date(Date.now() - n * DAY));

// 빠른 기간. "이번 달"·"올해"는 달력 기준, 나머지는 오늘부터 거꾸로
const RANGES: Array<{ label: string; since: () => string }> = [
  { label: "7일", since: () => daysAgo(7) },
  { label: "30일", since: () => daysAgo(30) },
  { label: "이번 달", since: () => today().slice(0, 8) + "01" },
  { label: "올해", since: () => today().slice(0, 4) + "-01-01" },
];

export default function Changes() {
  const [params, setParams] = useSearchParams();
  const since = params.get("since") ?? daysAgo(30);
  const until = params.get("until") ?? today();
  const corp = params.get("corp") ?? "";
  const view = params.get("view") === "text" ? "text" : "relations";

  const [corpList, setCorpList] = useState<Corp[]>([]);

  useEffect(() => {
    const c = new AbortController();
    corps(c.signal).then(setCorpList).catch(() => setCorpList([]));
    return () => c.abort();
  }, []);

  // 조건(기간·회사)이 바뀔 때마다 다시 받는다. 받는 중에는 이전 결과를 흐리게 둔다 (loading은 "지금 요청 중인 조건"으로 판단)
  const key = `${view}|${since}|${until}|${corp}`;
  type Loaded = { key: string; data: ChangesResponse | null; text: TextChangesResponse | null; error: string | null };
  const [loaded, setLoaded] = useState<Loaded>({ key: "", data: null, text: null, error: null });
  useEffect(() => {
    const c = new AbortController();
    const fail = (e: unknown) => {
      if (c.signal.aborted) return;
      setLoaded({ key, data: null, text: null, error: e instanceof ApiError ? `${e.message} (${e.status})` : `API에 닿지 못했습니다. ${API_URL}에 서버가 떠 있는지 확인하세요.` });
    };
    if (view === "text") textChanges(since, until, corp ? [corp] : [], c.signal).then((t) => setLoaded({ key, data: null, text: t, error: null })).catch(fail);
    else changes(since, until, corp ? [corp] : [], c.signal).then((d) => setLoaded({ key, data: d, text: null, error: null })).catch(fail);
    return () => c.abort();
  }, [view, since, until, corp, key]);
  const loading = loaded.key !== key;
  const data = loaded.data;
  const text = loaded.text;
  const error = loaded.key === key ? loaded.error : null;

  function set(next: Record<string, string>) {
    const p = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v) p.set(k, v);
      else p.delete(k);
    }
    setParams(p, { replace: true });
  }

  // 지주 먼저, 그 아래 자회사 — API 순서(role DESC, 이름순)를 지주별로 묶는다
  const grouped = useMemo(() => {
    const holdings = corpList.filter((c) => c.role === "holding");
    return holdings.map((h) => ({ holding: h, subs: corpList.filter((c) => c.parent_corp_code === h.corp_code) }));
  }, [corpList]);

  const withChanges = data?.reports.filter((r) => r.changes.length > 0) ?? [];
  const without = data?.reports.filter((r) => r.changes.length === 0) ?? [];

  return (
    <main>
      <div className={s.toolbar}>
        <div className="field">
          <label htmlFor="since">접수일</label>
          <div className={s.dates}>
            <input id="since" type="date" value={since} max={until} onChange={(e) => set({ since: e.target.value })} />
            <span aria-hidden="true">~</span>
            <input id="until" aria-label="접수일 끝" type="date" value={until} min={since} max={today()} onChange={(e) => set({ until: e.target.value })} />
          </div>
        </div>
        <div className={s.ranges} role="group" aria-label="빠른 기간">
          {RANGES.map((r) => (
            <button key={r.label} type="button" className={`btnQuiet ${since === r.since() && until === today() ? "btnQuietOn" : ""}`}
                    onClick={() => set({ since: r.since(), until: today() })}>
              {r.label}
            </button>
          ))}
        </div>
        <div className="field">
          <label htmlFor="corp">회사</label>
          <select id="corp" value={corp} onChange={(e) => set({ corp: e.target.value })}>
            <option value="">모든 회사</option>
            {grouped.map(({ holding, subs }) => (
              <optgroup key={holding.corp_code} label={holding.corp_name}>
                <option value={holding.corp_code}>{holding.corp_name}</option>
                {subs.map((c) => <option key={c.corp_code} value={c.corp_code}>{c.corp_name}</option>)}
              </optgroup>
            ))}
          </select>
        </div>
      </div>

      <div className={s.views} role="tablist" aria-label="무엇의 변화">
        <button type="button" role="tab" aria-selected={view === "relations"} className={`btnQuiet ${view === "relations" ? "btnQuietOn" : ""}`} onClick={() => set({ view: "" })}>임원·주주·출자</button>
        <button type="button" role="tab" aria-selected={view === "text"} className={`btnQuiet ${view === "text" ? "btnQuietOn" : ""}`} onClick={() => set({ view: "text" })}>보고서 문장</button>
        <span className={s.viewNote}>{view === "text" ? "전년 같은 종류의 보고서와 위험·회계정책·경영진단 절을 문장으로 견줍니다. 숫자·날짜만 바뀐 문장은 세기만 합니다." : "같은 회사의 직전 보고서 표와 견줍니다."}</span>
      </div>

      {error && <p role="alert" className="alert">{error}</p>}
      {loading && !data && !text && <p className="muted" aria-live="polite">보고서를 견주고 있습니다.</p>}

      {text && <TextView t={text} loading={loading} />}

      {data && (
        <div className={loading ? s.stale : undefined} aria-busy={loading}>
          {data.summary.reports === 0 ? (
            <section className="panel"><p className="panelEmpty" style={{ paddingTop: 14 }}>이 기간에 접수된 정기보고서가 없습니다. 정기보고서는 보통 3·5·8·11월에 들어옵니다.</p></section>
          ) : (
            <section className="panel">
              <div className="panelHead">
                <h3>변화 <span className="panelCount">{data.summary.changes}</span></h3>
                <p className="panelNote">
                  보고서 {data.summary.reports_with_changes}/{data.summary.reports}개에서
                  {Object.entries(data.summary.by_kind).map(([k, n]) => <span key={k} className={s.kind}><span className="chip chipQuiet">{kindLabel(data, k)}</span> {n}</span>)}
                </p>
              </div>
              <div className={s.reports}>
                {withChanges.map((r, i) => <Report key={r.rcept_no} r={r} open={i === 0 && withChanges.length <= 3} />)}
              </div>
              {without.length > 0 && (
                <details className={s.quiet}>
                  <summary>변화가 없는 보고서 {without.length}개</summary>
                  <ul>
                    {without.map((r) => (
                      <li key={r.rcept_no}>
                        <a href={r.url} target="_blank" rel="noreferrer">{r.corp_name} {r.report_nm}</a>
                        <span className="muted"> 접수 {r.rcept_dt}{r.prev_stlm_dt ? "" : " · 첫 보고서라 견줄 것이 없음"}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              <p className={s.foot}>지분율 변화는 {data.pct_threshold}%p 이상만</p>
            </section>
          )}
        </div>
      )}
    </main>
  );
}

function TextView({ t, loading }: { t: TextChangesResponse; loading: boolean }) {
  const compared = t.reports.filter((r) => r.prev_rcept_no);
  const changed = compared.filter((r) => r.sections.some((d) => d.status === "changed" || d.status === "added" || d.status === "removed"));
  const quiet = compared.filter((r) => !changed.includes(r));
  const noPrev = t.reports.filter((r) => !r.prev_rcept_no);
  return (
    <div className={loading ? s.stale : undefined} aria-busy={loading}>
      {t.summary.reports === 0 ? (
        <section className="panel"><p className="panelEmpty" style={{ paddingTop: 14 }}>이 기간에 본문이 적재된 정기보고서가 없습니다. 정기보고서는 보통 3·5·8·11월에 들어옵니다.</p></section>
      ) : (
        <section className="panel">
          <div className="panelHead">
            <h3>바뀐 문장 <span className="panelCount">{t.summary.text_changes}</span></h3>
            <p className="panelNote">보고서 {t.summary.compared}/{t.summary.reports}개를 전년과 견줌 · 글이 바뀐 절 {t.summary.sections_changed}개 · 숫자만 바뀐 문장 {t.summary.numbers_only}개</p>
          </div>
          <div className={s.reports}>
            {changed.map((r, i) => <TextReport key={r.rcept_no} r={r} open={i === 0 && changed.length <= 3} />)}
          </div>
          {(quiet.length > 0 || noPrev.length > 0) && (
            <details className={s.quiet}>
              <summary>문장이 바뀌지 않은 보고서 {quiet.length}개{noPrev.length ? `, 견줄 전년 보고서가 없는 것 ${noPrev.length}개` : ""}</summary>
              <ul>
                {quiet.map((r) => (
                  <li key={r.rcept_no}><a href={r.url} target="_blank" rel="noreferrer">{r.corp_name} {r.report_nm}</a>
                    <span className="muted"> 접수 {r.rcept_dt} · 숫자만 바뀐 문장 {r.sections.reduce((n, d) => n + d.numbers_only, 0)}개</span></li>
                ))}
                {noPrev.map((r) => (
                  <li key={r.rcept_no}><a href={r.url} target="_blank" rel="noreferrer">{r.corp_name} {r.report_nm}</a>
                    <span className="muted"> 접수 {r.rcept_dt} · 전년 같은 종류의 보고서가 없음</span></li>
                ))}
              </ul>
            </details>
          )}
          <p className={s.foot}>견준 절: 경로에 {t.section_words.join("·")}이 든 절. 표는 보지 않습니다. 해석은 넣지 않았습니다.</p>
        </section>
      )}
    </div>
  );
}

function TextReport({ r, open }: { r: TextChanges; open: boolean }) {
  const changed = r.sections.filter((d) => d.status === "changed");
  const extra = r.sections.filter((d) => d.status === "added" || d.status === "removed");
  const nText = changed.reduce((n, d) => n + d.text_changes, 0);
  const nNum = r.sections.reduce((n, d) => n + d.numbers_only, 0);
  return (
    <details className={s.report} open={open}>
      <summary className={s.reportRow}>
        <span className={s.reportTitle}><span className={s.reportCorp}>{r.corp_name}</span> <span className={s.reportNm}>{r.report_nm}</span></span>
        <span className={s.reportCounts}>
          <span className={s.count}>바뀐 문장 <b>{nText}</b></span>
          {extra.length > 0 && <span className={s.count}>절 추가·삭제 <b>{extra.length}</b></span>}
          <span className={s.count}>숫자만 <b>{nNum}</b></span>
        </span>
        <span className={s.reportDate}>{r.rcept_dt}</span>
      </summary>
      <div className={s.reportBody}>
        <p className={s.reportMeta}>
          <a href={r.url} target="_blank" rel="noreferrer">이번 보고서</a> ↔ <a href={r.prev_url ?? "#"} target="_blank" rel="noreferrer">전년 {r.prev_report_nm}</a>
        </p>
        {changed.map((d) => <SectionTable key={d.path} d={d} />)}
        {extra.length > 0 && (
          <ul className={s.extra}>
            {extra.map((d) => <li key={d.path}><span className="chip chipQuiet">{d.status === "added" ? "절 추가" : "절 삭제"}</span> {d.path} <span className="muted">문장 {d.sentences_after || d.sentences_before}개</span></li>)}
          </ul>
        )}
      </div>
    </details>
  );
}

const SENTENCES_SHOWN = 12;

function SectionTable({ d }: { d: SectionDiff }) {
  // 절마다 표 하나. 자리가 같은 짝은 한 줄에 전·후, 나머지는 한쪽만. 길면 접어 두고 "더 보기"
  const [all, setAll] = useState(false);
  const rows: Array<{ kind: string; before: string; after: string }> = [
    ...d.replaced.map(([b, a]) => ({ kind: "바뀜", before: b, after: a })),
    ...d.removed.map((b) => ({ kind: "삭제", before: b, after: "" })),
    ...d.added.map((a) => ({ kind: "추가", before: "", after: a })),
  ];
  const shown = all ? rows : rows.slice(0, SENTENCES_SHOWN);
  return (
    <div className={s.section}>
      <p className={s.sectionHead}><span className={s.sectionPath}>{d.path}</span> <span className="muted">바뀐 문장 {d.text_changes}개 · 숫자만 {d.numbers_only}개 · 문장 {d.sentences_before} → {d.sentences_after}{d.parts_before > 1 || d.parts_after > 1 ? ` · 전년 ${d.parts_before}절 ↔ 이번 ${d.parts_after}절을 묶어 견줌` : ""}</span></p>
      <div className="tblWrap">
        <table className={`tbl ${s.textTable}`}>
          <thead><tr><th className={s.thKind}>구분</th><th>전년</th><th>이번</th></tr></thead>
          <tbody>
            {shown.map((row, i) => (
              <tr key={i} className={row.kind === "삭제" ? s.gone : row.kind === "추가" ? s.added : undefined}>
                <td className="dim">{row.kind}</td>
                <td className={s.before}>{row.before || "—"}</td>
                <td className={s.after}>{row.after || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > SENTENCES_SHOWN && !all && <button type="button" className={`btnQuiet ${s.more}`} onClick={() => setAll(true)}>나머지 {rows.length - SENTENCES_SHOWN}문장 더 보기</button>}
    </div>
  );
}

function kindLabel(data: ChangesResponse, kind: string): string {
  for (const r of data.reports) for (const c of r.changes) if (c.kind === kind) return c.kind_label;
  return kind;
}

function Report({ r, open }: { r: ReportChanges; open: boolean }) {
  // 보고서마다 한 줄(회사·보고서·접수일·변화 종류별 건수)만 보이고, 누르면 표가 펼쳐진다 — 보고서 28개를 다 펼치면 한 화면에 안 들어간다
  const counts = new Map<string, number>();
  for (const c of r.changes) counts.set(c.kind_label, (counts.get(c.kind_label) ?? 0) + 1);
  return (
    <details className={s.report} open={open}>
      <summary className={s.reportRow}>
        <span className={s.reportTitle}>
          <span className={s.reportCorp}>{r.corp_name}</span> <span className={s.reportNm}>{r.report_nm}</span>
        </span>
        <span className={s.reportCounts}>
          {[...counts].map(([label, n]) => <span key={label} className={s.count}>{label} <b>{n}</b></span>)}
        </span>
        <span className={s.reportDate}>{r.rcept_dt}</span>
      </summary>
      <div className={s.reportBody}>
        <p className={s.reportMeta}>
          기준일 {r.stlm_dt ?? "?"}{r.prev_stlm_dt ? ` — 직전 ${r.prev_stlm_dt}과 견줌` : ""} ·{" "}
          <a href={r.url} target="_blank" rel="noreferrer">DART에서 보기</a>
        </p>
        <div className="tblWrap">
          <table className={`tbl ${s.table}`}>
            <thead>
              <tr><th className={s.thKind}>변화</th><th>대상</th><th className={s.thVal}>이전</th><th className={s.thVal}>이후</th><th className={s.thNote}>비고</th></tr>
            </thead>
            <tbody>
              {r.changes.map((c, i) => (
                <tr key={i} className={c.kind.endsWith("_gone") ? s.gone : c.kind.endsWith("_new") ? s.added : undefined}>
                  <td className="dim">{c.kind_label}</td>
                  <td className="strong">{c.subject}</td>
                  <td className={s.before}>{c.before || "—"}</td>
                  <td className={s.after}>{c.after || "—"}</td>
                  <td className="dim">{c.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </details>
  );
}
