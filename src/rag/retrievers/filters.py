"""검색 회사 필터: 질문에서 회사를 알아내 후보를 그 회사 보고서로 좁힌다 (0009 실험의 "회사 필터" 변수).

찾기는 src/graph/names.find_corps 한 벌을 쓴다 (2026-11-27, 코드 리뷰 A2). 전에는 여기 따로 있던 guess_corp_code가
target_corp·disclosure 이름만 보고 "가장 긴 일치"를 골라, 같은 이름이 두 회사를 가리켜도 거르지 않았고 라우터와 결과가 다를 수 있었다.
질문에 회사명이 없으면 None → 필터 없이 검색.
"""

from src.graph.names import guess_corp_code  # noqa: F401
