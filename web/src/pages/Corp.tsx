// 회사 페이지 (0013 세 번째 페이지). 회사 하나를 고르면 기준일의 임원·최대주주·출자·겸직 표 자리와 지분 추이를 한 장으로.
// API(/corps/{corp_code})가 한 번에 주는 것을 그대로 표로 보여 준다. 화면이 하는 것은 숫자 표기(원 → 억 원)와 "분기 행 접기"뿐.
// 돕는 질문(usecases 2절): 1 그 시점 대표·최대주주, 6 이 자회사의 지분율·장부가액이 해마다 어떻게 변했나, 3 겸직.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError, API_URL, corpPage, corps, type Corp, type CorpPage, type Ref } from "../api";
import s from "./corp.module.css";

const today = () => new Date().toISOString().slice(0, 10);

// 머리 판의 바로가기 순서 = 아래 표 순서. 표가 길어도 어디에 무엇이 몇 건인지 머리에서 먼저 보인다
const SECTIONS = [["stakes", "지분 추이"], ["shareholders", "최대주주"], ["executives", "임원"], ["concurrent_positions", "겸직 표 자리"], ["investments", "출자"]] as const;
// 표 한 판에 처음 보이는 줄 수. 넘치면 "모두 보기"
const ROWS = 8;

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
    <main>
      <div className={s.toolbar}>
        <div className="field">
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
        <div className="field">
          <label htmlFor="as_of">기준일</label>
          <input id="as_of" type="date" value={asOf} max={today()} onChange={(e) => setAsOf(e.target.value)} />
        </div>
        <p className={s.toolbarHint}>기준일에 참이었던 임원·최대주주·출자를 보여 줍니다. 날짜를 바꾸면 그때의 표로 바뀝니다.</p>
      </div>

      {!corpCode && corpList.length > 0 && (
        <section className="panel" aria-label="회사 고르기">
          <div className="panelHead"><h3>회사 {corpList.length}곳</h3><p className="panelNote">지주 4곳과 직속 자회사. 지주의 최신 사업보고서 타법인 출자현황에서 뽑았습니다.</p></div>
          <div className={s.pick}>
            {grouped.map(({ holding, subs }) => (
              <div key={holding.corp_code} className={s.pickGroup}>
                <Link to={`/corps/${holding.corp_code}`} className={s.pickHolding}>{holding.corp_name}</Link>
                <div className={s.pickSubs}>{subs.map((c) => <Link key={c.corp_code} to={`/corps/${c.corp_code}`}>{c.corp_name}</Link>)}</div>
              </div>
            ))}
          </div>
        </section>
      )}
      {!corpCode && corpList.length === 0 && <p className="muted">회사 목록을 불러오지 못했습니다. API 서버가 떠 있는지 확인하세요.</p>}

      {error && <p role="alert" className="alert">{error}</p>}
      {loading && !data && <p className="muted" aria-live="polite">불러오고 있습니다.</p>}

      {data && <Body data={data} />}
    </main>
  );
}

