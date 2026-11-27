// 물은 질문 이력. 브라우저(localStorage)에만 둔다 — 서버에 쓰려면 API가 DuckDB에 써야 하는데 API는 읽기 전용 연결이고(Dagster 잠금),
// 혼자 쓰는 도구라 새 저장소를 둘 이유가 없다 (0013). 결과(응답 그대로)를 함께 두어 다시 열 때 API를 부르지 않는다.
import type { AskResponse } from "./api";

export type Asked = { at: string; response: AskResponse };   // at: ISO 시각

const KEY = "dart-kg.asked.v2";
export const HISTORY_MAX = 50;

export function loadHistory(): Asked[] {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "[]") as Asked[];
    return Array.isArray(raw) ? raw.filter((a) => a && a.response && typeof a.at === "string") : [];
  } catch {
    return [];
  }
}

export function saveHistory(h: Asked[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(h.slice(0, HISTORY_MAX)));
  } catch {
    /* 저장이 막힌 브라우저면 이력 없이 간다 */
  }
}

/** 같은 질문은 최신 것 하나만 남기고 맨 앞에 */
export function addToHistory(h: Asked[], r: AskResponse): Asked[] {
  return [{ at: new Date().toISOString(), response: r }, ...h.filter((a) => a.response.question !== r.question)].slice(0, HISTORY_MAX);
}

export function removeFromHistory(h: Asked[], question: string): Asked[] {
  return h.filter((a) => a.response.question !== question);
}

/** 물은 시각을 짧게: 오늘이면 시:분, 아니면 월.일 */
export function whenLabel(at: string): string {
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const two = (n: number) => String(n).padStart(2, "0");
  return sameDay ? `${two(d.getHours())}:${two(d.getMinutes())}` : `${d.getMonth() + 1}.${d.getDate()}`;
}
