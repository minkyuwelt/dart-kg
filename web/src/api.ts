// FastAPI(src/api/main.py) 응답 꼴. 서버의 pydantic 모델과 같은 이름·같은 칸. 화면은 새 로직을 두지 않고 이것을 그대로 보여 준다 (0013).

export type Hit = {
  n: number;
  rcept_no: string;
  path: string;
  label: string;
  score: number;
  snippet: string;
  url: string;
  cited: boolean;
};

export type Answer = {
  text: string;
  citations: number[];
  model: string;
  not_found: boolean;
  input_tokens: number;
  output_tokens: number;
};

export type GraphRelation = {
  src: string;
  rel: string;
  dst: string;
  value: string;
  valid_from: string | null;
  valid_to: string | null;
  source_rcept_no: string | null;
  url: string | null;
};

export type GraphDerived = { rule: string; subject: string | null; value: string };

export type Graph = {
  n: number;
  cited: boolean;
  trace: Record<string, unknown>;
  relations: GraphRelation[];
  derived: GraphDerived[];
};

export type AskResponse = {
  question: string;
  retriever: string;
  answer: Answer | null;
  hits: Hit[];
  graph: Graph | null;
  graph_error: string | null;
};

export type Health = {
  retriever: string;
  embedder: string | null;
  duckdb: string;
  neo4j: string;
  llm_key: boolean;
  documents_with_chunks?: number;
};

// API 주소. 노트북에서는 uvicorn 기본 포트. Vercel에서는 환경변수 VITE_API_URL로 준다 (web/.env.example)
export const API_URL = (import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function parse<T>(res: Response): Promise<T> {
  if (res.ok) return (await res.json()) as T;
  // FastAPI는 오류를 {detail: "..."}로 준다 (503: DuckDB 잠금·LLM 키 없음, 422: 입력 오류)
  let detail = res.statusText;
  try {
    const body = await res.json();
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    /* 본문이 JSON이 아니면 상태 문구 그대로 */
  }
  throw new ApiError(res.status, detail);
}

export async function ask(question: string, useLlm: boolean, signal?: AbortSignal): Promise<AskResponse> {
  const res = await fetch(`${API_URL}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, use_llm: useLlm }),
    signal,
  });
  return parse<AskResponse>(res);
}

export async function health(signal?: AbortSignal): Promise<Health> {
  return parse<Health>(await fetch(`${API_URL}/health`, { signal }));
}

/** 답 글을 인용 번호 [n] 기준으로 자른다. "본문 [1] 본문 [2][3]" → [{text}, {cite:1}, {text}, {cite:2}, {cite:3}] */
export function splitCitations(text: string): Array<{ text: string } | { cite: number }> {
  const out: Array<{ text: string } | { cite: number }> = [];
  const re = /\[(\d{1,2})\]/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    out.push({ cite: Number(m[1]) });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
}
