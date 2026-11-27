// FastAPI(src/api/main.py) 응답 꼴. 서버의 pydantic 모델과 같은 이름·같은 칸. 화면은 새 로직을 두지 않고 이것을 그대로 보여 준다 (0013).

export type Hit = {
  n: number;
  rcept_no: string;
  corp_code: string | null;   // 공시를 낸 회사 → 회사 페이지
  corp_name: string | null;
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
  src_corp_code: string | null;   // 회사 페이지가 있는 회사일 때만
  rel: string;
  dst: string;
  dst_corp_code: string | null;
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
  llm_key: boolean;   // 서버 .env에 키가 있는가. 없으면 보는 사람이 자기 키를 넣어야 답 만들기가 된다
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

/** apiKey가 있으면 X-OpenAI-Key 헤더로 보낸다 — 보는 사람의 키, 서버는 이 요청에만 쓴다 (0013) */
export async function ask(question: string, useLlm: boolean, apiKey?: string, signal?: AbortSignal): Promise<AskResponse> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (useLlm && apiKey) headers["X-OpenAI-Key"] = apiKey;
  const res = await fetch(`${API_URL}/ask`, {
    method: "POST",
    headers,
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

// ----- 변화 리포트 (/changes, src/reports/changes.py · 0012) -----

export type Change = { kind: string; kind_label: string; subject: string; before: string; after: string; note: string };

export type ReportChanges = {
  rcept_no: string;
  corp_code: string;
  corp_name: string;
  report_nm: string;
  rcept_dt: string;
  stlm_dt: string | null;
  prev_stlm_dt: string | null;   // 비교한 직전 스냅샷 기준일. null이면 첫 보고서(비교 대상 없음)
  url: string;
  changes: Change[];
};

export type ChangesResponse = {
  since: string;
  until: string;
  pct_threshold: number;
  summary: { reports: number; reports_with_changes: number; changes: number; by_kind: Record<string, number> };
  reports: ReportChanges[];
};

// ----- 문장 변화 (/changes/text, src/reports/text_changes.py · 0012 결정 4) -----

export type SectionDiff = {
  path: string;
  status: "changed" | "numbers_only" | "same" | "added" | "removed" | string;
  replaced: [string, string][];   // [전년 문장, 이번 문장] 자리가 같은 짝
  removed: string[];
  added: string[];
  numbers_only: number;           // 숫자·날짜만 바뀐 문장 수 (글 변화로 치지 않음)
  sentences_before: number;
  sentences_after: number;
  parts_before: number;           // 묶음에 든 전년 절 수. 하나라도 1보다 크면 상위 절로 묶어 견준 것 (해마다 절 굵기가 다름)
  parts_after: number;
  text_changes: number;
};

export type TextChanges = {
  rcept_no: string;
  corp_code: string;
  corp_name: string;
  report_nm: string;
  rcept_dt: string;
  url: string;
  prev_rcept_no: string | null;   // 전년 같은 종류 보고서. null이면 비교 대상 없음
  prev_report_nm: string | null;
  prev_url: string | null;
  sections: SectionDiff[];
};

export type TextChangesResponse = {
  since: string;
  until: string;
  section_words: string[];
  summary: { reports: number; compared: number; sections_changed: number; text_changes: number; numbers_only: number };
  reports: TextChanges[];
};

export type Corp = { corp_code: string; corp_name: string; role: "holding" | "subsidiary" | string; parent_corp_code: string | null; stake_pct: number | null };

export async function changes(since: string, until: string, corps: string[], signal?: AbortSignal): Promise<ChangesResponse> {
  const q = new URLSearchParams({ since, until });
  for (const c of corps) q.append("corp", c);
  return parse<ChangesResponse>(await fetch(`${API_URL}/changes?${q}`, { signal }));
}

export async function textChanges(since: string, until: string, corps: string[], signal?: AbortSignal): Promise<TextChangesResponse> {
  const q = new URLSearchParams({ since, until });
  for (const c of corps) q.append("corp", c);
  return parse<TextChangesResponse>(await fetch(`${API_URL}/changes/text?${q}`, { signal }));
}

export async function corps(signal?: AbortSignal): Promise<Corp[]> {
  return parse<Corp[]>(await fetch(`${API_URL}/corps`, { signal }));
}

// ----- 회사 페이지 (/corps/{corp_code}, src/loaders/as_of.py corp_page · 0013) -----
// 모든 표 행에 근거 공시 링크(url)가 있고, 생년월·person_id는 서버가 내보내지 않는다 (0006)

export type Ref = { source_rcept_no: string; url: string; valid_from: string | null; valid_to: string | null };

export type CorpOverview = {
  corp_code: string;
  corp_name: string;
  role: "holding" | "subsidiary" | string;
  parent_corp_code: string | null;
  parent_corp_name: string | null;
  stake_pct: number | null;
  stock_code: string | null;
  latest_report: { source_rcept_no: string; report_nm: string; rcept_dt: string; url: string } | null;   // 감사보고서만 내는 회사는 null
};

export type Executive = Ref & {
  nm: string;
  ofcps: string | null;
  rgist_exctv_at: string | null;   // 등기구분: 사내이사·사외이사·기타비상무이사·미등기
  fte_at: string | null;           // 상근·비상근
  chrg_job: string | null;
  tenure_start_on: string | null;
  tenure_start_method: string | null;
  tenure_end_on: string | null;
};

export type Shareholder = Ref & {
  nm: string;
  relate: string | null;
  stock_knd: string | null;
  holder_corp_code: string | null;
  shares: number | null;
  pct: number | null;
};

export type Investment = Ref & {
  investee_name: string;
  investee_corp_code: string | null;   // 있으면 그 회사 페이지로 이어 준다
  invstmnt_purps: string | null;
  frst_acqs_de: string | null;
  shares: number | null;
  pct: number | null;
  book_value: number | null;           // 기말 장부가액(원)
};

export type ConcurrentPosition = Ref & {
  nm: string;
  ofcps: string | null;
  fte_at: string | null;
  appointed_on: string | null;
  appointed_raw: string | null;
  person_method: string;       // holding_executive(지주 임원 표의 사람과 이음) / not_found / ambiguous
  holding_corp_name: string;
};

export type StakePoint = {
  stlm_dt: string;
  investor_corp_code: string;
  investor_corp_name: string;
  share_class: string | null;
  shares: number | null;
  pct: number | null;
  book_value: number | null;
  source_rcept_no: string;
  url: string;
};

export type CorpPage = {
  corp: CorpOverview;
  as_of: string;
  executives: Executive[];
  shareholders: Shareholder[];
  investments: Investment[];
  concurrent_positions: ConcurrentPosition[];
  stake_history: StakePoint[];
};

export async function corpPage(corpCode: string, asOf: string, signal?: AbortSignal): Promise<CorpPage> {
  const q = new URLSearchParams({ as_of: asOf });
  return parse<CorpPage>(await fetch(`${API_URL}/corps/${encodeURIComponent(corpCode)}?${q}`, { signal }));
}
