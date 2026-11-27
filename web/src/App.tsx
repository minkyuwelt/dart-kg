// 공통 틀: 머리(이름·한 줄 설명·서버 상태)와 두 페이지 사이의 이동. 페이지는 pages/ 아래.
import { useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { API_URL, health, type Health } from "./api";
import Ask from "./pages/Ask";
import Changes from "./pages/Changes";
import s from "./shell.module.css";

export default function App() {
  return (
    <BrowserRouter>
      <div className={s.page}>
        <header className={s.top}>
          <div className={s.titles}>
            <h1 className={s.brand}>dart-kg</h1>
            <p className={s.tagline}>4대 금융지주와 자회사의 공시를 꿰어, 질문에 근거 공시와 함께 답합니다.</p>
          </div>
          <nav className={s.nav} aria-label="페이지">
            <NavLink to="/" end className={({ isActive }) => (isActive ? s.navOn : s.navLink)}>질문</NavLink>
            <NavLink to="/changes" className={({ isActive }) => (isActive ? s.navOn : s.navLink)}>변화 리포트</NavLink>
          </nav>
          <ServerStatus />
        </header>
        <Routes>
          <Route path="/" element={<Ask />} />
          <Route path="/changes" element={<Changes />} />
        </Routes>
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
