// 물은 질문 이력 페이지. 브라우저에 저장된 것(history.ts)을 한 줄씩: 시각·질문·답 첫 줄·인용 수·관계표 유무. 누르면 질문 페이지가 그 결과를 연다.
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { loadHistory, removeFromHistory, saveHistory, whenLabel, type Asked } from "../history";
import s from "./history.module.css";

export default function History() {
  const [items, setItems] = useState<Asked[]>(() => loadHistory());
  const navigate = useNavigate();

  function update(next: Asked[]) {
    setItems(next);
    saveHistory(next);
  }

  return (
    <main>
      <section className="panel">
        <div className="panelHead">
          <h3>물은 질문 <span className="panelCount">{items.length}</span></h3>
          <p className="panelNote">이 브라우저에만 남습니다</p>
          {items.length > 0 && <button type="button" className={s.clear} onClick={() => update([])}>모두 지우기</button>}
        </div>
        {items.length === 0 ? (
          <p className="panelEmpty">아직 물은 것이 없습니다. <Link to="/">질문하러 가기</Link></p>
        ) : (
          <ul className={s.list}>
            {items.map(({ at, response: r }) => {
              const first = r.answer ? r.answer.text.replace(/\[\d+\]/g, "").split(/(?<=[.다요])\s/)[0] : "";
              return (
                <li key={r.question} className={s.row}>
                  <button type="button" className={s.open} onClick={() => navigate("/", { state: { restore: r.question } })}>
                    <span className={s.when}>{whenLabel(at)}</span>
                    <span className={s.body}>
                      <span className={s.q}>{r.question}</span>
                      {r.answer ? (
                        <span className={r.answer.not_found ? `${s.a} ${s.aNone}` : s.a}>{first}</span>
                      ) : (
                        <span className={`${s.a} ${s.aNone}`}>답 없이 근거만 찾음</span>
                      )}
                    </span>
                    <span className={s.meta}>
                      <span className="chip chipQuiet">근거 {r.hits.length}</span>
                      {r.answer && r.answer.citations.length > 0 && <span className="chip chipMarker">인용 {r.answer.citations.length}</span>}
                      {r.graph && <span className="chip">관계표</span>}
                    </span>
                  </button>
                  <button type="button" className={s.remove} onClick={() => update(removeFromHistory(items, r.question))} aria-label="이 질문 지우기" title="지우기">×</button>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </main>
  );
}
