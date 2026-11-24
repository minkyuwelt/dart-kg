"""OpenDART 공시서류 원본파일 API (document.xml).

Phase 3 공시 원문 RAG의 원천이다. 접수번호(rcept_no) 하나를 주면 그 공시의 원문을 ZIP으로 돌려준다.
ZIP 안에는 DART 자체 형식의 XML(HTML에 가까운 태그 구조)이 들어 있다. 호출 1건 = 보고서 1건.

corpCode.xml과 같은 "ZIP 응답" API라서 dart_client.get_raw를 쓴다. 다른 점은 실패 응답의 모양이다.
접수번호가 틀리거나 한도를 넘으면 ZIP 대신 JSON(또는 XML) 오류 본문이 오므로, ZIP인지 먼저 확인하고
아니면 status 코드를 읽어 DartApiError/DartRateLimitError로 바꾼다.

이 모듈은 받은 바이트를 해석하지 않는다. 저장은 src/loaders/documents.py, 파싱은 Phase 3 다음 PR이 맡는다.
ZIP 안 파일 이름·인코딩·크기는 노트북에서 실제 1건을 받아 확인한다 (docs/plans/phase3.md "확인이 필요한 것").
"""

import io
import json
import re
import zipfile
from dataclasses import dataclass

from src.collectors.dart_client import STATUS_RATE_LIMIT, DartApiError, DartRateLimitError, get_raw

DOCUMENT_ENDPOINT = "document.xml"

RCEPT_NO_RE = re.compile(r"^\d{14}$")

# XML 선언에서 인코딩을 읽는다. 예: <?xml version="1.0" encoding="utf-8"?>
XML_ENCODING_RE = re.compile(rb"""<\?xml[^>]*encoding=["']([A-Za-z0-9_\-]+)["']""")


@dataclass(frozen=True)
class ZipEntry:
    """ZIP 안 파일 하나의 정보. 원문 형식을 파악하는 데 쓴다 (내용은 들고 있지 않음)."""

    name: str
    size: int
    encoding: str | None  # XML 선언의 encoding. 선언이 없거나 XML이 아니면 None


def fetch_document(api_key: str, rcept_no: str) -> bytes:
    """한 공시의 원문 ZIP을 바이트로 돌려준다. API 1건.

    ZIP이 아닌 응답(오류 JSON 등)은 status를 읽어 예외로 바꾼다.
    """
    if not RCEPT_NO_RE.match(rcept_no or ""):
        raise ValueError(f"rcept_no는 14자리 숫자여야 합니다: {rcept_no!r}")
    response = get_raw(DOCUMENT_ENDPOINT, api_key, {"rcept_no": rcept_no})
    content = response.content
    if zipfile.is_zipfile(io.BytesIO(content)):
        return content
    raise _error_from_body(rcept_no, content)


def _error_from_body(rcept_no: str, content: bytes) -> DartApiError:
    """ZIP이 아닌 응답 본문에서 DART status를 찾아 알맞은 예외를 만든다."""
    text = content.decode("utf-8", errors="replace")
    status = message = None
    try:
        body = json.loads(text)
        status, message = body.get("status"), body.get("message")
    except ValueError:
        m = re.search(r"<status>(\d+)</status>", text)
        if m:
            status = m.group(1)
            m2 = re.search(r"<message>(.*?)</message>", text)
            message = m2.group(1) if m2 else None
    summary = f"{DOCUMENT_ENDPOINT} rcept_no={rcept_no}: ZIP이 아닌 응답 status={status} {message or text[:200]!s}"
    if status == STATUS_RATE_LIMIT:
        return DartRateLimitError(summary, status)
    return DartApiError(summary, status)


def inspect_zip(zip_bytes: bytes) -> list[ZipEntry]:
    """ZIP 안 파일 목록과 각 XML의 선언 인코딩을 돌려준다. 파일 전체를 메모리에 올리지 않고 앞부분만 읽는다."""
    entries = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            encoding = None
            if info.filename.lower().endswith((".xml", ".html", ".htm")):
                with zf.open(info) as f:
                    head = f.read(512)
                m = XML_ENCODING_RE.search(head)
                encoding = m.group(1).decode("ascii").lower() if m else None
            entries.append(ZipEntry(info.filename, info.file_size, encoding))
    return entries
