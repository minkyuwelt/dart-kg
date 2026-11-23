"""OpenDART 공시검색(list.json)으로 한 회사의 기간별 공시 목록을 가져온다.

이 모듈은 DART 호출과 응답 해석만 한다. DB 저장은 src/loaders/disclosures.py가 맡고,
Dagster도 모른다. 그래서 테스트할 때는 dart_client만 바꿔 끼우면 된다.

list.json 요청 파라미터 (이 프로젝트에서 쓰는 것만)
- corp_code : 8자리 고유번호. 회사를 지정한다
- bgn_de, end_de : 접수일 기준 검색 기간 (YYYYMMDD)
- page_no, page_count : 페이지 번호, 페이지당 건수(최대 100)

응답은 status, total_count, total_page, list[...]로 구성된다.
list의 각 행에는 rcept_no(접수번호 14자리, 공시 하나를 가리키는 고유 키)가 들어 있다.

API 호출 수: 페이지 수만큼. 월 단위로 한 회사를 조회하면 보통 1건이다
(금융지주의 월 공시 건수는 100건보다 훨씬 적다).
"""

from dataclasses import dataclass
from urllib.parse import urlencode

from src.collectors.dart_client import STATUS_NO_DATA, get_json

ENDPOINT = "list.json"
PAGE_COUNT = 100  # DART가 허용하는 최대값

# disclosure 테이블에 넣을 컬럼. list.json 응답 필드 이름을 그대로 쓴다.
DISCLOSURE_FIELDS = [
    "rcept_no",
    "corp_code",
    "corp_name",
    "stock_code",
    "corp_cls",
    "report_nm",
    "flr_nm",
    "rcept_dt",
    "rm",
]


@dataclass
class ApiPage:
    """API 응답 한 페이지. 원본 보관(raw_api_response)에 그대로 저장한다."""

    endpoint: str
    request_key: str  # API 키를 뺀 요청 파라미터. 같은 요청이면 항상 같은 문자열
    body: dict


def make_request_key(params: dict) -> str:
    """파라미터를 이름순으로 정렬해 쿼리 문자열로 만든다. API 키는 넣지 않는다."""
    return urlencode(sorted(params.items()))


def fetch_disclosures(api_key: str, corp_code: str, bgn_de: str, end_de: str) -> list[ApiPage]:
    """한 회사의 기간 내 공시 목록을 모든 페이지에 걸쳐 가져온다.

    데이터가 없으면(status 013) 그 응답 한 페이지만 담아 돌려준다.
    "조회했는데 없었다"는 사실도 원본으로 남기기 위해서다.
    """
    pages: list[ApiPage] = []
    page_no = 1
    while True:
        params = {
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": page_no,
            "page_count": PAGE_COUNT,
        }
        body = get_json(ENDPOINT, api_key, params)
        pages.append(ApiPage(ENDPOINT, make_request_key(params), body))

        if body.get("status") == STATUS_NO_DATA:
            break
        total_page = int(body.get("total_page") or 1)
        if page_no >= total_page:
            break
        page_no += 1
    return pages


def parse_disclosures(pages: list[ApiPage]) -> list[dict]:
    """페이지들에서 공시 행을 뽑는다. 같은 rcept_no가 여러 번 나오면 마지막 것만 남긴다.

    페이지를 넘기는 사이에 새 공시가 올라오면 행이 다음 페이지로 밀려 중복될 수 있다.
    DuckDB의 ON CONFLICT DO UPDATE는 한 문장 안에 같은 키가 두 번 있으면 실패하므로 여기서 미리 거른다.
    """
    by_rcept_no: dict[str, dict] = {}
    for page in pages:
        for item in page.body.get("list", []):
            row = {field: (item.get(field) or "").strip() for field in DISCLOSURE_FIELDS}
            by_rcept_no[row["rcept_no"]] = row
    return list(by_rcept_no.values())
