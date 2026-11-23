"""OpenDART API 공통 호출 함수.

모든 수집기는 이 모듈을 거쳐 DART를 호출한다. 한곳에 모아 두는 이유는 세 가지다.

1) API 키 보호
   requests의 HTTP 에러 메시지에는 요청 URL이 그대로 들어간다.
   DART는 키를 쿼리 문자열(crtfc_key=...)로 받기 때문에, 에러를 그대로 올리면
   Dagster 로그나 터미널에 키가 찍힌다. 여기서 에러 메시지의 키를 ***로 바꾸고,
   원래 예외(URL 포함)는 연결하지 않는다(`raise ... from None`).

2) DART 상태 코드 처리
   DART는 HTTP 200으로 응답하면서 본문의 status로 성공/실패를 알린다.
   - 000: 정상
   - 013: 조회된 데이터 없음 → 에러가 아니라 "빈 결과"로 다룬다
   - 020: 요청 제한 초과(일 한도) → 다시 시도해도 소용없으므로 별도 예외로 구분한다
   - 그 밖의 코드(010 미등록 키, 800 시스템 점검 등) → DartApiError

3) 호출 간격
   짧은 시간에 몰아서 호출하지 않도록 호출 사이에 최소 간격을 둔다.
"""

import time

import requests

BASE_URL = "https://opendart.fss.or.kr/api"

STATUS_OK = "000"
STATUS_NO_DATA = "013"
STATUS_RATE_LIMIT = "020"

# 연속 호출 사이 최소 간격(초). 페이지를 넘기며 여러 번 호출할 때 적용된다.
MIN_INTERVAL_SEC = 0.2

_last_call_at = 0.0


class DartApiError(RuntimeError):
    """DART가 오류 상태를 돌려줬거나 HTTP 요청 자체가 실패했을 때."""

    def __init__(self, message: str, status: str | None = None):
        super().__init__(message)
        self.status = status


class DartRateLimitError(DartApiError):
    """일일 호출 한도 초과(status 020). 그날은 더 호출하지 말아야 한다."""


def mask_key(text: str, api_key: str) -> str:
    """문자열에 들어 있는 API 키를 ***로 바꾼다."""
    return text.replace(api_key, "***") if api_key else text


def _wait_for_interval() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_INTERVAL_SEC:
        time.sleep(MIN_INTERVAL_SEC - elapsed)
    _last_call_at = time.monotonic()


def get_raw(endpoint: str, api_key: str, params: dict | None = None, timeout: int = 60) -> requests.Response:
    """DART에 GET 요청을 보내고 응답 객체를 그대로 돌려준다 (ZIP 등 JSON이 아닌 응답용).

    HTTP 오류·네트워크 오류는 키를 가린 DartApiError로 바꿔서 올린다.
    """
    _wait_for_interval()
    url = f"{BASE_URL}/{endpoint}"
    try:
        response = requests.get(url, params={**(params or {}), "crtfc_key": api_key}, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as e:
        raise DartApiError(f"{endpoint} 요청 실패: {mask_key(str(e), api_key)}") from None
    return response


def get_json(endpoint: str, api_key: str, params: dict | None = None) -> dict:
    """JSON API를 호출하고 본문을 dict로 돌려준다.

    status가 000(정상) 또는 013(데이터 없음)이면 본문을 그대로 돌려준다.
    호출하는 쪽은 013일 때 'list' 키가 없다는 점만 신경 쓰면 된다.
    """
    response = get_raw(endpoint, api_key, params)
    try:
        body = response.json()
    except ValueError:
        raise DartApiError(f"{endpoint} 응답이 JSON이 아닙니다: {response.text[:200]}") from None

    status = body.get("status")
    if status in (STATUS_OK, STATUS_NO_DATA):
        return body
    message = f"{endpoint} 오류 status={status}: {body.get('message')}"
    if status == STATUS_RATE_LIMIT:
        raise DartRateLimitError(message, status)
    raise DartApiError(message, status)
