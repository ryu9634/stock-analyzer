"""
Quality Stock Screener v5 — 투자하기 좋은 미국 기업 발굴기

철학: "싸게 보이는 주식"이 아니라 "좋은 기업을 합리적인 가격에" 찾는다.
단기 수급·차트는 일절 보지 않고, 기업의 돈 버는 능력과 재무 체력만 본다.

[점수 체계] 퀄리티 중심 혼합
  1. 수익성     (35%) — ROE, ROIC, 영업이익률, 순이익률, 총마진
  2. 재무건전성 (25%) — 부채비율, 유동비율, 순부채/EBITDA, FCF마진, 현금비중
  3. 성장성     (25%) — 매출성장, 이익성장, 분기이익성장, 장기 주가추세
  4. 밸류에이션 (15%) — PER·PBR·EV/EBITDA(섹터 상대), PEG, FCF수익률

[설계 원칙]
  - 섹터 상대 평가: PER 15는 유틸리티에선 비싸고 반도체에선 싸다. 밸류에이션은
    섹터 중위값 대비로 환산한다. 금융주·REIT를 일반 기업 기준으로 재지 않는다.
  - 가중치 재정규화: 은행은 유동비율이 없고 REIT는 ROE가 구조적으로 낮다.
    측정 불가 지표는 임의의 50점을 주는 대신 가중치에서 제외한다.
  - '데이터 없음'과 '나쁨'을 구분: 적자라서 PER이 없는 것은 결측이 아니라 악재다.
  - 데이터 커버리지가 낮은 기업은 등급을 주지 않는다. 재무제표가 비어 있는
    종목이 점수 상위에 올라오는 것을 막는다.
"""

import json
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from statistics import median

import numpy as np
import requests
import yfinance as yf

# 출력이 파일이나 CI 로그로 리다이렉트되면 stdout 이 블록 버퍼링된다.
# 그러면 10분 걸리는 스캔의 진행 상황이 끝날 때까지 한 줄도 보이지 않는다.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

warnings.simplefilter("default", FutureWarning)
warnings.filterwarnings("ignore", message=".*auto_adjust.*")

# ====== 설정 ======

# 시총 제한 없음 (미국 전체). 대신 '실제로 투자 가능한가'만 걸러낸다.
MIN_PRICE = 1.0            # 1달러 미만: 재무 수치가 노이즈
MIN_DOLLAR_VOLUME = 200_000  # 일 거래대금 20만달러 미만: 진입/청산 불가

PRICE_PERIOD = "1y"
PRICE_CHUNK = 400
INFO_WORKERS = 8          # 지속 대량 조회 시 Yahoo 가 스로틀을 건다. 14 은 과했다.
INFO_RETRIES = 3
FETCH_BUDGET_SEC = 900     # 재무 조회 시간 상한 (Actions 타임아웃 보호)

MIN_COVERAGE = 0.55        # 등급 부여 최소 데이터 커버리지
FULL_DETAIL_TOP = 400      # 상세 지표를 저장할 상위 종목 수

CACHE_PATH = "data/.fundamentals_cache.json"
CACHE_TTL_HOURS = 72      # 재무는 분기 실적 때만 바뀐다. 길게 잡아 API 부하를 줄인다.

SECTOR_MIN_SAMPLES = 8     # 섹터 중위값을 신뢰할 최소 표본

# 기업이 아닌 상장물 제외
EXCLUDE_NAME_PATTERNS = (
    "etf", "etn", "fund", "trust", "index", "ishares", "spdr", "vanguard",
    "proshares", "direxion", "invesco", "acquisition corp", "blank check",
    "warrant", "right", "unit", "depositary", "preferred", "notes due",
    "preference", "%", "closed end", "municipal", "bond",
)

CATEGORY_WEIGHTS = {
    "profitability": 0.35,
    "health": 0.25,
    "growth": 0.25,
    "valuation": 0.15,
}


