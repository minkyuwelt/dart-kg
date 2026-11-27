import type { Metadata } from "next";
import "./globals.css";

// Pretendard: 한국어 본문용 가변 글꼴 (SIL OFL). 로마자 글꼴을 쓰면 한글은 시스템 글꼴로 떨어져 두 글꼴이 섞인다. 한 가족으로 끝낸다 (0013)
const PRETENDARD = "https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css";

export const metadata: Metadata = {
  title: "dart-kg",
  description: "4대 금융지주와 자회사의 공시를 꿰어, 질문에 근거 공시와 함께 답합니다.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <head>
        <link rel="stylesheet" href={PRETENDARD} />
      </head>
      <body>{children}</body>
    </html>
  );
}
