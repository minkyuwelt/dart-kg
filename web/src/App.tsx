// 공통 틀: 흰 머리띠(이름·페이지 탭·서버 상태) 아래에 페이지가 놓인다. 페이지는 pages/ 아래.
import { useEffect, useState } from "react";
import { BrowserRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import { API_URL, health, type Health } from "./api";
import Ask from "./pages/Ask";
import Changes from "./pages/Changes";
import CorpPageView from "./pages/Corp";
import s from "./shell.module.css";

export default function App() {
  const tab = ({ isActive }: { isActive: boolean }) => (isActive ? s.navOn : s.navLink);
  return (
    <BrowserRouter>
      <div className={s.page}>
        <header className={s.top}>
          <div className={s.topInner}>
            <Link to="/" className={s.brand}>
              <span className={s.brandName}>dart-kg</span>
              <span className={s.brandTag}>4대 금융지주 공시를 시간축 위에</span>
            </Link>
            <nav className={s.nav} aria-label="페이지">
              <NavLink to="/" end className={tab}>질문</NavLink>
              <NavLink to="/changes" className={tab}>변화 리포트</NavLink>
              <NavLink to="/corps" className={tab}>회사</NavLink>
            </nav>
            <ServerStatus />
          </div>
        </header>
        <div className={s.main}>
          <Routes>
            <Route path="/" element={<Ask />} />
            <Route path="/changes" element={<Changes />} />
            <Route path="/corps" element={<CorpPageView />} />
            <Route path="/corps/:corpCode" element={<CorpPageView />} />
          </Routes>
        </div>
        <p className={s.foot}>답과 표는 공시 원문에서만 만들고 수치는 공시 그대로입니다. 투자 판단용이 아닙니다. 자료: 금융감독원 전자공시(OpenDART).</p>
      </div>
    </BrowserRouter>
  );
}

function ServerStatus() {
  const [srv, setSrv] = useState<Health | null | "down">(null);
  useEffect(() => {
    const c = new AbortController();
    health(c.signal).then(setSrv).catch(() => setSrv("down"));
    return () => c.abort();
  }, []);
  if (srv === null) return <p className={s.status}>서버 확인 중</p>;
  if (srv === "down") return <p className={`${s.status} ${s.statusBad}`}>서버에 닿지 못함 · {API_URL}</p>;
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
