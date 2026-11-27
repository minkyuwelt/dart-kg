// 보는 사람의 OpenAI 키. 이 브라우저(localStorage)에만 두고 /ask 요청 헤더 X-OpenAI-Key로만 보낸다 (0013 "키").
// 공개 서버에는 운영자 키가 없으므로, 답 만들기는 자기 키를 넣은 사람만 자기 비용으로 쓴다. Vercel이나 서버에 저장되지 않는다.
const KEY = "dart-kg.openai_key";

export function loadKey(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveKey(k: string) {
  try {
    if (k.trim()) localStorage.setItem(KEY, k.trim());
    else localStorage.removeItem(KEY);
  } catch {
    /* 저장이 막힌 브라우저면 이번 탭에서만 */
  }
}
