// 회사 페이지 (0013 세 번째 페이지). 회사 하나를 고르면 기준일의 임원·최대주주·출자·겸직 표 자리와 지분 추이를 한 장으로.
// API(/corps/{corp_code})가 한 번에 주는 것을 그대로 표로 보여 준다. 숫자 표기(원 → 억 원 등)만 화면에서 한다.
// 돕는 질문(usecases 2절): 1 그 시점 대표·최대주주, 6 이 자회사의 지분율·장부가액이 해마다 어떻게 변했나, 3 겸직.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError, API_URL, corpPage, corps, type Corp, type CorpPage, type Ref } from "../api";
import s from "./corp.module.css";

const today = () => new Date().toISOString().slice(0, 10);

// 원 → 읽기 쉬운 단위. 공시 숫자는 원 단위 정수라 억 원이면 소수 한 자리까지만
function won(v: number | null): string {
  if (v === null || v === undefined) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e12) return `${(v / 1e12).toFixed(2)}조 원`;
  if (abs >= 1e8) return `${(v / 1e8).toFixed(1)}억 원`;
  if (abs >= 1e4) return `${Math.round(v / 1e4).toLocaleString()}만 원`;
  return `${v.toLocaleString()}원`;
}
const pct = (v: number | null) => (v === null || v === undefined ? "—" : `${v}%`);
const num = (v: number | null) => (v === null || v === undefined ? "—" : v.toLocaleString());

