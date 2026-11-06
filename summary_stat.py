"""data/analysis.js 의 요약 수치를 하나 출력한다.

워크플로우의 데이터 회귀 가드가 사용한다. YAML 안에 여러 줄 파이썬을
끼워 넣으면 블록 스칼라가 깨지므로 별도 파일로 둔다.

사용: python summary_stat.py graded_count [기본값]
파일이 없거나 읽을 수 없으면 기본값(기본 0)을 출력하고 정상 종료한다.
"""

import json
import sys

PATH = "data/analysis.js"
PREFIX = "var STOCK_DATA = "


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "graded_count"
    default = sys.argv[2] if len(sys.argv) > 2 else "0"
    try:
        text = open(PATH, encoding="utf-8").read()
        if text.startswith(PREFIX):
            text = text[len(PREFIX):]
        else:
            text = text[text.index("=") + 1:]
        data = json.loads(text.strip().rstrip(";"))
        print(data["summary"].get(key, default))
    except Exception:
        print(default)


if __name__ == "__main__":
    main()