function Body({ data }: { data: CorpPage }) {
  const { corp } = data;
  const noOwnTable = data.executives.length === 0 && data.concurrent_positions.length > 0;
  // 지분 추이: 분기마다 한 줄이면 5년에 20줄. 기본은 12월 말(사업보고서) 행만, 단추로 분기까지
  const [quarters, setQuarters] = useState(false);
  const yearEnd = data.stake_history.filter((h) => h.stlm_dt.endsWith("-12-31"));
  const stakes = quarters || yearEnd.length === 0 ? data.stake_history : yearEnd;

  return (
    <div>
      <header className={`panel ${s.head}`}>
        <div className={s.headTitle}>
          <h2 className={s.corpName}>{corp.corp_name}</h2>
          <span className={corp.role === "holding" ? "chip" : "chip chipQuiet"}>{corp.role === "holding" ? "지주회사" : "직속 자회사"}</span>
          {corp.stock_code ? <span className="chip chipQuiet">상장 {corp.stock_code}</span> : <span className="chip chipQuiet">비상장</span>}
        </div>
        <dl className={s.facts}>
          {corp.role !== "holding" && (
            <div><dt>지주</dt><dd><Link to={`/corps/${corp.parent_corp_code}`}>{corp.parent_corp_name}</Link>{corp.stake_pct !== null ? <span className={s.factSub}> 지분 {corp.stake_pct}%</span> : null}</dd></div>
          )}
          <div>
            <dt>최신 정기보고서</dt>
            <dd>{corp.latest_report ? <><a href={corp.latest_report.url} target="_blank" rel="noreferrer">{corp.latest_report.report_nm}</a><span className={s.factSub}> 접수 {corp.latest_report.rcept_dt}</span></> : <span className="muted">없음 — 감사보고서만 내는 회사</span>}</dd>
          </div>
          <div><dt>기준일</dt><dd>{data.as_of}</dd></div>
          <div><dt>고유번호</dt><dd className={s.factMono}>{corp.corp_code}</dd></div>
        </dl>
        <nav className={s.jump} aria-label="이 페이지의 표">
          {SECTIONS.map(([id, label]) => {
            const n = id === "stakes" ? stakes.length : data[id].length;
            return <a key={id} href={`#${id}`} className={n === 0 ? s.jumpOff : s.jumpOn}>{label} <b>{n}</b></a>;
          })}
        </nav>
        {noOwnTable && <p className={s.headNote}>이 회사는 임원·최대주주 표를 내지 않습니다. 지주 사업보고서의 겸직 현황 표에 적힌 자리가 아는 전부입니다.</p>}
      </header>

      <Section id="stakes" title="지분 추이" rows={stakes} empty="수집 대상 회사의 출자 표에 이 회사가 없습니다. 지주회사이거나, 출자 표의 이름이 이 회사로 이어지지 않은 경우입니다."
               note="지주의 사업보고서 타법인 출자현황이 적은 이 회사의 기말 지분율·장부가액입니다."
               tools={yearEnd.length > 0 && yearEnd.length < data.stake_history.length && (
                 <button type="button" className={`btnQuiet ${quarters ? "btnQuietOn" : ""}`} onClick={() => setQuarters((q) => !q)} aria-pressed={quarters}>
                   분기 포함 {data.stake_history.length}건
                 </button>
               )}
               head={<tr><th>기준일</th><th>출자한 회사</th><th>주식 종류</th><th className="num">주식 수</th><th className="num">지분율</th><th className="num">장부가액</th><th>근거</th></tr>}
               row={(h, i) => (
            <tr key={i}>
                <td className="strong nowrap">{h.stlm_dt}</td>
                <td><Link to={`/corps/${h.investor_corp_code}`}>{h.investor_corp_name}</Link></td>
                <td className="dim">{h.share_class ?? "—"}</td>
                <td className="num">{num(h.shares)}</td>
                <td className="num">{pct(h.pct)}</td>
                <td className="num">{won(h.book_value)}</td>
                <td><Src r={h} /></td>
              </tr>
          )} />

      <Section id="shareholders" title="최대주주와 특수관계인" rows={data.shareholders} empty="이 날짜에 유효한 최대주주 표가 없습니다."
               head={<tr><th>이름</th><th>관계</th><th>주식 종류</th><th className="num">주식 수</th><th className="num">지분율</th><th>근거</th></tr>}
               row={(h, i) => (
            <tr key={i}>
                <td className="strong">{h.holder_corp_code ? <Link to={`/corps/${h.holder_corp_code}`}>{h.nm}</Link> : h.nm}</td>
                <td className="dim">{h.relate ?? "—"}</td>
                <td className="dim">{h.stock_knd ?? "—"}</td>
                <td className="num">{num(h.shares)}</td>
                <td className="num">{pct(h.pct)}</td>
                <td><Src r={h} /></td>
              </tr>
          )} />

      <Section id="executives" title="임원" rows={data.executives}
               empty={noOwnTable ? "임원 표를 내지 않는 회사입니다. 아래 '지주 겸직 표의 자리'를 보세요." : "이 날짜에 유효한 임원 표가 없습니다."}
               note="등기구분·직위·담당은 공시 표 그대로입니다. 생년월은 사람을 구별하는 데만 쓰고 보여 주지 않습니다."
               head={<tr><th>이름</th><th>등기구분</th><th>직위</th><th>상근</th><th>담당</th><th>재임 시작</th><th>근거</th></tr>}
               row={(e, i) => (
            <tr key={i}>
                <td className="strong nowrap">{e.nm}</td>
                <td>{e.rgist_exctv_at ? <span className={e.rgist_exctv_at === "사외이사" ? "chip" : "chip chipQuiet"}>{e.rgist_exctv_at}</span> : "—"}</td>
                <td>{e.ofcps ?? "—"}</td>
                <td className="dim">{e.fte_at ?? "—"}</td>
                <td className="dim">{e.chrg_job ?? "—"}</td>
                <td className="dim nowrap">{e.tenure_start_on ?? "—"}</td>
                <td><Src r={e} /></td>
              </tr>
          )} />

      <Section id="concurrent_positions" title="지주 겸직 표의 자리" rows={data.concurrent_positions} empty="지주 사업보고서의 계열회사 임원 겸직 현황 표에 이 회사 자리가 없습니다."
               note="지주 사업보고서 '회사와 계열회사간 임원 겸직 현황' 표에서 이 회사 자리로 적힌 지주 임원입니다. 본부장·부장처럼 임원이 아닌 직책은 뺐습니다."
               head={<tr><th>이름</th><th>직책</th><th>상근</th><th>선임</th><th>지주</th><th>사람 연결</th><th>근거</th></tr>}
               row={(c, i) => (
            <tr key={i}>
                <td className="strong nowrap">{c.nm}</td>
                <td>{c.ofcps ?? "—"}</td>
                <td className="dim">{c.fte_at ?? "—"}</td>
                <td className="dim nowrap">{c.appointed_on ?? c.appointed_raw ?? "—"}</td>
                <td className="dim">{c.holding_corp_name}</td>
                <td className="dim">{c.person_method === "holding_executive" ? "지주 임원 표와 같은 사람" : c.person_method === "ambiguous" ? "같은 이름이 여럿이라 잇지 않음" : "지주 임원 표에 없음"}</td>
                <td><Src r={c} /></td>
              </tr>
          )} />

      <Section id="investments" title="타법인 출자" rows={data.investments} empty="이 날짜에 유효한 출자 표가 없습니다. 자회사는 대부분 출자 표를 내지 않습니다."
               note="기말 장부가액이 큰 순입니다. 수집 대상인 회사는 이름을 누르면 그 회사로 갑니다. 해외 법인·펀드는 이름만 있습니다."
               head={<tr><th>출자 대상</th><th>목적</th><th>최초 취득</th><th className="num">주식 수</th><th className="num">지분율</th><th className="num">장부가액</th><th>근거</th></tr>}
               row={(v, i) => (
            <tr key={i}>
                <td className="strong">{v.investee_corp_code ? <Link to={`/corps/${v.investee_corp_code}`}>{v.investee_name}</Link> : v.investee_name}</td>
                <td className="dim">{v.invstmnt_purps ?? "—"}</td>
                <td className="dim nowrap">{v.frst_acqs_de ?? "—"}</td>
                <td className="num">{num(v.shares)}</td>
                <td className="num">{pct(v.pct)}</td>
                <td className="num">{won(v.book_value)}</td>
                <td><Src r={v} /></td>
              </tr>
          )} />
    </div>
  );
}