export default function CorpPageView() {
  const { corpCode = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const asOf = params.get("as_of") ?? today();

  const [corpList, setCorpList] = useState<Corp[]>([]);
  useEffect(() => {
    const c = new AbortController();
    corps(c.signal).then(setCorpList).catch(() => setCorpList([]));
    return () => c.abort();
  }, []);

  const key = `${corpCode}|${asOf}`;
  const [loaded, setLoaded] = useState<{ key: string; data: CorpPage | null; error: string | null }>({ key: "", data: null, error: null });
  useEffect(() => {
    if (!corpCode) return;
    const c = new AbortController();
    corpPage(corpCode, asOf, c.signal)
      .then((d) => setLoaded({ key, data: d, error: null }))
      .catch((e) => {
        if (c.signal.aborted) return;
        setLoaded({ key, data: null, error: e instanceof ApiError ? `${e.message} (${e.status})` : `API에 닿지 못했습니다. ${API_URL}에 서버가 떠 있는지 확인하세요.` });
      });
    return () => c.abort();
  }, [corpCode, asOf, key]);
  const loading = Boolean(corpCode) && loaded.key !== key;
  const data = loaded.key === key ? loaded.data : null;
  const error = loaded.key === key ? loaded.error : null;

  const grouped = useMemo(() => {
    const holdings = corpList.filter((c) => c.role === "holding");
    return holdings.map((h) => ({ holding: h, subs: corpList.filter((c) => c.parent_corp_code === h.corp_code) }));
  }, [corpList]);

  function go(next: string) {
    navigate(`/corps/${next}${asOf !== today() ? `?as_of=${asOf}` : ""}`);
  }
  function setAsOf(v: string) {
    const p = new URLSearchParams(params);
    if (v && v !== today()) p.set("as_of", v);
    else p.delete("as_of");
    setParams(p, { replace: true });
  }

  return (
    <main className={s.page}>
      <h2 className={s.h2}>회사</h2>
      <p className={s.lead}>회사 하나를 골라 기준일의 임원·최대주주·출자와, 지주가 적은 이 회사 지분의 해마다 변화를 봅니다. 모든 행에 근거 공시가 붙고, 사람의 생년월은 적지 않습니다.</p>

      <form className={s.controls} onSubmit={(e) => e.preventDefault()}>
        <div className={s.field}>
          <label htmlFor="corp">회사</label>
          <select id="corp" value={corpCode} onChange={(e) => go(e.target.value)}>
            <option value="">고르세요</option>
            {grouped.map(({ holding, subs }) => (
              <optgroup key={holding.corp_code} label={holding.corp_name}>
                <option value={holding.corp_code}>{holding.corp_name}</option>
                {subs.map((c) => <option key={c.corp_code} value={c.corp_code}>{c.corp_name}</option>)}
              </optgroup>
            ))}
          </select>
        </div>
        <div className={s.field}>
          <label htmlFor="as_of">기준일</label>
          <input id="as_of" type="date" value={asOf} max={today()} onChange={(e) => setAsOf(e.target.value)} />
        </div>
      </form>

      {!corpCode && corpList.length > 0 && (
        <nav className={s.pick} aria-label="지주">
          {grouped.map(({ holding, subs }) => (
            <div key={holding.corp_code} className={s.pickGroup}>
              <Link to={`/corps/${holding.corp_code}`} className={s.pickHolding}>{holding.corp_name}</Link>
              <span className={s.pickSubs}>{subs.map((c) => <Link key={c.corp_code} to={`/corps/${c.corp_code}`}>{c.corp_name}</Link>)}</span>
            </div>
          ))}
        </nav>
      )}

      {error && <p role="alert" className={s.error}>{error}</p>}
      {loading && !data && <p className={s.muted} aria-live="polite">불러오고 있습니다.</p>}

      {data && <Body data={data} />}
    </main>
  );
}

function Body({ data }: { data: CorpPage }) {
  const { corp } = data;
  const noOwnTable = data.executives.length === 0 && data.concurrent_positions.length > 0;
  return (
    <div>
      <header className={s.corpHead}>
        <h3 className={s.corpName}>{corp.corp_name}</h3>
        <p className={s.corpMeta}>
          {corp.role === "holding" ? "지주회사" : <>직속 자회사 · 지주 <Link to={`/corps/${corp.parent_corp_code}`}>{corp.parent_corp_name}</Link>{corp.stake_pct !== null ? ` ${corp.stake_pct}%` : ""}</>}
          {corp.stock_code ? ` · 상장 ${corp.stock_code}` : " · 비상장"} · 고유번호 {corp.corp_code}
          {corp.latest_report ? (
            <> · 최신 정기보고서 <a href={corp.latest_report.url} target="_blank" rel="noreferrer">{corp.latest_report.report_nm}</a> (접수 {corp.latest_report.rcept_dt})</>
          ) : (
            <> · 정기보고서 없음(감사보고서만 내는 회사라 임원·최대주주 표가 없습니다)</>
          )}
        </p>
        <p className={s.asOf}>아래 임원·최대주주·출자는 <strong>{data.as_of}</strong> 기준으로 참이었던 행입니다.</p>
      </header>

      <Section title="지분 추이" count={data.stake_history.length} empty="수집 대상 회사의 출자 표에 이 회사가 없습니다(지주회사이거나, 이름이 이어지지 않은 경우)."
               note="지주(또는 다른 수집 대상 회사)의 사업보고서 타법인 출자현황이 적은 이 회사의 기말 지분율·장부가액을 기준일순으로.">
        <table className={s.table}>
          <thead><tr><th>기준일</th><th>출자한 회사</th><th>주식 종류</th><th className={s.num}>주식 수</th><th className={s.num}>지분율</th><th className={s.num}>장부가액</th><th>근거</th></tr></thead>
          <tbody>
            {data.stake_history.map((h, i) => (
              <tr key={i}>
                <td>{h.stlm_dt}</td>
                <td><Link to={`/corps/${h.investor_corp_code}`}>{h.investor_corp_name}</Link></td>
                <td className={s.dim}>{h.share_class ?? "—"}</td>
                <td className={s.num}>{num(h.shares)}</td>
                <td className={s.num}>{pct(h.pct)}</td>
                <td className={s.num}>{won(h.book_value)}</td>
                <td><Src r={h} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="최대주주와 특수관계인" count={data.shareholders.length} empty="이 날짜에 유효한 최대주주 표가 없습니다.">
        <table className={s.table}>
          <thead><tr><th>이름</th><th>관계</th><th>주식 종류</th><th className={s.num}>주식 수</th><th className={s.num}>지분율</th><th>근거</th></tr></thead>
          <tbody>
            {data.shareholders.map((h, i) => (
              <tr key={i}>
                <td>{h.holder_corp_code ? <Link to={`/corps/${h.holder_corp_code}`}>{h.nm}</Link> : h.nm}</td>
                <td className={s.dim}>{h.relate ?? "—"}</td>
                <td className={s.dim}>{h.stock_knd ?? "—"}</td>
                <td className={s.num}>{num(h.shares)}</td>
                <td className={s.num}>{pct(h.pct)}</td>
                <td><Src r={h} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="임원" count={data.executives.length}
               empty={noOwnTable ? "이 회사는 임원 표를 내지 않습니다. 아래 '지주 겸직 표의 자리'가 아는 전부입니다." : "이 날짜에 유효한 임원 표가 없습니다."}
               note="등기구분·직위·담당은 공시 표 그대로. 생년월은 식별에만 쓰고 내보내지 않습니다.">
        <table className={s.table}>
          <thead><tr><th>이름</th><th>등기구분</th><th>직위</th><th>상근</th><th>담당</th><th>재임 시작</th><th>근거</th></tr></thead>
          <tbody>
            {data.executives.map((e, i) => (
              <tr key={i} className={e.rgist_exctv_at === "사외이사" ? s.outside : undefined}>
                <td>{e.nm}</td>
                <td className={s.dim}>{e.rgist_exctv_at ?? "—"}</td>
                <td>{e.ofcps ?? "—"}</td>
                <td className={s.dim}>{e.fte_at ?? "—"}</td>
                <td className={s.dim}>{e.chrg_job ?? "—"}</td>
                <td className={s.dim}>{e.tenure_start_on ?? "—"}</td>
                <td><Src r={e} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="지주 겸직 표의 자리" count={data.concurrent_positions.length} empty="지주 사업보고서의 계열회사 임원 겸직 현황 표에 이 회사 자리가 없습니다."
               note="지주 사업보고서 '회사와 계열회사간 임원 겸직 현황' 표에서 이 회사 자리로 적힌 지주 임원(0014). 임원 아닌 직책(본부장·부장 등)은 뺐습니다.">
        <table className={s.table}>
          <thead><tr><th>이름</th><th>직책</th><th>상근</th><th>선임</th><th>지주</th><th>사람 연결</th><th>근거</th></tr></thead>
          <tbody>
            {data.concurrent_positions.map((c, i) => (
              <tr key={i}>
                <td>{c.nm}</td>
                <td>{c.ofcps ?? "—"}</td>
                <td className={s.dim}>{c.fte_at ?? "—"}</td>
                <td className={s.dim}>{c.appointed_on ?? c.appointed_raw ?? "—"}</td>
                <td className={s.dim}>{c.holding_corp_name}</td>
                <td className={s.dim}>{c.person_method === "holding_executive" ? "지주 임원 표와 같은 사람" : c.person_method === "ambiguous" ? "같은 이름 여럿(잇지 않음)" : "지주 임원 표에 없음"}</td>
                <td><Src r={c} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="타법인 출자" count={data.investments.length} empty="이 날짜에 유효한 출자 표가 없습니다(자회사는 대부분 출자 표가 없습니다)." note="기말 장부가액 큰 순. 수집 대상 회사는 이름을 누르면 그 회사로.">
        <table className={s.table}>
          <thead><tr><th>출자 대상</th><th>목적</th><th>최초 취득</th><th className={s.num}>주식 수</th><th className={s.num}>지분율</th><th className={s.num}>장부가액</th><th>근거</th></tr></thead>
          <tbody>
            {data.investments.map((v, i) => (
              <tr key={i}>
                <td>{v.investee_corp_code ? <Link to={`/corps/${v.investee_corp_code}`}>{v.investee_name}</Link> : v.investee_name}</td>
                <td className={s.dim}>{v.invstmnt_purps ?? "—"}</td>
                <td className={s.dim}>{v.frst_acqs_de ?? "—"}</td>
                <td className={s.num}>{num(v.shares)}</td>
                <td className={s.num}>{pct(v.pct)}</td>
                <td className={s.num}>{won(v.book_value)}</td>
                <td><Src r={v} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <p className={s.foot}>수치는 공시 그대로이며 투자 판단용이 아닙니다. 출자 대상 중 수집 대상이 아닌 회사(해외 법인·펀드 등)는 이름만 있습니다.</p>
    </div>
  );
}

function Section({ title, count, empty, note, children }: { title: string; count: number; empty: string; note?: string; children: React.ReactNode }) {
  return (
    <section className={s.section}>
      <h4 className={s.h4}>{title} <span className={s.count}>{count}</span></h4>
      {note && <p className={s.note}>{note}</p>}
      {count === 0 ? <p className={s.empty}>{empty}</p> : <div className={s.tableWrap}>{children}</div>}
    </section>
  );
}

// 근거 공시 링크. 접수번호 앞 8자리가 접수일이라 그것만 보여 준다
function Src({ r }: { r: Pick<Ref, "source_rcept_no" | "url"> }) {
  const d = r.source_rcept_no.slice(0, 8);
  return <a className={s.src} href={r.url} target="_blank" rel="noreferrer" title={`접수번호 ${r.source_rcept_no}`}>{`${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`}</a>;
}
