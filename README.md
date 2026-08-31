# stock-analyzer

미국·한국 시장의 급등 후보 종목을 매일 자동 분석해 **텔레그램 알림**과 **웹 대시보드**로 전달하는 도구입니다.
GitHub Actions로 실행되고 GitHub Pages로 발행되어 별도 서버 없이 무료로 운영됩니다.

## 분석 항목

| 구분 | 내용 |
|------|------|
| 기술적 분석 | 거래량 급증, 이평선 정배열, RSI, MACD, 볼린저밴드, 모멘텀 |
| 재무 분석 | PER, PBR, 매출성장률, 영업이익률, 부채비율 |
| 패턴 분석 | 쌍바닥, 거래량-가격 추세, 변동성 스퀴즈, 연속 양봉 |

## 구성

```
analyzer.py                   분석 엔진
index.html                    대시보드
data/analysis.json            최신 분석 결과
data/history.json             누적 이력
.github/workflows/analyze.yml 매일 자동 분석
.github/workflows/deploy.yml  Pages 배포
```

## 실행

```bash
pip install -r requirements.txt
python analyzer.py
```

텔레그램 알림을 쓰려면 저장소 Secrets에 `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`를 등록하세요.
자세한 설정은 [SETUP_GUIDE.md](SETUP_GUIDE.md)를 참고하세요.

## 면책

투자 판단의 참고 자료일 뿐이며 수익을 보장하지 않습니다. 투자에 대한 책임은 본인에게 있습니다.
