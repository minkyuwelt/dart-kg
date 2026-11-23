"""OpenDART 정기보고서 주요정보 API 3종: 최대주주 현황, 최대주주 변동현황, 임원 현황.

Phase 2 관계 데이터의 원천이다. 세 API 모두 타법인 출자현황(src/collectors/investment.py)과
같은 파라미터(corp_code, bsns_year, reprt_code)를 받고, 페이지 넘김 없이 호출 1건에 전체가 온다.

| 이름                      | endpoint            | 내용                                   | 시간 정보 (0005)                 |
|---------------------------|---------------------|----------------------------------------|----------------------------------|
| major_shareholder         | hyslrSttus.json     | 보고서 기준일의 최대주주와 지분율      | stlm_dt (분기 단위)              |
| major_shareholder_change  | hyslrChgSttus.json  | 최대주주 변동 이력 (날짜 있음)         | change_on (일 단위)              |
| executive                 | exctvSttus.json     | 임원 명단, 직위, 등기·상근, 임기만료일 | stlm_dt, hffc_pd 역산, tenure_end_on |

2026-11-23 KB금융 2025 사업보고서 실제 응답(tests/fixtures/)에서 확인한 것:
- 세 응답 모두 행마다 rcept_no가 있고, 값은 그 보고서의 **최신 정정본** 접수번호다. 정정 전 응답은 받을 수 없다
- 날짜 형식이 API마다 다르다: stlm_dt '2025-12-31', change_on·tenure_end_on '2019년 12월 31일', birth_ym '1961년 06월'
- 변동현황은 보고서마다 과거 변동이 누적되어 실린다 (2025 보고서에 2019년 변동까지)
- 최대주주 현황에는 nm이 '계'인 합계 행이 있고, 그 행에는 relate 키가 아예 없다
해석은 src/loaders/relations.py가 맡는다.
"""

from src.collectors.dart_client import get_json
from src.collectors.disclosure_list import ApiPage, make_request_key
from src.collectors.investment import REPORT_CODES

ENDPOINTS = {
    "major_shareholder": "hyslrSttus.json",
    "major_shareholder_change": "hyslrChgSttus.json",
    "executive": "exctvSttus.json",
}


def fetch_report_detail(api_key: str, name: str, corp_code: str, bsns_year: str, reprt_code: str) -> ApiPage:
    """한 회사의 한 보고서에서 주요정보 하나(name은 ENDPOINTS의 키)를 가져온다. API 1건."""
    if name not in ENDPOINTS:
        raise ValueError(f"name은 {list(ENDPOINTS)} 중 하나여야 합니다: {name}")
    if reprt_code not in REPORT_CODES:
        raise ValueError(f"reprt_code는 {list(REPORT_CODES)} 중 하나여야 합니다: {reprt_code}")
    params = {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code}
    endpoint = ENDPOINTS[name]
    body = get_json(endpoint, api_key, params)
    return ApiPage(endpoint, make_request_key(params), body)
