# 평가 세트 만들기

임베딩 모델·청킹·프롬프트는 모두 이 세트의 점수로 비교한다 (`docs/plans/phase3.md` 4절). 세트가 없으면 "느낌상 좋아졌다"밖에 말할 수 없다.

## 파일

| 파일 | 역할 | 커밋 |
|---|---|---|
| `eval/questions.jsonl` | 질문. 한 줄에 하나 (형식: `eval/questions.py` 설명) | O |
| `eval/questions.draft.jsonl` | `python -m eval.draft`가 만든 후보. 정답을 채워 `questions.jsonl`로 옮긴다 | X (데이터에서 생성) |
| `eval/results/` | `python -m eval.run` 결과 | X |

## 빠른 길: `python -m eval.review`

JSON을 손으로 고치지 않아도 된다. 후보를 한 개씩 보여 주면서 **그 섹션의 원문(DuckDB에 파싱된 글과 표)**을 함께 띄우니, 읽고 정답을 치면 끝이다.

```bash
python -m eval.draft --seed      # 후보 만들기 (한 번)
python -m eval.review            # 한 개씩 보며 정답 입력 → questions.jsonl에 ready로 저장
python -m eval.review --type 정형표   # 유형 하나만
```

입력: 정답 글자(저장) / Enter(건너뛰기) / m(원문 더 보기) / d(후보 삭제) / q(종료). "없음" 유형은 뷰어 검색으로 확인한 뒤 `y`.
숫자(지분율·배당·직원 수)는 화면의 뷰어 링크로 한 번 대조하는 것을 권한다. 정답은 사람이 읽고 치는 것이고 코드는 추측하지 않는다.

## 손으로 하는 순서 (노트북, 2~3시간)

1. **후보 만들기**
   ```bash
   python -m eval.draft --seed
   ```
   대상 회사의 최신 사업보고서(지주는 틀 11개, 자회사는 회사마다 2개)와 전 연도 보고서(시점 유형)로 후보를 만든다.
   접수번호와 근거 섹션 경로는 데이터에서 채워지고 **정답(`answer`)은 비어 있다**. `--seed`는 Phase 2 질문 4개의 접수번호도 채운다.

2. **30개 고르기.** 분포 목표: 정형표 10 · 서술 10 · 시점 5 · 없음 5. 지주 4곳만 아니라 자회사(은행·카드·증권·보험)와 2022~2025년이 섞이게.
   회사 이름이 어색한 질문, 섹션이 비어 보이는 후보는 지운다.

3. **정답 확인.** 후보의 `note`에 있는 뷰어 링크(`https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호`)를 열어 근거 섹션을 직접 보고 `answer`를 적는다.
   - 표에 있는 값은 표에 적힌 그대로 (단위 포함: "1,234백만원", "70.0%")
   - 서술은 원문 요지 2~3문장. 원문에 없는 말을 보태지 않는다
   - 섹션 경로가 맞는지 모르겠으면 정답에 나오는 글자로 찾는다:
     ```bash
     python -m eval.locate 20260313001191 "신한금융지주회사"
     ```
     출력된 경로를 `evidence_section`에 붙인다. 경로 전체가 아니라 뒷부분 일부만 적어도 된다 (부분 일치)
   - **확인하지 못한 질문은 ready로 바꾸지 않는다.** 추측으로 적은 정답은 평가 전체를 망친다

4. **없음 유형.** `questions.jsonl`에 미리 적힌 5개는 뷰어의 본문 검색으로 정말 없는지 확인하고 ready로 바꾼다. 있으면 질문을 바꾼다.

5. `status`를 `ready`로 바꾸고 `questions.jsonl`에 붙인다. `id`는 겹치지 않게 (`q001`부터 다시 매겨도 된다).

6. **형식 검사와 기본선 실행**
   ```bash
   python -c "from eval.questions import load_questions; qs = load_questions(); print(len(qs), sum(q.is_ready for q in qs))"
   ```
   ```bash
   python -m eval.run --retrieval-only --label baseline
   ```
   키워드 기본선의 hit@5·MRR이 나온다. 이 숫자가 임베딩(6번 PR)·하이브리드(8번 PR)와 비교하는 바닥이다.

7. `questions.jsonl`을 커밋해 push한다. 결과 파일은 커밋하지 않는다.

## 규칙

- 정답과 접수번호는 사람이 뷰어에서 확인한 것만 적는다 (계획 4절). 코드가 정답을 추측해 채우지 않는다
- 임원 생년월 같은 개인정보는 질문·정답에 넣지 않는다. 공개 저장소다
- `phase: 4` 질문(다중경로)은 RAG만으로는 못 답한다. 검색이 근거 공시를 찾는지만 잰다
- "없음" 유형은 검색 지표에서 빠지고, LLM 단계(9번 PR)에서 "모른다"고 답하는지로 채점한다

## 지표

- `hit@5 공시`: 상위 5개 조각 중 근거 접수번호의 조각이 있는 비율
- `hit@5 섹션`: 그중 경로에 `evidence_section`이 들어 있는 조각이 있는 비율 ("맞는 보고서, 엉뚱한 섹션"을 구분)
- `MRR`: 첫 정답 조각 순위의 역수 평균
- 목표(계획 2절): hit@5 공시 80% 이상