def fmt_money(v):
    if not v:
        return "N/A"
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.2f}T"
    if a >= 1e9:
        return f"${v / 1e9:.1f}B"
    if a >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def num(v):
    """None/NaN/inf를 걸러낸 float 변환."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return f


def is_financial(sector, industry=""):
    """은행·보험·리츠는 일반 기업의 재무 지표가 그대로 적용되지 않는다.

    예금이 부채로 잡히고 현금이 시총을 넘는 것이 정상이므로, 일반 제조/서비스
    기업 기준으로 재면 체계적으로 왜곡된다.
    """
    s = (sector or "").lower()
    i = (industry or "").lower()
    if "financ" in s or "real estate" in s:
        return True
    return any(k in i for k in ("bank", "insurance", "reit", "savings",
                                "investment", "asset management", "brokerage"))


def grade_of(score):
    if score >= 80:
        return "A+"
    if score >= 72:
        return "A"
    if score >= 64:
        return "B+"
    if score >= 56:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def band(value, table, fallback):
    """높을수록 좋은 지표. (임계값, 점수) 내림차순 테이블."""
    for threshold, score in table:
        if value >= threshold:
            return score
    return fallback


def band_low(value, table, fallback):
    """낮을수록 좋은 지표. (상한, 점수) 오름차순 테이블.

    부채비율·PER 같은 지표는 '작으면 좋다'. 내림차순 테이블을 재활용하면
    중간 구간이 최저점을 받는 버그가 생기므로 전용 헬퍼를 둔다.
    """
    for threshold, score in table:
        if value < threshold:
            return score
    return fallback


# ====== 유니버스 ======

class UniverseFetcher:
    """Nasdaq 스크리너 — download=true로 미국 전체를 1회 호출로 받는다."""

    URL = ("https://api.nasdaq.com/api/screener/stocks"
           "?tableonly=true&limit=25000&offset=0&download=true")
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept": "application/json",
    }

    @staticmethod
    def _money(s):
        if not s:
            return None
        s = str(s).replace("$", "").replace(",", "").strip()
        if not s or s in ("N/A", "--"):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    @staticmethod
    def _is_company(symbol, name):
        if not symbol or not symbol.isalpha() or len(symbol) > 5:
            return False
        low = name.lower()
        return not any(p in low for p in EXCLUDE_NAME_PATTERNS)

    @classmethod
    def fetch(cls):
        rows = []
        for attempt in range(3):
            try:
                resp = requests.get(cls.URL, headers=cls.HEADERS, timeout=60)
                payload = resp.json()["data"]
                rows = payload.get("rows") or payload["table"]["rows"]
                break
            except Exception as e:
                wait = 2 ** attempt
                print(f"  ⚠️ Nasdaq 재시도 {attempt + 1}/3 ({wait}s): {e}")
                time.sleep(wait)
        if not rows:
            raise RuntimeError("Nasdaq 스크리너 응답을 받지 못했습니다")

        print(f"  📡 Nasdaq 응답: {len(rows)}개 상장물")

        meta = {}
        skipped_kind = skipped_liquidity = 0
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            name = (r.get("name") or sym).strip()

            if not cls._is_company(sym, name):
                skipped_kind += 1
                continue

            price = cls._money(r.get("lastsale"))
            volume = cls._money(r.get("volume"))
            mcap = cls._money(r.get("marketCap"))

            if price is None or price < MIN_PRICE:
                skipped_liquidity += 1
                continue
            if volume is None or price * volume < MIN_DOLLAR_VOLUME:
                skipped_liquidity += 1
                continue
            if not mcap or mcap <= 0:
                skipped_liquidity += 1
                continue

            meta[sym] = {
                "name": name,
                "market_cap": mcap,
                "sector": (r.get("sector") or "").strip(),
                "industry": (r.get("industry") or "").strip(),
                "country": (r.get("country") or "").strip(),
                "ipo_year": (r.get("ipoyear") or "").strip(),
            }

        print(f"  🧹 제외: 비기업 상장물 {skipped_kind}개, "
              f"가격/유동성 미달 {skipped_liquidity}개")
        print(f"  ✅ 분석 대상: {len(meta)}개 기업")
        return meta


# ====== 재무 데이터 ======

class FundamentalsFetcher:
    """yfinance .info 병렬 수집 + 로컬 캐시."""

    KEEP = (
        "quoteType", "marketCap", "enterpriseValue", "trailingPE", "forwardPE",
        "priceToBook", "pegRatio", "trailingPegRatio", "enterpriseToEbitda",
        "returnOnEquity", "returnOnAssets", "profitMargins", "operatingMargins",
        "grossMargins", "revenueGrowth", "earningsGrowth",
        "earningsQuarterlyGrowth", "debtToEquity", "currentRatio", "totalDebt",
        "totalCash", "freeCashflow", "operatingCashflow", "totalRevenue",
        "ebitda", "netIncomeToCommon", "trailingEps", "dividendYield",
        "payoutRatio", "sharesOutstanding", "sector", "industry", "longName",
        "beta", "numberOfAnalystOpinions", "targetMeanPrice",
    )

    def __init__(self, path=CACHE_PATH, ttl_hours=CACHE_TTL_HOURS):
        self.path = path
        self.ttl = timedelta(hours=ttl_hours)
        self.cache = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _fresh(self, ticker):
        entry = self.cache.get(ticker)
        if not entry:
            return None
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(entry["_at"])
        except Exception:
            return None
        return entry if age < self.ttl else None

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, separators=(",", ":"))
        except Exception as e:
            print(f"  ⚠️ 캐시 저장 실패: {e}")

    def _fetch_one(self, ticker):
        for attempt in range(INFO_RETRIES + 1):
            try:
                info = yf.Ticker(ticker).info or {}
                if info:
                    slim = {k: info.get(k) for k in self.KEEP if info.get(k) is not None}
                    slim["_at"] = datetime.now(timezone.utc).isoformat()
                    return ticker, slim
                raise ValueError("empty info")
            except Exception as e:
                if attempt >= INFO_RETRIES:
                    break
                msg = str(e).lower()
                # 429/401(Invalid Crumb)은 서버가 속도를 낮추라는 신호다.
                # 0.5초 재시도는 오히려 상황을 악화시키므로 길게 쉰다.
                throttled = any(k in msg for k in
                                ("429", "too many", "unauthorized", "crumb", "rate"))
                base = 6.0 if throttled else 0.8
                time.sleep(base * (2 ** attempt))
        return ticker, None

    def fetch_all(self, tickers):
        cached, pending = {}, []
        for t in tickers:
            hit = self._fresh(t)
            if hit:
                cached[t] = hit
            else:
                pending.append(t)

        print(f"  💾 캐시 적중 {len(cached)}개 / 신규 조회 {len(pending)}개")
        out = dict(cached)
        if not pending:
            return out

        t0 = time.time()
        done = failed = 0
        with ThreadPoolExecutor(max_workers=INFO_WORKERS) as ex:
            futures = {ex.submit(self._fetch_one, t): t for t in pending}
            for fut in as_completed(futures):
                done += 1
                try:
                    ticker, slim = fut.result()
                except Exception:
                    failed += 1
                    continue
                if slim:
                    out[ticker] = slim
                    self.cache[ticker] = slim
                else:
                    failed += 1

                if done % 500 == 0:
                    rate = done / max(time.time() - t0, 1e-9)
                    left = (len(pending) - done) / max(rate, 1e-9)
                    print(f"  📥 재무 {done}/{len(pending)} "
                          f"({rate:.0f}/s, 남은시간 ~{left / 60:.1f}분)")

                if time.time() - t0 > FETCH_BUDGET_SEC:
                    print(f"  ⏱️ 조회 예산 {FETCH_BUDGET_SEC}s 초과 — "
                          f"{done}/{len(pending)}에서 중단")
                    for f2 in futures:
                        f2.cancel()
                    break

        rate = len(out) / max(len(tickers), 1)
        print(f"  ✅ 재무 수집 완료: {len(out)}개 "
              f"({time.time() - t0:.0f}초, 실패 {failed}개, 커버리지 {rate:.0%})")
        if rate < 0.7:
            print(f"  ⚠️ 재무 커버리지 {rate:.0%} — API 레이트리밋 가능성이 높습니다.")
            print("     캐시가 남아 있으므로 잠시 후 다시 실행하면 누락분만 채웁니다.")
        self._save()
        return out


# ====== 가격 지표 ======

class PriceMetrics:
    """OHLCV에서 뽑는 보조 지표. 매매 타이밍이 아니라 '시장의 평가' 참고용."""

    @staticmethod
    def compute(close):
        c = close.dropna()
        if len(c) < 60:
            return None

        last = float(c.iloc[-1])

        def ret(days, min_bars=None):
            """days 거래일 전 대비 수익률.

            period="1y" 로 받으면 거래일이 약 250일이라 251일 전 값을 요구하면
            항상 None 이 된다. 가용 범위로 보정하되, 최소 거래일 수를 채우지
            못하면 (신규 상장 등) 기간을 속이지 않고 None 을 반환한다.
            """
            need = min_bars if min_bars is not None else days
            if len(c) <= need:
                return None
            idx = min(days, len(c) - 1)
            prev = float(c.iloc[-idx - 1])
            return (last / prev - 1) * 100 if prev > 0 else None

        hi, lo = float(c.max()), float(c.min())
        pos = (last - lo) / (hi - lo) * 100 if hi > lo else 50.0

        logret = np.log(c / c.shift(1)).dropna()
        vol = float(logret.std() * np.sqrt(252) * 100) if len(logret) > 20 else None

        runmax = c.cummax()
        mdd = float(((c / runmax - 1) * 100).min())

        step = max(1, len(c) // 52)
        pts = c.iloc[::step].values[-52:]
        p_min, p_max = float(pts.min()), float(pts.max())
        span = p_max - p_min if p_max > p_min else 1.0
        spark = [round((float(p) - p_min) / span * 100) for p in pts]

        return {
            "price": round(last, 2),
            "return_1m": None if ret(21) is None else round(ret(21), 1),
            "return_3m": None if ret(63) is None else round(ret(63), 1),
            # 1년 = 약 251 거래일. 최소 200 거래일(약 10개월)은 있어야
            # '1년 수익률'이라 부를 수 있다.
            "return_1y": None if ret(251, 200) is None else round(ret(251, 200), 1),
            "range_pos": round(pos, 1),
            "volatility": None if vol is None else round(vol, 1),
            "max_drawdown": round(mdd, 1),
            "sparkline": spark,
        }


# ====== 카테고리별 채점 ======

class Profitability:
    """수익성 — 이 기업이 얼마나 효율적으로 돈을 버는가."""

    @staticmethod
    def roe(v):
        if v is None:
            return None, "N/A"
        pct = v * 100
        if v <= 0:
            return 5, f"ROE {pct:.1f}% 손실"
        # 과도한 ROE는 자기자본 축소/레버리지 부작용이므로 만점을 주지 않는다
        if v > 1.0:
            return 88, f"ROE {pct:.0f}% (레버리지 영향)"
        score = band(v, [(0.40, 92), (0.25, 95), (0.18, 85),
                         (0.12, 70), (0.08, 55), (0.03, 35)], 20)
        return score, f"ROE {pct:.1f}%"

    @staticmethod
    def roic(op_margin, revenue, total_debt, market_cap, pbr, cash):
        """ROIC ≈ 영업이익 / (순차입금 + 자기자본장부가)"""
        if None in (op_margin, revenue, market_cap, pbr) or pbr <= 0 or revenue <= 0:
            return None, "N/A"
        book_equity = market_cap / pbr
        invested = book_equity + (total_debt or 0) - (cash or 0)
        if invested <= 0:
            return None, "N/A"
        r = (op_margin * revenue) / invested
        if r <= 0:
            return 8, f"ROIC {r * 100:.1f}% 손실"
        if r > 1.0:
            # 투하자본이 극히 작거나 일회성 이익이 섞인 경우. 만점을 주지 않는다.
            return 85, f"ROIC {r * 100:.0f}% (비정상 과대)"
        score = band(r, [(0.20, 95), (0.15, 85), (0.10, 70),
                         (0.06, 52), (0.03, 35)], 20)
        return score, f"ROIC {r * 100:.1f}%"

    @staticmethod
    def operating_margin(v):
        if v is None:
            return None, "N/A"
        if v <= 0:
            return 5, f"영업이익률 {v * 100:.1f}% 적자"
        score = band(v, [(0.30, 95), (0.20, 85), (0.12, 70),
                         (0.06, 52), (0.02, 35)], 22)
        return score, f"영업이익률 {v * 100:.1f}%"

    @staticmethod
    def net_margin(v, op_margin=None):
        """순이익률. 단, 영업이익률을 크게 웃돌면 일회성 이익으로 본다.

        자산매각·세금환입·소송합의금 같은 비영업 이익은 TTM 순이익을 부풀린다.
        지속가능한 수익성은 영업에서 나오므로, 영업이익률로 상한을 씌운다.
        """
        if v is None:
            return None, "N/A"
        if v <= 0:
            return 5, f"순이익률 {v * 100:.1f}% 적자"
        score = band(v, [(0.25, 95), (0.15, 83), (0.08, 68), (0.04, 50)], 30)

        if op_margin is not None and v > op_margin * 1.5 + 0.05:
            op_score = Profitability.operating_margin(op_margin)[0]
            if op_score is not None and op_score < score:
                return op_score, (f"순이익률 {v * 100:.1f}% "
                                  f"(영업 {op_margin * 100:.1f}% 대비 과대)")
        return score, f"순이익률 {v * 100:.1f}%"

    @staticmethod
    def gross_margin(v):
        if v is None:
            return None, "N/A"
        if v <= 0:
            return 10, "총마진 음수"
        score = band(v, [(0.60, 92), (0.45, 80), (0.32, 65), (0.20, 48)], 30)
        return score, f"총마진 {v * 100:.0f}%"


class FinancialHealth:
    """재무건전성 — 불황을 버틸 체력이 있는가."""

    @staticmethod
    def debt_to_equity(v):
        # yfinance는 퍼센트로 준다 (84.3 == 0.84배)
        if v is None:
            return None, "N/A"
        x = v / 100
        score = band_low(x, [(0.2, 95), (0.5, 85), (1.0, 70),
                             (1.5, 55), (2.5, 38), (4.0, 22)], 8)
        return score, f"부채비율 {x:.2f}배"

    @staticmethod
    def current_ratio(v):
        if v is None:
            return None, "N/A"
        score = band(v, [(2.5, 92), (2.0, 85), (1.5, 72),
                         (1.2, 58), (1.0, 45), (0.8, 28)], 10)
        return score, f"유동비율 {v:.2f}"

    @staticmethod
    def net_debt_ebitda(total_debt, cash, ebitda):
        if ebitda is None or ebitda <= 0:
            return None, "N/A"
        net_debt = (total_debt or 0) - (cash or 0)
        x = net_debt / ebitda
        if x < 0:
            return 98, f"순현금 ({abs(x):.1f}배)"
        score = band_low(x, [(1.0, 88), (2.0, 75), (3.0, 60),
                             (4.0, 42), (5.5, 25)], 8)
        return score, f"순부채/EBITDA {x:.1f}배"

    @staticmethod
    def fcf_margin(fcf, revenue):
        if fcf is None or not revenue or revenue <= 0:
            return None, "N/A"
        m = fcf / revenue
        if m <= 0:
            return 12, f"FCF마진 {m * 100:.1f}% 음수"
        score = band(m, [(0.20, 95), (0.12, 83), (0.06, 68), (0.02, 52)], 38)
        return score, f"FCF마진 {m * 100:.1f}%"

    @staticmethod
    def cash_position(cash, market_cap, financial=False):
        if cash is None or not market_cap or market_cap <= 0:
            return None, "N/A"
        r = cash / market_cap
        if financial:
            # 은행·보험은 예금/운용자산이 현금으로 잡혀 시총을 훌쩍 넘는다.
            # 일반 기업의 '현금 여력'과 의미가 달라 점수화하지 않는다.
            return None, f"현금/시총 {r * 100:.0f}% (금융업 — 해당없음)"
        if r > 1.5:
            # 비금융기업이 시총의 1.5배 현금 보유 = 시총 오류이거나 청산성 쉘.
            return 45, f"현금/시총 {r * 100:.0f}% (데이터 의심)"
        score = band(r, [(0.30, 90), (0.20, 80), (0.10, 65), (0.05, 50)], 35)
        return score, f"현금/시총 {r * 100:.0f}%"


class Growth:
    """성장성 — 사업이 커지고 있는가."""

    @staticmethod
    def revenue_growth(v):
        if v is None:
            return None, "N/A"
        if v > 2.0:
            # 200%+ 성장은 거의 항상 기저효과(직전 매출이 0에 가까움),
            # 인수합병, 또는 일회성 계약이다. 지속 성장으로 보지 않는다.
            return 60, f"매출성장 {v * 100:+.0f}% (기저효과 의심)"
        score = band(v, [(0.30, 95), (0.20, 87), (0.12, 75), (0.06, 62),
                         (0.02, 48), (0.0, 35), (-0.05, 22)], 8)
        return score, f"매출성장 {v * 100:+.1f}%"

    @staticmethod
    def earnings_growth(v):
        if v is None:
            return None, "N/A"
        if v > 3.0:
            return 62, f"이익성장 {v * 100:+.0f}% (기저효과 의심)"
        score = band(v, [(0.30, 93), (0.15, 80), (0.07, 66),
                         (0.0, 50), (-0.15, 30)], 10)
        return score, f"이익성장 {v * 100:+.1f}%"

    @staticmethod
    def quarterly_growth(v):
        if v is None:
            return None, "N/A"
        score = band(v, [(0.25, 90), (0.10, 76), (0.03, 62),
                         (0.0, 50), (-0.15, 32)], 14)
        return score, f"분기이익 {v * 100:+.1f}%"

    @staticmethod
    def price_trend(ret_1y):
        """장기 주가추세 = 시장이 이 기업의 성장을 인정하는지에 대한 약한 신호."""
        if ret_1y is None:
            return None, "N/A"
        r = ret_1y / 100
        score = band(r, [(0.30, 85), (0.15, 72), (0.05, 60),
                         (-0.05, 50), (-0.20, 35)], 18)
        return score, f"1년 주가 {ret_1y:+.1f}%"


class Valuation:
    """밸류에이션 — 좋은 기업을 비싸게 사면 좋은 투자가 아니다.

    섹터 중위값 대비로 환산한다. PER 15는 유틸리티에선 비싸고 반도체에선 싸다.
    """

    @staticmethod
    def _relative(value, sector_median, label, unit=""):
        if value is None or value <= 0:
            return None, "N/A"
        if not sector_median or sector_median <= 0:
            # 섹터 표본 부족 — 절대 기준으로 대체
            return None, f"{label} {value:.1f}{unit} (섹터비교 불가)"
        r = value / sector_median
        score = band_low(r, [(0.5, 95), (0.7, 85), (0.9, 72),
                             (1.1, 58), (1.4, 42), (2.0, 26)], 10)
        return score, f"{label} {value:.1f}{unit} (섹터 {r:.0%})"

    @staticmethod
    def per(pe, sector_median, is_profitable):
        if pe is None or pe <= 0:
            # 결측이 아니라 '적자라서 PER이 없음' — 퀄리티 관점에선 악재다
            return (18, "PER 없음 (적자)") if is_profitable is False else (None, "N/A")
        if pe > 100:
            return 12, f"PER {pe:.0f} 과열"
        return Valuation._relative(pe, sector_median, "PER")

    @staticmethod
    def pbr(v, sector_median):
        return Valuation._relative(v, sector_median, "PBR")

    @staticmethod
    def ev_ebitda(v, sector_median):
        if v is not None and 0 < v < 1.0:
            # EV 가 EBITDA 1배 미만인 기업은 사실상 없다 — 단위 불일치 신호
            return None, f"EV/EBITDA {v:.1f} (단위 불일치 의심)"
        return Valuation._relative(v, sector_median, "EV/EBITDA")

    @staticmethod
    def peg(v):
        if v is None or v <= 0:
            return None, "N/A"
        score = band_low(v, [(0.8, 95), (1.2, 82), (1.8, 65),
                             (2.5, 48), (4.0, 30)], 12)
        return score, f"PEG {v:.2f}"

    @staticmethod
    def fcf_yield(fcf, market_cap):
        if fcf is None or not market_cap or market_cap <= 0:
            return None, "N/A"
        y = fcf / market_cap
        if y <= 0:
            return 12, f"FCF수익률 {y * 100:.1f}%"
        if y > 1.0:
            # 연간 FCF 가 시가총액을 넘는 기업은 없다. 해외 상장(ADS)에서
            # 재무제표는 현지통화, 시총은 USD 로 잡히는 단위 불일치가 전형적이다.
            # 저평가로 읽히면 안 되므로 점수화하지 않는다.
            return None, f"FCF수익률 {y * 100:.0f}% (단위 불일치 의심)"
        score = band(y, [(0.10, 95), (0.07, 85), (0.045, 72), (0.025, 55)], 38)
        return score, f"FCF수익률 {y * 100:.1f}%"


# ====== 메인 엔진 ======

class QualityScreener:

    def __init__(self):
        self.meta = {}
        self.fundamentals = {}
        self.prices = {}
        self.raw = {}
        self.sector_medians = {}
        self.results = []
        self.summary = {}

    # --- 1단계: 가격 데이터 ---

    def download_prices(self, tickers):
        out = {}
        chunks = [tickers[i:i + PRICE_CHUNK] for i in range(0, len(tickers), PRICE_CHUNK)]
        for n, chunk in enumerate(chunks, 1):
            print(f"  📦 가격 {n}/{len(chunks)} ({len(chunk)}종목)")
            try:
                df = yf.download(chunk, period=PRICE_PERIOD, group_by="ticker",
                                 threads=True, progress=False, timeout=45,
                                 auto_adjust=True)
                if df is None or df.empty:
                    continue
                for t in chunk:
                    try:
                        close = df[t]["Close"] if len(chunk) > 1 else df["Close"]
                    except (KeyError, TypeError):
                        continue
                    m = PriceMetrics.compute(close)
                    if m:
                        out[t] = m
            except Exception as e:
                print(f"    ⚠️ 가격 다운로드 오류: {e}")
            if n < len(chunks):
                time.sleep(0.5)
        return out

    # --- 2단계: 원시 지표 ---

    def build_raw(self):
        """채점 전에 원시 수치를 모아둔다. 섹터 중위값 계산에 필요하다."""
        for ticker, meta in self.meta.items():
            info = self.fundamentals.get(ticker)
            price = self.prices.get(ticker)
            if not info or not price:
                continue
            if info.get("quoteType") and info["quoteType"] != "EQUITY":
                continue

            # 시총: Nasdaq을 1차 신뢰. yfinance와 5배 이상 벌어지면 Nasdaq 유지.
            mcap = meta["market_cap"]
            yf_mcap = num(info.get("marketCap"))
            if yf_mcap and mcap and 0.2 < (yf_mcap / mcap) < 5:
                mcap = yf_mcap

            sector = meta["sector"] or (info.get("sector") or "")
            profit_margin = num(info.get("profitMargins"))

            self.raw[ticker] = {
                "name": meta["name"],
                "sector": sector,
                "industry": meta["industry"] or (info.get("industry") or ""),
                "market_cap": mcap,
                "price": price,
                "roe": num(info.get("returnOnEquity")),
                "roa": num(info.get("returnOnAssets")),
                "op_margin": num(info.get("operatingMargins")),
                "net_margin": profit_margin,
                "gross_margin": num(info.get("grossMargins")),
                "revenue": num(info.get("totalRevenue")),
                "ebitda": num(info.get("ebitda")),
                "total_debt": num(info.get("totalDebt")),
                "cash": num(info.get("totalCash")),
                "fcf": num(info.get("freeCashflow")),
                "debt_to_equity": num(info.get("debtToEquity")),
                "current_ratio": num(info.get("currentRatio")),
                "revenue_growth": num(info.get("revenueGrowth")),
                "earnings_growth": num(info.get("earningsGrowth")),
                "quarterly_growth": num(info.get("earningsQuarterlyGrowth")),
                "pe": num(info.get("trailingPE")),
                "forward_pe": num(info.get("forwardPE")),
                "pbr": num(info.get("priceToBook")),
                "peg": num(info.get("trailingPegRatio")) or num(info.get("pegRatio")),
                "ev_ebitda": num(info.get("enterpriseToEbitda")),
                "dividend_yield": num(info.get("dividendYield")),
                "payout_ratio": num(info.get("payoutRatio")),
                "beta": num(info.get("beta")),
                "eps": num(info.get("trailingEps")),
                "target_price": num(info.get("targetMeanPrice")),
                "analysts": num(info.get("numberOfAnalystOpinions")),
                "is_profitable": None if profit_margin is None else profit_margin > 0,
                "is_financial": is_financial(sector, meta["industry"] or info.get("industry") or ""),
            }
        print(f"  ✅ 원시 지표 구성: {len(self.raw)}개 기업")

    def compute_sector_medians(self):
        buckets = {}
        for r in self.raw.values():
            sec = r["sector"]
            if not sec:
                continue
            b = buckets.setdefault(sec, {"pe": [], "pbr": [], "ev_ebitda": []})
            for key in ("pe", "pbr", "ev_ebitda"):
                v = r[key]
                if v is not None and 0 < v < 300:
                    b[key].append(v)

        for sec, b in buckets.items():
            entry = {}
            for key, vals in b.items():
                entry[key] = median(vals) if len(vals) >= SECTOR_MIN_SAMPLES else None
            self.sector_medians[sec] = entry

        usable = sum(1 for e in self.sector_medians.values() if e.get("pe"))
        print(f"  📊 섹터 중위값: {len(self.sector_medians)}개 섹터 "
              f"(PER 비교 가능 {usable}개)")

    # --- 3단계: 채점 ---

    @staticmethod
    def _wavg(items):
        """None 점수는 가중치에서 제외하고 재정규화. 반환: (점수, 커버리지)"""
        valid = [i for i in items if i["score"] is not None]
        if not valid:
            return None, 0.0
        tw = sum(i["w"] for i in valid)
        total_w = sum(i["w"] for i in items)
        avg = sum(i["score"] * i["w"] for i in valid) / tw
        return avg, tw / total_w

    def score_one(self, ticker, r):
        sm = self.sector_medians.get(r["sector"], {})

        prof = [
            {"name": "ROE", "w": 25, **dict(zip(("score", "value"), Profitability.roe(r["roe"])))},
            {"name": "ROIC", "w": 25, **dict(zip(("score", "value"), Profitability.roic(
                r["op_margin"], r["revenue"], r["total_debt"], r["market_cap"],
                r["pbr"], r["cash"])))},
            {"name": "영업이익률", "w": 20, **dict(zip(("score", "value"), Profitability.operating_margin(r["op_margin"])))},
            {"name": "순이익률", "w": 15, **dict(zip(("score", "value"), Profitability.net_margin(r["net_margin"], r["op_margin"])))},
            {"name": "총마진", "w": 15, **dict(zip(("score", "value"), Profitability.gross_margin(r["gross_margin"])))},
        ]

        health = [
            {"name": "부채비율", "w": 25, **dict(zip(("score", "value"), FinancialHealth.debt_to_equity(r["debt_to_equity"])))},
            {"name": "유동비율", "w": 20, **dict(zip(("score", "value"), FinancialHealth.current_ratio(r["current_ratio"])))},
            {"name": "순부채/EBITDA", "w": 20, **dict(zip(("score", "value"), FinancialHealth.net_debt_ebitda(
                r["total_debt"], r["cash"], r["ebitda"])))},
            {"name": "FCF 마진", "w": 20, **dict(zip(("score", "value"), FinancialHealth.fcf_margin(r["fcf"], r["revenue"])))},
            {"name": "현금 비중", "w": 15, **dict(zip(("score", "value"), FinancialHealth.cash_position(r["cash"], r["market_cap"], r["is_financial"])))},
        ]

        growth = [
            {"name": "매출성장률", "w": 30, **dict(zip(("score", "value"), Growth.revenue_growth(r["revenue_growth"])))},
            {"name": "이익성장률", "w": 30, **dict(zip(("score", "value"), Growth.earnings_growth(r["earnings_growth"])))},
            {"name": "분기 이익성장", "w": 20, **dict(zip(("score", "value"), Growth.quarterly_growth(r["quarterly_growth"])))},
            {"name": "1년 주가추세", "w": 20, **dict(zip(("score", "value"), Growth.price_trend(r["price"]["return_1y"])))},
        ]

        val = [
            {"name": "PER", "w": 25, **dict(zip(("score", "value"), Valuation.per(r["pe"], sm.get("pe"), r["is_profitable"])))},
            {"name": "PBR", "w": 20, **dict(zip(("score", "value"), Valuation.pbr(r["pbr"], sm.get("pbr"))))},
            {"name": "PEG", "w": 20, **dict(zip(("score", "value"), Valuation.peg(r["peg"])))},
            {"name": "EV/EBITDA", "w": 20, **dict(zip(("score", "value"), Valuation.ev_ebitda(r["ev_ebitda"], sm.get("ev_ebitda"))))},
            {"name": "FCF 수익률", "w": 15, **dict(zip(("score", "value"), Valuation.fcf_yield(r["fcf"], r["market_cap"])))},
        ]

        cats = {}
        coverages = []
        for key, items in (("profitability", prof), ("health", health),
                           ("growth", growth), ("valuation", val)):
            score, cov = self._wavg(items)
            cats[key] = score
            coverages.append(cov * CATEGORY_WEIGHTS[key])

        coverage = sum(coverages)

        # 카테고리 자체가 비어 있으면 그 비중도 재정규화한다
        avail = {k: v for k, v in cats.items() if v is not None}
        if not avail:
            return None
        wsum = sum(CATEGORY_WEIGHTS[k] for k in avail)
        total = sum(v * CATEGORY_WEIGHTS[k] for k, v in avail.items()) / wsum

        graded = coverage >= MIN_COVERAGE
        # 등급은 반드시 저장되는(반올림된) 점수에서 산출한다.
        # 원시 점수로 등급을 매기면 63.96 -> 저장 64.0 인데 등급은 B 가 되어
        # 화면의 점수와 등급이 어긋난다.
        rounded = round(total, 1)
        flags = self._flags(r, cats)

        return {
            "ticker": ticker,
            "name": r["name"],
            "sector": r["sector"],
            "industry": r["industry"],
            "price": r["price"]["price"],
            "market_cap": r["market_cap"],
            "market_cap_fmt": fmt_money(r["market_cap"]),
            "total_score": rounded,
            "grade": grade_of(rounded) if graded else "—",
            "graded": graded,
            "coverage": round(coverage, 2),
            "profitability_score": None if cats["profitability"] is None else round(cats["profitability"], 1),
            "health_score": None if cats["health"] is None else round(cats["health"], 1),
            "growth_score": None if cats["growth"] is None else round(cats["growth"], 1),
            "valuation_score": None if cats["valuation"] is None else round(cats["valuation"], 1),
            "roe": None if r["roe"] is None else round(r["roe"] * 100, 1),
            "op_margin": None if r["op_margin"] is None else round(r["op_margin"] * 100, 1),
            "revenue_growth": None if r["revenue_growth"] is None else round(r["revenue_growth"] * 100, 1),
            "pe": None if r["pe"] is None else round(r["pe"], 1),
            "pbr": None if r["pbr"] is None else round(r["pbr"], 2),
            "debt_to_equity": None if r["debt_to_equity"] is None else round(r["debt_to_equity"] / 100, 2),
            "dividend_yield": None if r["dividend_yield"] is None else round(r["dividend_yield"], 2),
            "beta": None if r["beta"] is None else round(r["beta"], 2),
            "return_1m": r["price"]["return_1m"],
            "return_3m": r["price"]["return_3m"],
            "return_1y": r["price"]["return_1y"],
            "range_pos": r["price"]["range_pos"],
            "max_drawdown": r["price"]["max_drawdown"],
            "sparkline": r["price"]["sparkline"],
            "upside": self._upside(r),
            "flags": flags,
            "details": {
                "profitability": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in prof],
                "health": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in health],
                "growth": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in growth],
                "valuation": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in val],
            },
        }

    @staticmethod
    def _upside(r):
        tp, px = r["target_price"], r["price"]["price"]
        if not tp or not px or px <= 0 or not r["analysts"]:
            return None
        return round((tp / px - 1) * 100, 1)

    @staticmethod
    def _flags(r, cats):
        """플래그 생성. 각 판정 조건은 한 번만 계산한다.

        같은 조건을 두 곳에서 따로 계산하면 임계값을 한쪽만 바꿨을 때
        경고 표시와 긍정 플래그 억제가 조용히 어긋난다.
        """
        nm, om = r["net_margin"], r["op_margin"]
        rg, dy = r["revenue_growth"], r["dividend_yield"]
        de = None if r["debt_to_equity"] is None else r["debt_to_equity"] / 100
        health = cats["health"]

        # --- 수치 왜곡 판정 ---
        one_off = nm is not None and om is not None and nm > om * 1.5 + 0.05
        base_effect = rg is not None and rg > 2.0
        cash_odd = bool(not r["is_financial"] and r["cash"] and r["market_cap"]
                        and r["cash"] / r["market_cap"] > 1.5)
        unit_odd = bool(r["fcf"] and r["market_cap"] and r["fcf"] / r["market_cap"] > 1.0)
        suspect = one_off or base_effect or cash_odd or unit_odd

        warn, good = [], []

        if one_off:
            warn.append("⚠️ 일회성 이익 의심")
        if base_effect:
            warn.append("⚠️ 기저효과 성장")
        if cash_odd or unit_odd:
            warn.append("⚠️ 재무수치 이상")
        if r["is_profitable"] is False:
            warn.append("🚩 적자")
        if rg is not None and rg < 0:
            warn.append("📉 매출 역성장")
        if de is not None and de > 2.5:
            warn.append("⚠️ 부채 과다")
        if health is not None and health < 40:
            warn.append("⚠️ 재무 취약")

        # 종합 우량 플래그는 수치가 의심스러우면 주지 않는다
        if not suspect and all(v is not None and v >= 60 for v in cats.values()):
            good.append("⭐ 올라운더")
        if (not suspect and cats["profitability"] is not None
                and cats["profitability"] >= 75
                and cats["valuation"] is not None and cats["valuation"] >= 65):
            good.append("💰 저평가 우량주")

        if r["roe"] is not None and 0.20 <= r["roe"] <= 1.0:
            good.append("💎 고ROE")
        if om is not None and om >= 0.25:
            good.append("🏰 고마진")
        if de is not None and de < 0.3:
            good.append("🏦 저부채")
        if r["fcf"] is not None and r["revenue"] and r["fcf"] / r["revenue"] >= 0.15:
            good.append("💵 현금창출 우수")
        if rg is not None and 0.20 <= rg <= 2.0:
            good.append("🚀 고성장")
        if dy is not None and dy >= 3:
            good.append("🎁 고배당")

        # 표에는 첫 플래그만 노출된다. 경고가 긍정 플래그에 가려지면
        # 투자 도구로서 위험하므로 경고를 앞에 둔다.
        return warn + good

    # --- 실행 ---

    def run(self):
        print("=" * 62)
        print("  🏛️  Quality Stock Screener v5 — 투자하기 좋은 미국 기업")
        print("=" * 62)
        t0 = time.time()

        print("\n📋 유니버스 수집...")
        self.meta = UniverseFetcher.fetch()
        tickers = sorted(self.meta.keys())

        print(f"\n📈 가격 데이터 ({len(tickers)}종목)...")
        self.prices = self.download_prices(tickers)
        print(f"  ✅ 가격 확보: {len(self.prices)}개")

        have_price = [t for t in tickers if t in self.prices]
        print(f"\n📊 재무 데이터 ({len(have_price)}종목)...")
        self.fundamentals = FundamentalsFetcher().fetch_all(have_price)

        print("\n🧮 지표 구성...")
        self.build_raw()
        self.compute_sector_medians()

        print("\n⚖️ 채점...")
        results = []
        for ticker, r in self.raw.items():
            try:
                scored = self.score_one(ticker, r)
                if scored:
                    results.append(scored)
            except Exception as e:
                print(f"  ⚠️ {ticker} 채점 실패: {type(e).__name__}: {e}")

        # 등급 미부여(데이터 부족) 종목은 뒤로
        results.sort(key=lambda x: (x["graded"], x["total_score"]), reverse=True)
        self.results = results

        graded = [r for r in results if r["graded"]]
        self.summary = {
            "total_analyzed": len(results),
            "graded_count": len(graded),
            "universe_size": len(self.meta),
            "a_plus": len([r for r in graded if r["grade"] == "A+"]),
            "a_grade": len([r for r in graded if r["grade"] in ("A+", "A")]),
            "undervalued_quality": len([r for r in graded if "💰 저평가 우량주" in r["flags"]]),
            "all_rounder": len([r for r in graded if "⭐ 올라운더" in r["flags"]]),
            "high_roe": len([r for r in graded if "💎 고ROE" in r["flags"]]),
            "low_debt": len([r for r in graded if "🏦 저부채" in r["flags"]]),
            "dividend": len([r for r in graded if "🎁 고배당" in r["flags"]]),
            "avg_score": round(float(np.mean([r["total_score"] for r in graded])), 1) if graded else 0,
            "sector_count": len([s for s in self.sector_medians if s]),
            "price_count": len(self.prices),
            "fundamentals_count": len(self.fundamentals),
            "scan_sec": round(time.time() - t0),
            "updated_at": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST"),
        }
        print(f"\n⏱️ 완료: {self.summary['scan_sec']}초 "
              f"(채점 {len(results)}개 / 등급부여 {len(graded)}개)")
        return results

    def sector_stats(self):
        buckets = {}
        for r in self.results:
            if not r["graded"] or not r["sector"]:
                continue
            b = buckets.setdefault(r["sector"], [])
            b.append(r["total_score"])
        return [
            {"sector": s, "count": len(v),
             "avg_score": round(float(np.mean(v)), 1),
             "top_count": len([x for x in v if x >= 72])}
            for s, v in sorted(buckets.items(), key=lambda kv: -float(np.mean(kv[1])))
            if len(v) >= 3
        ]

    def save(self, path="data/analysis.js"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        slim_keys = (
            "ticker", "name", "sector", "price", "market_cap", "market_cap_fmt",
            "total_score", "grade", "graded", "coverage", "profitability_score",
            "health_score", "growth_score", "valuation_score", "roe",
            "op_margin", "revenue_growth", "pe", "pbr", "debt_to_equity",
            "dividend_yield", "return_1m", "return_3m", "return_1y",
            "sparkline", "flags",
        )
        stocks = []
        for i, r in enumerate(self.results):
            if i < FULL_DETAIL_TOP:
                stocks.append(r)
            else:
                stocks.append({k: r.get(k) for k in slim_keys})

        payload = {
            "version": "5.0",
            "focus": "us-quality-fundamentals",
            "summary": self.summary,
            "sector_stats": self.sector_stats(),
            "sector_medians": {k: v for k, v in self.sector_medians.items() if k},
            "stocks": stocks,
        }

        with open(path, "w", encoding="utf-8") as f:
            f.write("var STOCK_DATA = ")
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.write(";\n")
        print(f"💾 저장: {path} ({os.path.getsize(path) / 1024 / 1024:.1f}MB)")


def main():
    screener = QualityScreener()
    results = screener.run()
    if not results:
        print("❌ 결과 없음")
        raise SystemExit(1)
    screener.save()

    graded = [r for r in results if r["graded"]]
    print("\n" + "=" * 62)
    print("  🏆 종합 TOP 20 — 투자 매력도 상위")
    print("=" * 62)
    for i, r in enumerate(graded[:20], 1):
        print(f"  {i:2d}. [{r['grade']:>2s}] {r['name'][:34]:<34s} {r['ticker']:<6s} "
              f"{r['total_score']:5.1f} | {r['market_cap_fmt']:>8s} | "
              f"수익{r['profitability_score'] or 0:4.0f} 건전{r['health_score'] or 0:4.0f} "
              f"성장{r['growth_score'] or 0:4.0f} 밸류{r['valuation_score'] or 0:4.0f}")
        if r["flags"]:
            print(f"      {' '.join(r['flags'][:4])}")

    under = [r for r in graded if "💰 저평가 우량주" in r["flags"]]
    if under:
        print("\n" + "=" * 62)
        print("  💰 저평가 우량주 TOP 10 — 품질 높고 섹터 대비 싼 기업")
        print("=" * 62)
        for i, r in enumerate(under[:10], 1):
            pe = f"PER {r['pe']}" if r["pe"] else "PER N/A"
            print(f"  {i:2d}. [{r['grade']:>2s}] {r['name'][:32]:<32s} {r['ticker']:<6s} "
                  f"{r['total_score']:5.1f} | {pe:<10s} | ROE {r['roe'] or 0:5.1f}%")

    print("\n⚠️ 투자 참고자료입니다. 투자 판단과 책임은 본인에게 있습니다.")


if __name__ == "__main__":
    main()
