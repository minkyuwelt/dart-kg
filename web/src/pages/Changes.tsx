// 변화 리포트 (0012): 기간 안에 접수된 정기보고서가 최대주주·임원·출자 관계에 만든 변화. API 응답(/changes)을 보고서별 표로.
// 참고한 꼴: 공시 비교 도구들(BamSEC·SECSift)의 "바뀐 것만 남기고 나머지는 흐리게(blackline)", 변화마다 접수번호·기준일 꼬리표.
// 숫자는 공시 그대로이고, 지분율 변화는 API가 정한 문턱(pct_threshold)을 넘은 것만 온다.
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, API_URL, changes, corps, type ChangesResponse, type Corp, type ReportChanges } from "../api";
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

  const [corpList, setCorpList] = useState<Corp[]>([]);

  useEffect(() => {
    const c = new AbortController();
    corps(c.signal).then(setCorpList).catch(() => setCorpList([]));
    return () => c.abort();
  }, []);

  // 조건(기간·회사)이 바뀔 때마다 다시 받는다. 받는 중에는 이전 결과를 흐리게 둔다 (loading은 "지금 요청 중인 조건"으로 판단)
  const key = `${since}|${until}|${corp}`;
  const [loaded, setLoaded] = useState<{ key: string; data: ChangesResponse | null; error: string | null }>({ key: "", data: null, error: null });
  useEffect(() => {
    const c = new AbortController();
    changes(since, until, corp ? [corp] : [], c.signal)
      .then((d) => setLoaded({ key, data: d, error: null }))
      .catch((e) => {
        if (c.signal.aborted) return;
        setLoaded({ key, data: null, error: e instanceof ApiError ? `${e.message} (${e.status})` : `API에 닿지 못했습니다. ${API_URL}에 서버가 떠 있는지 확인하세요.` });
      });
    return () => c.abort();
  }, [since, until, corp, key]);
  const loading = loaded.key !== key;
  const data = loaded.data;
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
    <main className={s.page}>
      <h2 className={s.h2}>변화 리포트</h2>
      <p className={s.lead}>기간 안에 접수된 사업·분기보고서가 직전 보고서와 견주어 최대주주·임원·출자에 무엇을 바꿨는지 봅니다. 사람의 생년월은 적지 않습니다.</p>

      <form className={s.controls} onSubmit={(e) => e.preventDefault()}>
        <div className={s.field}>
          <label htmlFor="since">접수일</label>
          <div className={s.dates}>
            <input id="since" type="date" value={since} max={until} onChange={(e) => set({ since: e.target.value })} />
            <span aria-hidden="true">~</span>
            <input id="until" aria-label="접수일 끝" type="date" value={until} min={since} max={today()} onChange={(e) => set({ until: e.target.value })} />
          </div>
          <div className={s.ranges} role="group" aria-label="빠른 기간">
            {RANGES.map((r) => (
              <button key={r.label} type="button" className={since === r.since() && until === today() ? s.rangeOn : s.range}
                      onClick={() => set({ since: r.since(), until: today() })}>
                {r.label}
              </button>
            ))}
          </div>
        </div>
        <div className={s.field}>
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
      </form>

      {error && <p role="alert" className={s.error}>{error}</p>}
      {loading && !data && <p className={s.muted} aria-live="polite">보고서를 견주고 있습니다.</p>}

      {data && (
        <div className={loading ? s.stale : undefined} aria-busy={loading}>
          <p className={s.summary}>
            {data.summary.reports === 0 ? (
              <>이 기간에 접수된 관계 보고서가 없습니다. 기간을 넓히거나 다른 회사를 고르세요. 정기보고서는 보통 3·5·8·11월에 접수됩니다.</>
            ) : (
              <>
                보고서 {data.summary.reports}개 중 변화가 있는 것 {data.summary.reports_with_changes}개, 변화 {data.summary.changes}건.
                {Object.keys(data.summary.by_kind).length > 0 && (
                  <span className={s.kinds}>
                    {Object.entries(data.summary.by_kind).map(([k, n]) => (
                      <span key={k} className={s.kind}>{kindLabel(data, k)} {n}</span>
                    ))}
                  </span>
                )}
              </>
            )}
          </p>

          {withChanges.map((r) => <Report key={r.rcept_no} r={r} />)}

          {without.length > 0 && (
            <details className={s.quiet}>
              <summary>변화가 없는 보고서 {without.length}개</summary>
              <ul>
                {without.map((r) => (
                  <li key={r.rcept_no}>
                    <a href={r.url} target="_blank" rel="noreferrer">{r.corp_name} {r.report_nm}</a>
                    <span className={s.muted}> 접수 {r.rcept_dt}{r.prev_stlm_dt ? "" : " · 첫 보고서라 견줄 것이 없음"}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}
          <p className={s.foot}>지분율 변화는 {data.pct_threshold}%p 이상만 적습니다. 수치는 공시 그대로이며 투자 판단용이 아닙니다.</p>
        </div>
      )}
    </main>
  );
}

function kindLabel(data: ChangesResponse, kind: string): string {
  for (const r of data.reports) for (const c of r.changes) if (c.kind === kind) return c.kind_label;
  return kind;
}

function Report({ r }: { r: ReportChanges }) {
  return (
    <section className={s.report} aria-label={`${r.corp_name} ${r.report_nm}`}>
      <header className={s.reportHead}>
        <h3 className={s.h3}>
          {r.corp_name} <span className={s.reportNm}>{r.report_nm}</span>
        </h3>
        <p className={s.reportMeta}>
          접수 {r.rcept_dt} · 기준일 {r.stlm_dt ?? "?"}{r.prev_stlm_dt ? ` (직전 ${r.prev_stlm_dt}과 견줌)` : ""} ·{" "}
          <a href={r.url} target="_blank" rel="noreferrer">DART에서 보기</a>
        </p>
      </header>
      <div className={s.tableWrap}>
      <table className={s.table}>
        <thead>
          <tr><th className={s.thKind}>변화</th><th>대상</th><th className={s.thVal}>이전</th><th className={s.thVal}>이후</th><th className={s.thNote}>비고</th></tr>
        </thead>
        <tbody>
          {r.changes.map((c, i) => (
            <tr key={i} className={c.kind.endsWith("_gone") ? s.gone : c.kind.endsWith("_new") ? s.added : undefined}>
              <td className={s.tdKind}>{c.kind_label}</td>
              <td>{c.subject}</td>
              <td className={s.before}>{c.before || "—"}</td>
              <td className={s.after}>{c.after || "—"}</td>
              <td className={s.note}>{c.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </section>
  );
}