function Section<T>({ id, title, rows, empty, note, tools, head, row }: {
  id: string; title: string; rows: T[]; empty: string; note?: string; tools?: React.ReactNode; head: React.ReactNode; row: (r: T, i: number) => React.ReactNode;
}) {
  // 줄이 많으면(임원 30명, 출자 60곳) 처음 ROWS줄만 보이고 나머지는 단추로 편다 — 페이지가 표 다섯 개라 하나가 길면 나머지가 안 보인다
  const [all, setAll] = useState(false);
  const shown = all || rows.length <= ROWS ? rows : rows.slice(0, ROWS);
  return (
    <section id={id} className="panel">
      <div className="panelHead">
        <h3>{title} <span className="panelCount">{rows.length}</span></h3>
        {note && <p className="panelNote">{note}</p>}
        {tools && <div className={s.tools}>{tools}</div>}
      </div>
      {rows.length === 0 ? (
        <p className="panelEmpty">{empty}</p>
      ) : (
        <div className="tblWrap">
          <table className="tbl">
            <thead>{head}</thead>
            <tbody>{shown.map(row)}</tbody>
          </table>
          {rows.length > ROWS && (
            <button type="button" className={s.more} onClick={() => setAll((a) => !a)} aria-expanded={all}>
              {all ? "처음 8줄만 보기" : `나머지 ${rows.length - ROWS}줄 더 보기`}
            </button>
          )}
        </div>
      )}
    </section>
  );
}

// 근거 공시 링크. 접수번호 앞 8자리가 접수일이라 날짜 칩으로 보여 주고, 누르면 DART 뷰어
function Src({ r }: { r: Pick<Ref, "source_rcept_no" | "url"> }) {
  const d = r.source_rcept_no.slice(0, 8);
  return <a className="chip chipQuiet" href={r.url} target="_blank" rel="noreferrer" title={`접수번호 ${r.source_rcept_no} — DART에서 보기`}>{`${d.slice(0, 4)}.${d.slice(4, 6)}.${d.slice(6, 8)}`}</a>;
}
