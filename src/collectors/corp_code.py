"""OpenDART 고유번호(corpCode.xml)를 내려받아 4대 금융지주의 corp_code를 출력한다.

실행 (저장소 루트에서):
    python -m src.collectors.corp_code

OpenDART의 corpCode.xml API는 공시 대상 전체 회사의 고유번호 목록을
ZIP 파일(안에 CORPCODE.xml 하나)로 돌려준다. 이 스크립트는
1) ZIP을 디스크에 저장하지 않고 메모리에서 바로 풀고
2) stock_code가 있는 상장사만 남긴 뒤
3) 대상 회사명과 정확히 일치하는 행의 corp_code를 출력한다.

API 호출은 실행 1회당 1건이다.
"""

import io
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"

# corpCode.xml의 corp_name과 정확히 일치해야 한다.
TARGET_CORP_NAMES = ["KB금융", "신한지주", "하나금융지주", "우리금융지주"]


def get_api_key() -> str:
    """.env에서 DART_API_KEY를 읽는다. 없으면 바로 종료한다."""
    load_dotenv()
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        sys.exit("DART_API_KEY가 없습니다. .env.example을 복사해 .env를 만들고 키를 넣어주세요.")
    return api_key


def download_corp_code_xml(api_key: str) -> bytes:
    """corpCode.xml ZIP을 받아 메모리에서 풀고 XML 바이트를 돌려준다."""
    response = requests.get(CORP_CODE_URL, params={"crtfc_key": api_key}, timeout=60)
    response.raise_for_status()

    buffer = io.BytesIO(response.content)
    # 키가 틀렸거나 한도를 넘으면 ZIP 대신 오류 메시지(XML/JSON)가 온다.
    if not zipfile.is_zipfile(buffer):
        raise RuntimeError(f"ZIP이 아닌 응답을 받았습니다: {response.text[:300]}")

    with zipfile.ZipFile(buffer) as zf:
        xml_name = zf.namelist()[0]  # 보통 CORPCODE.xml
        return zf.read(xml_name)


def parse_listed_corps(xml_bytes: bytes) -> list[dict]:
    """XML을 파싱해 stock_code가 있는 상장사만 dict 목록으로 돌려준다."""
    root = ET.fromstring(xml_bytes)
    corps = []
    for item in root.iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        if not stock_code:
            continue  # 비상장사는 stock_code가 공백
        corps.append(
            {
                "corp_code": (item.findtext("corp_code") or "").strip(),
                "corp_name": (item.findtext("corp_name") or "").strip(),
                "stock_code": stock_code,
                "modify_date": (item.findtext("modify_date") or "").strip(),
            }
        )
    return corps


def main() -> None:
    api_key = get_api_key()
    xml_bytes = download_corp_code_xml(api_key)
    listed = parse_listed_corps(xml_bytes)
    print(f"상장사 수: {len(listed):,}")

    by_name = {}
    for corp in listed:
        if corp["corp_name"] in TARGET_CORP_NAMES:
            by_name.setdefault(corp["corp_name"], []).append(corp)

    print(f"{'회사명':<10} {'corp_code':<10} {'stock_code':<10} modify_date")
    for name in TARGET_CORP_NAMES:
        matches = by_name.get(name)
        if not matches:
            print(f"{name:<10} (찾지 못함 — corpCode.xml의 회사명을 확인하세요)")
            continue
        for corp in matches:
            print(f"{name:<10} {corp['corp_code']:<10} {corp['stock_code']:<10} {corp['modify_date']}")


if __name__ == "__main__":
    main()
