"""OpenDART 고유번호(corpCode.xml)를 내려받아 4대 금융지주의 corp_code를 확인한다.

실행 (저장소 루트에서):
    python -m src.collectors.corp_code

OpenDART의 corpCode.xml API는 공시 대상 전체 회사의 고유번호 목록을
ZIP 파일(안에 CORPCODE.xml 하나)로 돌려준다. 이 스크립트는
1) ZIP을 디스크에 저장하지 않고 메모리에서 바로 풀고
2) 전체 회사를 corp_code 기준으로 색인한 뒤
3) 미리 고정해 둔 대상 corp_code가 목록에 있는지, 회사명이 기대한 대로인지 확인한다.

왜 회사명이 아니라 corp_code로 찾는가:
corpCode.xml에는 같은 이름의 회사가 여러 개 있을 수 있다. 예를 들어 "우리금융지주"는
2014년 우리은행에 합병되며 상장폐지된 옛 법인(00375302)과 2019년 재출범한 현재
법인(01350869)이 둘 다 들어 있고, 상장폐지된 회사도 stock_code가 그대로 남아 있어
"stock_code가 있으면 상장사"라는 필터로는 걸러지지 않는다.
그래서 대상 회사는 corp_code로 고정하고, 회사명은 확인용으로만 쓴다.

API 호출은 실행 1회당 1건이다.
"""

import io
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

from dotenv import load_dotenv

from src.collectors.dart_client import get_raw

CORP_CODE_ENDPOINT = "corpCode.xml"

# 대상 회사: corp_code → 기대하는 회사명(corpCode.xml의 corp_name).
# 2026-11-23 노트북에서 실제 corpCode.xml로 확인한 값.
TARGET_CORPS = {
    "00688996": "KB금융",
    "00382199": "신한지주",
    "00547583": "하나금융지주",
    "01350869": "우리금융지주",  # 옛 법인 00375302(상장폐지)와 혼동 주의
}


def get_api_key() -> str:
    """.env에서 DART_API_KEY를 읽는다. 없으면 바로 종료한다."""
    load_dotenv()
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        sys.exit("DART_API_KEY가 없습니다. .env.example을 복사해 .env를 만들고 키를 넣어주세요.")
    return api_key


def download_corp_code_xml(api_key: str) -> bytes:
    """corpCode.xml ZIP을 받아 메모리에서 풀고 XML 바이트를 돌려준다.

    HTTP 호출은 dart_client.get_raw를 거친다. 실패했을 때 에러 메시지에서 API 키가 가려진다.
    """
    response = get_raw(CORP_CODE_ENDPOINT, api_key)

    buffer = io.BytesIO(response.content)
    # 키가 틀렸거나 한도를 넘으면 ZIP 대신 오류 메시지(XML/JSON)가 온다.
    if not zipfile.is_zipfile(buffer):
        raise RuntimeError(f"ZIP이 아닌 응답을 받았습니다: {response.text[:300]}")

    with zipfile.ZipFile(buffer) as zf:
        xml_name = zf.namelist()[0]  # 보통 CORPCODE.xml
        return zf.read(xml_name)


def parse_corps(xml_bytes: bytes) -> dict[str, dict]:
    """XML을 파싱해 전체 회사를 {corp_code: 회사 정보} 형태로 돌려준다.

    상장 여부로 미리 거르지 않는다. 대상은 corp_code로 정확히 찾기 때문에
    필터가 필요 없고, 비상장 자회사를 다룰 때(Phase 2)도 같은 함수를 쓸 수 있다.
    """
    root = ET.fromstring(xml_bytes)
    corps = {}
    for item in root.iter("list"):
        corp_code = (item.findtext("corp_code") or "").strip()
        corps[corp_code] = {
            "corp_code": corp_code,
            "corp_name": (item.findtext("corp_name") or "").strip(),
            "stock_code": (item.findtext("stock_code") or "").strip(),  # 비상장사는 공백
            "modify_date": (item.findtext("modify_date") or "").strip(),
        }
    return corps


def main() -> None:
    api_key = get_api_key()
    xml_bytes = download_corp_code_xml(api_key)
    corps = parse_corps(xml_bytes)
    print(f"전체 회사 수: {len(corps):,}")

    print(f"{'회사명':<10} {'corp_code':<10} {'stock_code':<10} modify_date")
    problems = 0
    for corp_code, expected_name in TARGET_CORPS.items():
        corp = corps.get(corp_code)
        if corp is None:
            problems += 1
            print(f"{expected_name:<10} {corp_code:<10} (찾지 못함 — corp_code를 확인하세요)")
            continue
        print(f"{corp['corp_name']:<10} {corp_code:<10} {corp['stock_code']:<10} {corp['modify_date']}")
        # 사명 변경 등으로 이름이 달라졌다면 알려준다. corp_code는 그대로여도 확인이 필요하다.
        if corp["corp_name"] != expected_name:
            problems += 1
            print(f"  ⚠ 회사명이 기대값({expected_name})과 다릅니다. 사명 변경 여부를 확인하세요.")

    if problems:
        sys.exit(f"확인이 필요한 항목 {problems}건")


if __name__ == "__main__":
    main()
