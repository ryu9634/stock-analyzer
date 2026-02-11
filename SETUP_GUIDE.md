# 🚀 급등주 예측기 - 셋업 가이드

> 미국 + 한국 시장 급등 후보를 매일 자동 분석하여
> 텔레그램 알림 + 웹 대시보드로 확인하세요!

---

## 기능 요약

| 기능 | 설명 |
|------|------|
| 📊 기술적 분석 | 거래량 급증, 이평선 정배열, RSI, MACD, 볼린저밴드, 모멘텀 |
| 📈 재무 분석 | PER, PBR, 매출성장률, 영업이익률, 부채비율 |
| 🤖 패턴 분석 | 쌍바닥, 거래량-가격 추세, 변동성 스퀴즈, 연속 양봉 |
| 📱 텔레그램 | 매일 아침 7시 TOP 10 종목 알림 |
| 🌐 대시보드 | GitHub Pages 무료 호스팅, 종목 상세 분석 |
| 💰 비용 | **완전 무료** (GitHub Actions + Pages) |

---

## 셋업 (약 20분)

### Step 1. 텔레그램 봇 (이전 뉴스봇과 동일한 봇 사용 가능)

이미 뉴스봇 텔레그램을 만드셨다면 같은 토큰/Chat ID를 쓰면 됩니다.

아직 없다면:
1. @BotFather → `/newbot` → 토큰 복사
2. 봇에게 메시지 보내기 → `https://api.telegram.org/bot{토큰}/getUpdates` → Chat ID 확인

### Step 2. GitHub 저장소 생성

1. [github.com](https://github.com) → `+` → `New repository`
2. 이름: `surge-predictor`
3. **Public** 선택 (GitHub Pages 무료 사용을 위해)
4. `Add a README` 체크 후 생성

### Step 3. 파일 업로드

저장소에 아래 파일들을 업로드:

```
surge-predictor/
├── analyzer.py                         ← 분석 엔진
├── index.html                          ← 웹 대시보드
├── requirements.txt
└── .github/
    └── workflows/
        ├── analyze.yml                 ← 자동 분석 크론잡
        └── deploy.yml                  ← 대시보드 배포
```

**웹에서 업로드:**
1. `analyzer.py`, `index.html`, `requirements.txt` → Add file → Upload
2. `.github/workflows/analyze.yml` → Add file → Create new file → 경로 입력
3. `.github/workflows/deploy.yml` → 동일

### Step 4. Secrets 등록

Settings → Secrets and variables → Actions → New repository secret

| Name | Value |
|------|-------|
| `TELEGRAM_BOT_TOKEN` | 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 본인 Chat ID |

### Step 5. GitHub Pages 활성화

1. Settings → Pages
2. Source: **GitHub Actions** 선택
3. 저장

### Step 6. Actions 권한 설정

1. Settings → Actions → General
2. "Workflow permissions" → **Read and write permissions** 선택
3. 저장

### Step 7. 첫 실행 테스트

1. Actions 탭 → `🚀 급등주 예측기 자동 분석` → Run workflow
2. 약 3~5분 후:
   - ✅ 텔레그램 알림 수신
   - ✅ `data/analysis.json` 자동 생성
   - ✅ 대시보드 접속: `https://{username}.github.io/surge-predictor`

---

## 자동 실행 스케줄

| 시간 (KST) | 설명 |
|------------|------|
| 매일 아침 7시 | 장 시작 전 분석 (평일만) |
| 매일 저녁 6시 | 장 마감 후 최종 분석 (평일만) |

---

## 커스터마이징

### 종목 추가/변경

`analyzer.py`의 `US_STOCKS`, `KR_STOCKS` 리스트를 수정:

```python
US_STOCKS = [
    "PL", "RDW", "RKLB",   # 우주항공 (기존)
    "MARA", "RIOT",          # 추가: 크립토 관련주
    "GME", "AMC",            # 추가: 밈 주식
]

KR_STOCKS = [
    "005930.KS",  # 삼성전자
    "000660.KS",  # SK하이닉스
    # .KS = KOSPI, .KQ = KOSDAQ
]
```

### 분석 가중치 변경

`analyzer.py`의 `analyze_stock` 메서드 내:

```python
# 현재: 기술 40% + 재무 25% + 패턴 25% + 뉴스 10%
total_score = tech_avg * 0.4 + fund_avg * 0.25 + pattern_avg * 0.25 + news_score * 0.1

# 기술적 분석 중심으로 변경:
total_score = tech_avg * 0.5 + fund_avg * 0.15 + pattern_avg * 0.25 + news_score * 0.1
```

### 알림 시간 변경

`.github/workflows/analyze.yml`의 cron 수정:

```yaml
# 한국시간 = UTC + 9
- cron: '0 22 * * 0-4'   # KST 아침 7시 (평일)
- cron: '0 23 * * 0-4'   # KST 아침 8시로 변경
```

---

## 향후 확장 아이디어

1. **뉴스 감정 분석** — RSS 뉴스 + LLM 호재/악재 분류
2. **백테스트** — 과거 시그널의 적중률 검증
3. **종목 스크리닝 확대** — S&P 500 전종목, KOSPI 200 전종목
4. **차트 이미지** — matplotlib으로 기술적 차트 생성 후 텔레그램 전송
5. **알림 커스텀** — 특정 조건(급등 5%+, 거래량 3배+) 시 즉시 알림

---

## 주의사항

⚠️ **이 도구는 투자 참고용입니다.**
- 어떤 분석도 100% 정확한 예측은 불가능합니다
- 스코어가 높다고 무조건 매수하지 마세요
- 반드시 본인의 판단과 리스크 관리 하에 투자하세요
- 손실에 대한 책임은 투자자 본인에게 있습니다
