"""OpenDART 정기보고서 주요정보 - 타법인 출자현황(otrCprInvstmntSttus.json).

한 회사가 정기보고서(사업·반기·분기)에 적은 "다른 법인에 출자한 내역"을 가져온다.
금융지주의 경우 이 목록에 자회사가 들어 있으므로, 대상 범위(어느 자회사까지 수집할지)를
정하는 데 쓰고, Phase 2에서는 지분 관계 그래프의 원천 데이터가 된다.

요청 파라미터
- corp_code : 출자한 회사 (예: 지주사)
- bsns_year : 사업연도 (2015년 이후)
- reprt_code: 11011 사업보고서, 11012 반기보고서, 11013 1분기보고서, 11014 3분기보고서

이 API는 페이지 넘김이 없다. 호출 1건에 해당 보고서의 출자 내역 전체가 온다.

주의: 응답의 출자 대상(inv_prm)은 corp_code가 아니라 회사 "이름"이다.
그래서 corp 테이블과 이름으로 연결해야 하고, 이것이 엔티티 연결 문제의 첫 사례다
(src/loaders/investments.py 참고).
"""

from src.collectors.dart_client import get_json
from src.collectors.disclosure_list import ApiPage, make_request_key

ENDPOINT = "otrCprInvstmntSttus.json"

REPORT_CODES = {
    "11011": "사업보고서",
    "11012": "반기보고서",
    "11013": "1분기보고서",
    "11014": "3분기보고서",
}


def fetch_investments(api_key: str, corp_code: str, bsns_year: str, reprt_code: str = "11011") -> ApiPage:
    """한 회사의 한 보고서에 실린 타법인 출자 내역을 가져온다. API 1건."""
    if reprt_code not in REPORT_CODES:
        raise ValueError(f"reprt_code는 {list(REPORT_CODES)} 중 하나여야 합니다: {reprt_code}")
    params = {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code}
    body = get_json(ENDPOINT, api_key, params)
    return ApiPage(ENDPOINT, make_request_key(params), body)
