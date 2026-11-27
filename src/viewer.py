"""DART 공시 뷰어 주소. 답·리포트·화면·평가 도구가 접수번호를 사람이 열어 볼 수 있는 링크로 바꿀 때 모두 이것을 쓴다."""

VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def viewer_url(rcept_no: str) -> str:
    return VIEWER_URL.format(rcept_no=rcept_no)
