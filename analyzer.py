"""
급등 예측기 v4 - Low Cap US Stock Surge Detector
핵심: 시총이 낮고 변동성이 높은 미국 소형주에서 급등 직전 종목을 찾는다

[분석 카테고리]
1. 변동성 프로파일 (15%) — ATR%, 역사적변동성, 유동비율, 상대거래량, 캔들범위
2. 매집 감지 (35%) — OBV 다이버전스, Chaikin MF, 거래량 건조→급증, A/D Line
3. 차트 패턴 (30%) — 볼린저스퀴즈, 저항선접근, 삼각수렴, 컵앤핸들, 이평선밀집,
                       저점상승, 베이스돌파, 포켓피봇, 갭분석, VWAP회복, 상대강도
4. 기술 모멘텀 (20%) — RSI, MACD, 모멘텀
5. 보너스 — 주봉추세 정렬, 숏스퀴즈, 섹터 상대강도

[v4 신규]
- 주봉 멀티타임프레임 분석 & 보너스
- 시그널 지속성 추적 (신규 vs N일 연속)
- 과거 시그널 적중률 자동 계산
- 숏 이자율 & 숏스퀴즈 감지
- 실적 발표 근접 경고
- 섹터 상대강도 보너스
- 미니차트 스파크라인 데이터
- OBV 벡터화 성능 최적화
- Nasdaq API 재시도 로직
- 데이터 사이즈 최적화 (상위 500 상세, 나머지 요약)
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import html
import requests
import warnings
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

# ====== 설정 ======
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

MAX_MARKET_CAP = 2_000_000_000  # $2B
MIN_VOLUME = 100_000
BATCH_SIZE = 500
MAX_WORKERS = 5


def format_market_cap(mcap):
    if not mcap:
        return "N/A"
    if mcap >= 1_000_000_000:
        return f"${mcap / 1_000_000_000:.1f}B"
    elif mcap >= 1_000_000:
        return f"${mcap / 1_000_000:.0f}M"
    return f"${mcap:,.0f}"


# ====== 종목 수집 ======

class UniverseFetcher:
    """미국 소형주 유니버스 수집"""

    EXTRA_TICKERS = [
        "PL", "RDW", "RKLB", "LUNR", "ASTS", "MNTS", "BKSY", "SATL", "SPCE",
        "SMCI", "SOUN", "BBAI", "IREN", "CLSK", "APLD",
        "SMR", "NNE", "OKLO",
        "IONQ", "RGTI", "QUBT",
        "CRSP", "NTLA", "BEAM", "EDIT",
        "SOFI", "AFRM", "UPST", "NU",
        "HIMS", "DUOL", "CAVA", "TOST",
    ]

    BENCHMARK_TICKERS = ["SPY", "QQQ", "IWM"]

    @staticmethod
    def _parse_market_cap(s):
        if not s or s == "N/A" or s == "":
            return None
        s = s.replace("$", "").replace(",", "").strip()
        try:
            if s.endswith("B"):
                return float(s[:-1]) * 1_000_000_000
            elif s.endswith("M"):
                return float(s[:-1]) * 1_000_000
            elif s.endswith("T"):
                return float(s[:-1]) * 1_000_000_000_000
            else:
                return float(s)
        except ValueError:
            return None

    @staticmethod
    def fetch_nasdaq_screener(max_market_cap=MAX_MARKET_CAP):
        """Nasdaq Screener API로 소형주 수집 — 재시도 로직 + 섹터/업종 포함"""
        metadata = {}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        offset = 0
        limit = 200
        total = None
        max_retries = 3

        while True:
            url = (
                f"https://api.nasdaq.com/api/screener/stocks"
                f"?tableonly=true&limit={limit}&offset={offset}"
            )
            success = False
            for attempt in range(max_retries):
                try:
                    resp = requests.get(url, headers=headers, timeout=30)
                    data = resp.json()
                    rows = data["data"]["table"]["rows"]
                    if total is None:
                        total = int(data["data"]["totalrecords"])
                        print(f"  📡 Nasdaq screener: {total}개 상장 종목")

                    for row in rows:
                        symbol = row.get("symbol", "").strip()
                        name = row.get("name", symbol).strip()
                        mcap_str = row.get("marketCap", "")
                        mcap = UniverseFetcher._parse_market_cap(mcap_str)
                        sector = row.get("sector", "").strip()
                        industry = row.get("industry", "").strip()

                        if (symbol
                            and mcap is not None
                            and 0 < mcap <= max_market_cap
                            and not any(c in symbol for c in ['^', '/', '.'])
                        ):
                            metadata[symbol] = {
                                "name": name,
                                "market_cap": mcap,
                                "sector": sector,
                                "industry": industry,
                            }

                    success = True
                    break

                except Exception as e:
                    if attempt < max_retries - 1:
                        wait = 2 ** attempt
                        print(f"  ⚠️ Nasdaq API 재시도 {attempt+1}/{max_retries} ({wait}초 후): {e}")
                        time.sleep(wait)
                    else:
                        print(f"  ⚠️ Nasdaq API 오류 (offset {offset}): {e}")

            if not success and total is None:
                break

            offset += limit
            if total is not None and offset >= total:
                break
            time.sleep(0.3)

        print(f"  ✅ 필터링 완료: {len(metadata)}개 (시총 < ${max_market_cap/1e9:.0f}B)")
        return metadata

    @staticmethod
    def _fallback_wikipedia():
        """폴백: Wikipedia에서 S&P500 + NASDAQ100"""
        tickers = set()
        try:
            tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
            sp500 = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
            tickers.update(sp500)
            print(f"  ✅ S&P 500: {len(sp500)}종목")
        except Exception as e:
            print(f"  ⚠️ S&P 500 로드 실패: {e}")
        try:
            tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
            for t in tables:
                if "Ticker" in t.columns:
                    tickers.update(t["Ticker"].tolist())
                    break
        except Exception as e:
            print(f"  ⚠️ NASDAQ 100 로드 실패: {e}")
        return list(tickers)

    @staticmethod
    def get_universe():
        """메인 진입점: Nasdaq API → 폴백 → 추가종목. (tickers, metadata) 반환"""
        metadata = {}

        api_meta = UniverseFetcher.fetch_nasdaq_screener()
        if len(api_meta) > 100:
            metadata.update(api_meta)
        else:
            print("  ⚠️ Nasdaq API 실패, Wikipedia 폴백 사용")
            fb_tickers = UniverseFetcher._fallback_wikipedia()
            for t in fb_tickers:
                metadata[t] = {"name": t, "market_cap": None, "sector": "", "industry": ""}

        for t in UniverseFetcher.EXTRA_TICKERS:
            if t not in metadata:
                metadata[t] = {"name": t, "market_cap": None, "sector": "", "industry": ""}

        tickers = sorted(metadata.keys())
        print(f"  📊 총 유니버스: {len(tickers)}종목")
        return tickers, metadata


# ====== 변동성 분석 ======

class VolatilityAnalyzer:
    """변동성 프로파일 — 급등 가능성이 높은 특성 측정"""

    @staticmethod
    def atr_percent(high, low, close, period=14):
        """ATR%: 일일 변동성 크기"""
        if len(close) < period + 1:
            return 0, "데이터 부족"
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        atr_pct = (atr / close * 100).iloc[-1]
        if pd.isna(atr_pct):
            return 50, "N/A"
        if atr_pct > 8:    return 95, f"ATR:{atr_pct:.1f}% 극한변동"
        elif atr_pct > 5:  return 80, f"ATR:{atr_pct:.1f}% 고변동"
        elif atr_pct > 3:  return 65, f"ATR:{atr_pct:.1f}% 중간"
        elif atr_pct > 1.5: return 40, f"ATR:{atr_pct:.1f}% 저변동"
        return 20, f"ATR:{atr_pct:.1f}% 매우낮음"

    @staticmethod
    def historical_volatility(close, period=20):
        """역사적 변동성: 연환산 로그수익률 표준편차"""
        if len(close) < period + 1:
            return 0, "데이터 부족"
        log_ret = np.log(close / close.shift(1)).dropna()
        if len(log_ret) < period:
            return 50, "N/A"
        hvol = log_ret.iloc[-period:].std() * np.sqrt(252) * 100
        if hvol > 100:   return 95, f"HV:{hvol:.0f}% 극한"
        elif hvol > 70:  return 80, f"HV:{hvol:.0f}% 매우높음"
        elif hvol > 45:  return 65, f"HV:{hvol:.0f}% 높음"
        elif hvol > 25:  return 45, f"HV:{hvol:.0f}% 보통"
        return 20, f"HV:{hvol:.0f}% 낮음"

    @staticmethod
    def float_ratio(info):
        """유동비율: 낮을수록 급등 시 폭발적"""
        float_shares = info.get("floatShares")
        shares_out = info.get("sharesOutstanding")
        if not float_shares or not shares_out or shares_out == 0:
            return 50, "N/A", None
        ratio = float_shares / shares_out
        if ratio < 0.3:    return 95, f"Float:{ratio:.0%} 극소", ratio
        elif ratio < 0.5:  return 75, f"Float:{ratio:.0%} 낮음", ratio
        elif ratio < 0.7:  return 55, f"Float:{ratio:.0%} 보통", ratio
        elif ratio < 0.85: return 35, f"Float:{ratio:.0%} 높음", ratio
        return 20, f"Float:{ratio:.0%} 매우높음", ratio

    @staticmethod
    def relative_volume(volume, info):
        """상대거래량: 최근 거래량 / 평균 거래량"""
        if len(volume) < 20:
            return 0, "데이터 부족"
        recent_avg = volume.iloc[-3:].mean()
        avg_20d = volume.iloc[-20:].mean()
        if avg_20d == 0:
            return 50, "N/A"
        rvol = recent_avg / avg_20d
        avg_vol = info.get("averageVolume")
        if avg_vol and avg_vol > 0:
            rvol = max(rvol, recent_avg / avg_vol)
        if rvol > 3:     return 95, f"RVol:{rvol:.1f}x 폭증"
        elif rvol > 2:   return 80, f"RVol:{rvol:.1f}x 급증"
        elif rvol > 1.5: return 65, f"RVol:{rvol:.1f}x 증가"
        elif rvol > 1:   return 45, f"RVol:{rvol:.1f}x 보통"
        return 25, f"RVol:{rvol:.1f}x 감소"

    @staticmethod
    def candle_range(high, low, close, period=10):
        """캔들 범위: 평균 캔들 크기 (고가-저가)/종가"""
        if len(close) < period:
            return 0, "데이터 부족"
        body_pct = ((high - low) / close * 100).iloc[-period:].mean()
        if pd.isna(body_pct):
            return 50, "N/A"
        if body_pct > 8:    return 90, f"Range:{body_pct:.1f}% 넓음"
        elif body_pct > 5:  return 70, f"Range:{body_pct:.1f}% 큰편"
        elif body_pct > 3:  return 50, f"Range:{body_pct:.1f}% 보통"
        return 25, f"Range:{body_pct:.1f}% 좁음"


# ====== 매집 감지 ======

class AccumulationDetector:
    """매집 감지 엔진"""

    @staticmethod
    def calc_obv(close, volume):
        """벡터화된 OBV 계산 (기존 루프 대비 10x+ 빠름)"""
        direction = np.sign(close.diff()).fillna(0)
        return (direction * volume).cumsum()

    @staticmethod
    def obv_divergence(close, volume):
        """OBV 다이버전스: 가격 횡보/하락 + OBV 상승 = 매집"""
        if len(close) < 30:
            return 0, "데이터 부족"
        obv = AccumulationDetector.calc_obv(close, volume)
        price_slope = np.polyfit(range(20), close.iloc[-20:].values, 1)[0]
        obv_slope = np.polyfit(range(20), obv.iloc[-20:].values, 1)[0]
        price_chg = price_slope / close.iloc[-20] * 100
        obv_norm = obv_slope / (abs(obv.iloc[-20:]).mean() + 1)

        if price_chg < 0.5 and obv_norm > 0:
            strength = min(100, abs(obv_norm) * 500 + abs(price_chg) * 10)
            if price_chg < -1:
                strength = min(100, strength * 1.3)
            return min(100, strength), f"가격{price_chg:+.1f}% OBV↑"
        elif price_chg > 1 and obv_norm > 0:
            return 40, "동반 상승"
        return 15, "매집 미감지"

    @staticmethod
    def chaikin_mf(high, low, close, volume, period=20):
        """Chaikin Money Flow: 자금 유입 강도"""
        if len(close) < period:
            return 0, "N/A"
        mfm = ((close - low) - (high - close)) / (high - low + 1e-10)
        mfv = mfm * volume
        cmf = mfv.rolling(period).sum() / volume.rolling(period).sum()
        val = cmf.iloc[-1]
        if pd.isna(val):
            return 50, "N/A"
        if val > 0.15:   return 95, f"CMF:{val:.2f} 강한매집"
        elif val > 0.05: return 75, f"CMF:{val:.2f} 매집"
        elif val > -0.05: return 50, f"CMF:{val:.2f} 중립"
        elif val > -0.15: return 25, f"CMF:{val:.2f} 매도압력"
        return 10, f"CMF:{val:.2f} 강한매도"

    @staticmethod
    def volume_dryup_spike(volume, period=20):
        """거래량 건조 후 급증 = 세력 진입"""
        if len(volume) < period + 5:
            return 0, "데이터 부족"
        dry_avg = volume.iloc[-(period+5):-5].mean()
        recent_avg = volume.iloc[-3:].mean()
        if dry_avg == 0:
            return 0, "N/A"
        full_avg = volume.iloc[-period:].mean()
        dryness = dry_avg / full_avg if full_avg > 0 else 1
        spike = recent_avg / dry_avg

        if dryness < 0.7 and spike > 2:
            return min(100, 60 + spike * 10), f"건조→급증 {spike:.1f}x"
        elif spike > 1.5:
            return min(85, 50 + spike * 8), f"거래량↑ {spike:.1f}x"
        return 20, f"평이 {spike:.1f}x"

    @staticmethod
    def ad_line(high, low, close, volume):
        """A/D Line: 종가 위치 기반 매집/분산"""
        if len(close) < 20:
            return 0, "데이터 부족"
        clv = ((close - low) - (high - close)) / (high - low + 1e-10)
        ad = (clv * volume).cumsum()
        ad_slope = np.polyfit(range(20), ad.iloc[-20:].values, 1)[0]
        price_slope = np.polyfit(range(20), close.iloc[-20:].values, 1)[0]
        ad_trend = ad_slope / (abs(ad.iloc[-20:]).mean() + 1)
        price_trend = price_slope / close.iloc[-20] * 100

        if ad_trend > 0 and price_trend < 0.5:
            return min(90, 60 + abs(ad_trend) * 200), "A/D↑ 가격→ 매집"
        elif ad_trend > 0:
            return 55, "A/D↑ 동반상승"
        return 20, "A/D↓ 분산"


# ====== 차트 패턴 ======

class PatternDetector:
    """돌파 직전 패턴 감지"""

    @staticmethod
    def bollinger_squeeze(close, period=20):
        """볼린저밴드 스퀴즈: 밴드 극도로 좁아짐 → 폭발 직전"""
        if len(close) < period + 10:
            return 0, "데이터 부족"
        sma = close.rolling(period).mean()
        std = close.rolling(period).std()
        bw = (std / sma * 100).dropna()
        if len(bw) < 10:
            return 0, "데이터 부족"
        curr = bw.iloc[-1]
        avg = bw.iloc[-60:].mean() if len(bw) >= 60 else bw.mean()
        ratio = curr / avg if avg > 0 else 1

        if ratio < 0.4:   return 95, f"극한스퀴즈 {ratio:.0%}"
        elif ratio < 0.6: return 80, f"강한스퀴즈 {ratio:.0%}"
        elif ratio < 0.8: return 60, f"스퀴즈진행 {ratio:.0%}"
        return 30, f"일반 {ratio:.0%}"

    @staticmethod
    def resistance_approach(high, close):
        """저항선 접근: 여러번 맞고 내려온 가격대에 재접근"""
        if len(close) < 60:
            return 0, "데이터 부족"
        current = close.iloc[-1]
        peak = high.iloc[-60:].max()
        dist = (peak - current) / current * 100
        zone = peak * 0.98
        touches = (high.iloc[-60:] >= zone).sum()

        if dist < 2 and touches >= 2:
            return 90, f"저항선 {dist:.1f}% (터치{touches})"
        elif dist < 3 and touches >= 2:
            return 75, f"접근 {dist:.1f}%"
        elif dist < 5:
            return 55, f"근처 {dist:.1f}%"
        return 25, f"먼거리 {dist:.1f}%"

    @staticmethod
    def triangle_convergence(high, low, close):
        """삼각수렴: 고점↓ + 저점↑ = 에너지 축적"""
        if len(close) < 30:
            return 0, "데이터 부족"
        seg = [(-30, -20), (-20, -10), (-10, None)]
        highs = [high.iloc[s:e].max() for s, e in seg]
        lows = [low.iloc[s:e].min() for s, e in seg]
        h_fall = highs[2] < highs[1] < highs[0]
        l_rise = lows[2] > lows[1] > lows[0]
        r1, r3 = highs[0] - lows[0], highs[2] - lows[2]
        conv = r3 / r1 if r1 > 0 else 1

        if h_fall and l_rise and conv < 0.6:
            return 90, f"삼각수렴 범위{conv:.0%}"
        elif (h_fall or l_rise) and conv < 0.7:
            return 65, f"부분수렴 {conv:.0%}"
        return 20, "수렴 없음"

    @staticmethod
    def cup_and_handle(close, volume):
        """컵앤핸들: U자 바닥 후 소폭 눌림"""
        if len(close) < 40:
            return 0, "데이터 부족"
        r40 = close.iloc[-40:]
        min_pos = r40.values.argmin()
        if not (10 < min_pos < 30):
            return 20, "컵 미형성"
        bottom = r40.iloc[min_pos]
        left = r40.iloc[:5].mean()
        right = r40.iloc[-5:].mean()
        rim_diff = abs(left - right) / left * 100
        depth = (left - bottom) / left * 100
        handle_val = (r40.iloc[-10:].max() - close.iloc[-1]) / r40.iloc[-10:].max() * 100

        if rim_diff < 5 and 5 < depth < 30 and 0 < handle_val < 8:
            return 85, f"컵앤핸들 깊이{depth:.0f}%"
        elif rim_diff < 8 and depth > 3:
            return 50, "컵 형성 중"
        return 15, "패턴 없음"

    @staticmethod
    def ma_tightening(close):
        """이평선 밀집: 5/10/20/50선 모임 → 방향성 폭발"""
        if len(close) < 50:
            return 0, "데이터 부족"
        mas = [close.iloc[-n:].mean() for n in [5, 10, 20, 50]]
        curr = close.iloc[-1]
        spread = (max(mas) - min(mas)) / curr * 100
        above = sum(1 for m in mas if curr > m)

        if spread < 2:   score, label = 90, f"극한밀집 {spread:.1f}%"
        elif spread < 4: score, label = 70, f"밀집 {spread:.1f}%"
        elif spread < 6: score, label = 50, f"보통 {spread:.1f}%"
        else:            score, label = 20, f"분산 {spread:.1f}%"
        if above == 4:
            score = min(100, score + 10)
            label += " 정배열"
        return score, label

    @staticmethod
    def higher_lows(low):
        """연속 저점 상승: 우상향 기반 확인"""
        if len(low) < 20:
            return 0, "데이터 부족"
        lows = [low.iloc[s:s+5].min() for s in range(-20, 0, 5)]
        rising = sum(1 for i in range(len(lows)-1) if lows[i] < lows[i+1])
        if rising >= 3:
            return 85, f"저점 연속↑ {rising+1}구간"
        elif rising >= 2:
            return 60, f"저점 상승 {rising}구간"
        return 25, "저점 미약"

    # ====== 신규 패턴 ======

    @staticmethod
    def base_breakout(close, volume, period=20):
        """베이스 돌파: 좁은 횡보 구간 돌파 + 거래량 확대"""
        if len(close) < period + 5:
            return 0, "데이터 부족"
        base = close.iloc[-(period+5):-5]
        base_range = (base.max() - base.min()) / base.mean() * 100
        current = close.iloc[-1]
        base_high = base.max()
        breakout_pct = (current - base_high) / base_high * 100

        base_vol = volume.iloc[-(period+5):-5].mean()
        recent_vol = volume.iloc[-3:].mean()
        vol_expansion = recent_vol / base_vol if base_vol > 0 else 1

        if base_range < 8 and breakout_pct > 0 and vol_expansion > 1.5:
            return min(100, int(80 + vol_expansion * 5)), f"돌파! 범위:{base_range:.0f}% vol:{vol_expansion:.1f}x"
        elif base_range < 10 and breakout_pct > -1:
            return 65, f"돌파 근접 범위:{base_range:.0f}%"
        elif base_range < 12:
            return 45, f"횡보 중 범위:{base_range:.0f}%"
        return 20, f"베이스 없음 {base_range:.0f}%"

    @staticmethod
    def pocket_pivot(close, volume):
        """포켓 피봇: 상승일 거래량 > 10일간 최대 하락일 거래량"""
        if len(close) < 12:
            return 0, "데이터 부족"
        today_up = close.iloc[-1] > close.iloc[-2]
        today_vol = volume.iloc[-1]

        down_vols = []
        for i in range(-11, -1):
            if len(close) > abs(i) and close.iloc[i] < close.iloc[i-1]:
                down_vols.append(volume.iloc[i])

        if not down_vols:
            return 50, "하락일 없음"

        max_down_vol = max(down_vols)

        if today_up and today_vol > max_down_vol:
            ratio = today_vol / max_down_vol
            if len(close) >= 50:
                sma50 = close.iloc[-50:].mean()
                near_ma = abs(close.iloc[-1] - sma50) / sma50 < 0.05
                if near_ma:
                    return min(100, int(80 + ratio * 5)), f"포켓피봇+MA50 {ratio:.1f}x"
            return min(90, int(65 + ratio * 5)), f"포켓피봇 {ratio:.1f}x"
        elif today_up and today_vol > max_down_vol * 0.8:
            return 55, "포켓피봇 근접"
        return 20, "포켓피봇 없음"

    @staticmethod
    def gap_analysis(open_price, close, high, low):
        """갭 분석: 미충전 갭업 패턴 (강세)"""
        if len(close) < 5:
            return 0, "데이터 부족"
        gaps = []
        for i in range(-5, 0):
            if len(close) > abs(i):
                gap_pct = (open_price.iloc[i] - close.iloc[i-1]) / close.iloc[i-1] * 100
                if gap_pct > 1:
                    filled = low.iloc[i:].min() < close.iloc[i-1]
                    gaps.append({"day": i, "pct": gap_pct, "filled": filled})

        unfilled_gaps = [g for g in gaps if not g["filled"]]

        if unfilled_gaps:
            biggest = max(unfilled_gaps, key=lambda g: g["pct"])
            return min(90, int(65 + biggest["pct"] * 5)), f"미충전갭 +{biggest['pct']:.1f}%"
        elif gaps:
            return 45, f"갭 충전됨 ({len(gaps)}개)"
        return 20, "갭 없음"

    @staticmethod
    def vwap_reclaim(high, low, close, volume):
        """VWAP 회복: VWAP 아래→위 회복"""
        if len(close) < 20:
            return 0, "데이터 부족"
        period = min(20, len(close))
        typical = (high.iloc[-period:] + low.iloc[-period:] + close.iloc[-period:]) / 3
        cum_tv = (typical * volume.iloc[-period:]).cumsum()
        cum_v = volume.iloc[-period:].cumsum()
        vwap = cum_tv / cum_v

        current_price = close.iloc[-1]
        current_vwap = vwap.iloc[-1]
        yesterday_price = close.iloc[-2]
        yesterday_vwap = vwap.iloc[-2] if len(vwap) >= 2 else current_vwap

        if yesterday_price < yesterday_vwap and current_price > current_vwap:
            pct_above = (current_price - current_vwap) / current_vwap * 100
            return min(90, int(75 + pct_above * 5)), f"VWAP 회복 +{pct_above:.1f}%"
        elif current_price > current_vwap:
            pct_above = (current_price - current_vwap) / current_vwap * 100
            return 55, f"VWAP 위 +{pct_above:.1f}%"
        else:
            pct_below = (current_vwap - current_price) / current_vwap * 100
            return 25, f"VWAP 아래 -{pct_below:.1f}%"

    @staticmethod
    def relative_strength_vs_spy(close, spy_close):
        """상대강도: SPY 대비 5/10/20일 초과수익"""
        if len(close) < 20 or spy_close is None or len(spy_close) < 20:
            return 0, "데이터 부족"

        outperform = 0
        details = []
        for days in [5, 10, 20]:
            if len(close) >= days and len(spy_close) >= days:
                stock_ret = (close.iloc[-1] / close.iloc[-days] - 1) * 100
                spy_ret = (spy_close.iloc[-1] / spy_close.iloc[-days] - 1) * 100
                alpha = stock_ret - spy_ret
                if alpha > 0:
                    outperform += 1
                details.append(f"{days}d:{alpha:+.1f}%")

        if outperform == 3:   return 90, f"RS+++ {' '.join(details)}"
        elif outperform == 2: return 70, f"RS++ {' '.join(details)}"
        elif outperform == 1: return 45, f"RS+ {' '.join(details)}"
        return 20, f"RS약 {' '.join(details)}"


# ====== 멀티 타임프레임 ======

class MultiTimeframeAnalyzer:
    """주봉 추세 분석 — 일봉 데이터를 주봉으로 리샘플링"""

    @staticmethod
    def weekly_trend(close, volume):
        """주봉 추세: 10주 MA, 4주 변화율, 주봉 거래량"""
        if len(close) < 50:
            return 0, "데이터 부족"

        try:
            wc = close.resample('W').last().dropna()
            wv = volume.resample('W').sum().dropna()
        except Exception:
            return 50, "리샘플링 실패"

        if len(wc) < 10:
            return 50, "주봉 부족"

        ma10 = wc.rolling(10).mean()
        above_ma = False
        if not pd.isna(ma10.iloc[-1]):
            above_ma = wc.iloc[-1] > ma10.iloc[-1]

        w4_ret = 0
        if len(wc) >= 4:
            w4_ret = (wc.iloc[-1] / wc.iloc[-4] - 1) * 100

        wv_ratio = 1
        if len(wv) >= 8:
            recent_wv = wv.iloc[-2:].mean()
            avg_wv = wv.iloc[-8:].mean()
            if avg_wv > 0:
                wv_ratio = recent_wv / avg_wv

        score = 50
        parts = []

        if above_ma:
            score += 15
            parts.append("10주MA↑")
        else:
            score -= 10
            parts.append("10주MA↓")

        if 0 < w4_ret < 10:
            score += 15
            parts.append(f"4주+{w4_ret:.1f}%")
        elif w4_ret >= 10:
            score += 5
            parts.append(f"4주+{w4_ret:.1f}%급등")
        elif -5 < w4_ret < 0:
            score += 5
            parts.append(f"4주{w4_ret:.1f}%눌림")
        else:
            score -= 10
            parts.append(f"4주{w4_ret:.1f}%")

        if wv_ratio > 1.3:
            score += 10
            parts.append(f"주Vol:{wv_ratio:.1f}x")

        return min(100, max(0, score)), " ".join(parts)


# ====== 시그널 추적 ======

class SignalTracker:
    """시그널 지속성 추적 + 과거 적중률 계산"""

    def __init__(self, path="data/history.json"):
        self.path = path
        self.history = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"snapshots": []}

    def get_persistence(self, ticker):
        """이 종목이 최근 며칠 연속 관심(55+) 이상이었는지"""
        days = 0
        for snap in reversed(self.history["snapshots"]):
            if ticker in snap.get("stocks", {}):
                days += 1
            else:
                break
        return days

    def get_new_signals(self, current_results, threshold=55):
        """이전 스냅샷에 없던 새로운 시그널 종목"""
        if not self.history["snapshots"]:
            return {r["ticker"] for r in current_results if r["total_score"] >= threshold}

        prev_tickers = set(self.history["snapshots"][-1].get("stocks", {}).keys())
        return {
            r["ticker"] for r in current_results
            if r["total_score"] >= threshold and r["ticker"] not in prev_tickers
        }

    def compute_hit_rates(self):
        """과거 '급등 임박' 시그널의 실제 성과 계산"""
        snapshots = self.history["snapshots"]
        if len(snapshots) < 4:
            return None

        periods = {"7d": (5, 9), "14d": (12, 16), "30d": (27, 33)}
        results = {}

        for period_name, (min_days, max_days) in periods.items():
            hits_10 = 0
            hits_5 = 0
            positive = 0
            total = 0
            total_return = 0

            for i, snap in enumerate(snapshots):
                try:
                    snap_date = datetime.strptime(snap["date"], "%Y-%m-%d")
                except (ValueError, KeyError):
                    continue

                for future_snap in snapshots[i+1:]:
                    try:
                        future_date = datetime.strptime(future_snap["date"], "%Y-%m-%d")
                    except (ValueError, KeyError):
                        continue
                    diff = (future_date - snap_date).days

                    if diff < min_days:
                        continue
                    if diff > max_days:
                        break

                    for ticker, data in snap["stocks"].items():
                        if data.get("score", 0) >= 78:
                            future_data = future_snap["stocks"].get(ticker)
                            if future_data and data.get("price", 0) > 0:
                                ret = (future_data["price"] / data["price"] - 1) * 100
                                total += 1
                                total_return += ret
                                if ret >= 10:
                                    hits_10 += 1
                                if ret >= 5:
                                    hits_5 += 1
                                if ret > 0:
                                    positive += 1
                    break
            if total > 0:
                results[period_name] = {
                    "total": total,
                    "hit_10pct": round(hits_10 / total * 100, 1),
                    "hit_5pct": round(hits_5 / total * 100, 1),
                    "positive_pct": round(positive / total * 100, 1),
                    "avg_return": round(total_return / total, 2),
                }

        return results if results else None

    def save_snapshot(self, results):
        """현재 분석 결과 스냅샷 저장 (대기 이상만)"""
        today = datetime.now().strftime("%Y-%m-%d")

        stocks = {}
        for r in results:
            if r["total_score"] >= 40:
                stocks[r["ticker"]] = {
                    "score": r["total_score"],
                    "signal": r["signal"],
                    "price": r["price"],
                }

        self.history["snapshots"] = [
            s for s in self.history["snapshots"] if s.get("date") != today
        ]
        self.history["snapshots"].append({"date": today, "stocks": stocks})
        self.history["snapshots"] = self.history["snapshots"][-90:]

        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False)
        print(f"  💾 시그널 히스토리 저장 ({len(stocks)}종목, 총 {len(self.history['snapshots'])}일)")


# ====== 메인 엔진 ======

class PreSurgePredictor:
    """급등 예측기 v4 메인"""

    def __init__(self):
        self.results = []
        self.market_summary = {}
        self.spy_close = None
        self.metadata = {}
        self.signal_tracker = SignalTracker()

    def analyze_stock(self, ticker, hist):
        """개별 종목 분석 — hist는 yf.download()로 받은 DataFrame"""
        if hist is None or hist.empty or len(hist) < 30:
            return None

        meta = self.metadata.get(ticker, {})
        c, h, l, v = hist["Close"], hist["High"], hist["Low"], hist["Volume"]
        o = hist["Open"]

        if c.dropna().empty or len(c.dropna()) < 30:
            return None

        info = meta.get("_info", {})

        # ===== 변동성 프로파일 (15%) =====
        atr_s, atr_d = VolatilityAnalyzer.atr_percent(h, l, c)
        hv_s, hv_d = VolatilityAnalyzer.historical_volatility(c)
        fr_result = VolatilityAnalyzer.float_ratio(info)
        fr_s, fr_d, fr_val = fr_result
        rv_s, rv_d = VolatilityAnalyzer.relative_volume(v, info)
        cr_s, cr_d = VolatilityAnalyzer.candle_range(h, l, c)

        vol_items = [
            {"name": "ATR%",       "score": atr_s, "value": atr_d, "w": 25},
            {"name": "역사적변동성", "score": hv_s,  "value": hv_d,  "w": 25},
            {"name": "유동비율",    "score": fr_s,  "value": fr_d,  "w": 20},
            {"name": "상대거래량",  "score": rv_s,  "value": rv_d,  "w": 15},
            {"name": "캔들범위",    "score": cr_s,  "value": cr_d,  "w": 15},
        ]

        # ===== 매집 감지 (35%) =====
        obv_s, obv_d = AccumulationDetector.obv_divergence(c, v)
        cmf_s, cmf_d = AccumulationDetector.chaikin_mf(h, l, c, v)
        vds_s, vds_d = AccumulationDetector.volume_dryup_spike(v)
        ad_s, ad_d = AccumulationDetector.ad_line(h, l, c, v)

        acc_items = [
            {"name": "OBV 다이버전스", "score": obv_s, "value": obv_d, "w": 30},
            {"name": "Chaikin MF",     "score": cmf_s, "value": cmf_d, "w": 25},
            {"name": "거래량 건조→급증", "score": vds_s, "value": vds_d, "w": 25},
            {"name": "A/D Line",       "score": ad_s,  "value": ad_d,  "w": 20},
        ]

        # ===== 차트 패턴 (30%) =====
        sq_s, sq_d = PatternDetector.bollinger_squeeze(c)
        rs2_s, rs2_d = PatternDetector.resistance_approach(h, c)
        tr_s, tr_d = PatternDetector.triangle_convergence(h, l, c)
        ch_s, ch_d = PatternDetector.cup_and_handle(c, v)
        ma_s, ma_d = PatternDetector.ma_tightening(c)
        hl_s, hl_d = PatternDetector.higher_lows(l)
        bb_s, bb_d = PatternDetector.base_breakout(c, v)
        pp_s, pp_d = PatternDetector.pocket_pivot(c, v)
        ga_s, ga_d = PatternDetector.gap_analysis(o, c, h, l)
        vw_s, vw_d = PatternDetector.vwap_reclaim(h, l, c, v)
        rspy_s, rspy_d = PatternDetector.relative_strength_vs_spy(c, self.spy_close)

        pat_items = [
            {"name": "볼린저 스퀴즈",  "score": sq_s,   "value": sq_d,   "w": 12},
            {"name": "저항선 접근",     "score": rs2_s,  "value": rs2_d,  "w": 12},
            {"name": "베이스 돌파",     "score": bb_s,   "value": bb_d,   "w": 12},
            {"name": "포켓 피봇",       "score": pp_s,   "value": pp_d,   "w": 10},
            {"name": "삼각수렴",        "score": tr_s,   "value": tr_d,   "w": 8},
            {"name": "컵앤핸들",        "score": ch_s,   "value": ch_d,   "w": 8},
            {"name": "이평선 밀집",     "score": ma_s,   "value": ma_d,   "w": 8},
            {"name": "연속 저점↑",      "score": hl_s,   "value": hl_d,   "w": 8},
            {"name": "갭 분석",         "score": ga_s,   "value": ga_d,   "w": 8},
            {"name": "VWAP 회복",       "score": vw_s,   "value": vw_d,   "w": 7},
            {"name": "상대강도 vs SPY", "score": rspy_s, "value": rspy_d, "w": 7},
        ]

        # ===== 기술 모멘텀 (20%) =====
        rsi_s, rsi_d = self._rsi(c)
        macd_s, macd_d = self._macd(c)
        mom_s, mom_d = self._momentum(c)

        tech_items = [
            {"name": "RSI",    "score": rsi_s,  "value": rsi_d,  "w": 35},
            {"name": "MACD",   "score": macd_s, "value": macd_d, "w": 35},
            {"name": "모멘텀", "score": mom_s,  "value": mom_d,  "w": 30},
        ]

        # ===== 주봉 멀티타임프레임 =====
        weekly_s, weekly_d = MultiTimeframeAnalyzer.weekly_trend(c, v)

        def wavg(items):
            tw = sum(i["w"] for i in items)
            return sum(i["score"] * i["w"] / tw for i in items)

        vol_avg = wavg(vol_items)
        acc_avg = wavg(acc_items)
        pat_avg = wavg(pat_items)
        tech_avg = wavg(tech_items)

        total = vol_avg * 0.15 + acc_avg * 0.35 + pat_avg * 0.30 + tech_avg * 0.20

        # 보너스
        bonus = 0
        if acc_avg >= 70 and sq_s >= 70:   bonus += 12
        if acc_avg >= 65 and bb_s >= 70:   bonus += 10
        if acc_avg >= 65 and rs2_s >= 70:  bonus += 8
        if vol_avg >= 70 and acc_avg >= 65: bonus += 8
        if pp_s >= 70 and rs2_s >= 65:     bonus += 6
        # 주봉 정렬 보너스
        if weekly_s >= 70:                  bonus += 5
        total = min(100, total + bonus)

        # 시그널
        if total >= 78:   sig = "🔴 급등 임박"
        elif total >= 68: sig = "🟠 매집 진행"
        elif total >= 55: sig = "🟡 관심"
        elif total >= 40: sig = "🔵 대기"
        else:             sig = "⚪ 관망"

        name = html.escape(meta.get("name") or info.get("shortName") or ticker)

        r1d = (c.iloc[-1] / c.iloc[-2] - 1) * 100 if len(c) >= 2 else 0
        r5d = (c.iloc[-1] / c.iloc[-5] - 1) * 100 if len(c) >= 5 else 0
        r20d = (c.iloc[-1] / c.iloc[-20] - 1) * 100 if len(c) >= 20 else 0
        vr = float(v[-3:].mean() / v[-20:].mean()) if len(v) >= 20 and v[-20:].mean() > 0 else 1.0

        # ATR% 원시값
        atr_pct_val = None
        if len(c) >= 15:
            tr1 = h - l
            tr2 = (h - c.shift(1)).abs()
            tr3 = (l - c.shift(1)).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr_raw = tr.rolling(14).mean()
            atr_pct_val = round(float((atr_raw / c * 100).iloc[-1]), 2) if not pd.isna((atr_raw / c * 100).iloc[-1]) else None

        flags = []
        if acc_avg >= 70 and sq_s >= 70:    flags.append("💥 매집+스퀴즈")
        if acc_avg >= 65 and bb_s >= 70:    flags.append("🚀 매집+돌파")
        if obv_s >= 75:                     flags.append("🕵️ OBV 매집")
        if sq_s >= 80:                      flags.append("🔥 변동성 폭발 임박")
        if rs2_s >= 75:                     flags.append("🚪 저항선 돌파 임박")
        if vds_s >= 75:                     flags.append("⚡ 거래량 급증")
        if acc_avg >= 70:                   flags.append("📦 강한 매집")
        if pp_s >= 70:                      flags.append("💎 포켓 피봇")
        if bb_s >= 70:                      flags.append("📊 베이스 돌파")
        if vol_avg >= 75:                   flags.append("🌋 고변동성")
        if fr_s >= 80:                      flags.append("🎯 Low Float")
        if weekly_s >= 75:                  flags.append("📈 주봉 정렬")

        mcap = meta.get("market_cap") or info.get("marketCap")

        # 스파크라인 데이터 (최근 30일, 0~100 정규화)
        spark_len = min(30, len(c))
        spark_raw = c.iloc[-spark_len:].values
        sp_min, sp_max = float(spark_raw.min()), float(spark_raw.max())
        sp_range = sp_max - sp_min if sp_max != sp_min else 1
        sparkline = [round((float(p) - sp_min) / sp_range * 100) for p in spark_raw]

        return {
            "ticker": ticker,
            "name": name,
            "market": "US",
            "price": round(float(c.iloc[-1]), 2),
            "signal": sig,
            "total_score": round(total, 1),
            "volatility_score": round(vol_avg, 1),
            "accum_score": round(acc_avg, 1),
            "pattern_score": round(pat_avg, 1),
            "tech_score": round(tech_avg, 1),
            "weekly_score": round(weekly_s, 1),
            "return_1d": round(r1d, 2),
            "return_5d": round(r5d, 2),
            "return_20d": round(r20d, 2),
            "volume_ratio": round(vr, 2),
            "market_cap": mcap,
            "market_cap_fmt": format_market_cap(mcap),
            "float_ratio": round(fr_val, 2) if fr_val is not None else None,
            "atr_pct": atr_pct_val,
            "sparkline": sparkline,
            "details": {
                "volatility": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in vol_items],
                "accumulation": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in acc_items],
                "pattern": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in pat_items],
                "technical": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in tech_items],
                "weekly": [{"name": "주봉 추세", "score": round(weekly_s, 1), "value": weekly_d}],
            },
            "sector": html.escape(meta.get("sector", "") or info.get("sector", "")),
            "industry": html.escape(meta.get("industry", "") or info.get("industry", "")),
            "per": info.get("trailingPE"),
            "flags": flags,
            "updated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        }

    def _rsi(self, close, period=14):
        if len(close) < period + 1:
            return 50, "N/A"
        d = close.diff()
        g = d.where(d > 0, 0).rolling(period).mean()
        lo = (-d.where(d < 0, 0)).rolling(period).mean()
        rs = g / lo.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        v = rsi.iloc[-1]
        if pd.isna(v):
            return 50, "N/A"
        if v < 30:   return 85, f"RSI:{v:.0f} 과매도"
        elif v < 45: return 70, f"RSI:{v:.0f} 반등구간"
        elif v < 55: return 55, f"RSI:{v:.0f} 중립"
        elif v < 70: return 45, f"RSI:{v:.0f} 상승중"
        return 20, f"RSI:{v:.0f} 과매수"

    def _macd(self, close):
        if len(close) < 26:
            return 50, "N/A"
        macd = close.ewm(span=12).mean() - close.ewm(span=26).mean()
        sig = macd.ewm(span=9).mean()
        h = macd - sig
        if h.iloc[-1] > 0 and h.iloc[-2] <= 0:
            return 90, "골든크로스!"
        elif h.iloc[-1] > 0 and h.iloc[-1] > h.iloc[-2]:
            return 70, "히스토그램↑"
        elif h.iloc[-1] > 0:
            return 55, "양수"
        elif h.iloc[-1] < 0 and h.iloc[-1] > h.iloc[-2]:
            return 60, "반등시도"
        return 30, "음수"

    def _momentum(self, close):
        if len(close) < 20:
            return 50, "N/A"
        r5 = (close.iloc[-1] / close.iloc[-5] - 1) * 100
        r10 = (close.iloc[-1] / close.iloc[-10] - 1) * 100
        if 0 < r5 < 3 and r10 > 0:
            return 75, f"적절상승 {r5:+.1f}%"
        elif -3 < r5 < 0 and r10 > 0:
            return 65, f"눌림목 {r5:+.1f}%"
        elif r5 > 5:
            return 35, f"이미상승 {r5:+.1f}%"
        elif r5 < -5:
            return 40, f"급락 {r5:+.1f}%"
        return 50, f"횡보 {r5:+.1f}%"

    def _bulk_download(self, tickers, period="6mo", chunk_size=500):
        """yf.download()로 OHLCV 일괄 다운로드 — 개별 호출 대비 10x+ 빠름"""
        all_data = {}
        total_chunks = (len(tickers) + chunk_size - 1) // chunk_size

        for i in range(0, len(tickers), chunk_size):
            chunk = tickers[i:i + chunk_size]
            chunk_num = i // chunk_size + 1
            print(f"  📦 다운로드 {chunk_num}/{total_chunks} ({len(chunk)}종목)...")
            try:
                df = yf.download(
                    chunk, period=period, group_by="ticker",
                    threads=True, progress=False, timeout=30
                )
                if df.empty:
                    continue

                if len(chunk) == 1:
                    t = chunk[0]
                    if not df.empty and len(df) >= 30:
                        all_data[t] = df
                else:
                    for t in chunk:
                        try:
                            ticker_df = df[t].dropna(how="all")
                            if not ticker_df.empty and len(ticker_df) >= 30:
                                all_data[t] = ticker_df
                        except (KeyError, TypeError):
                            pass
            except Exception as e:
                print(f"    ⚠️ 다운로드 오류: {e}")

            if i + chunk_size < len(tickers):
                time.sleep(1)

        return all_data

    def _add_sector_bonuses(self, results):
        """섹터 상대강도 보너스: 같은 섹터 대비 초과 수익 시 가산"""
        sector_returns = {}
        for r in results:
            sector = r.get("sector", "")
            if sector and sector != "N/A" and sector != "":
                if sector not in sector_returns:
                    sector_returns[sector] = []
                sector_returns[sector].append(r["return_5d"])

        sector_avg = {}
        for s, rets in sector_returns.items():
            if len(rets) >= 3:
                sector_avg[s] = np.mean(rets)

        for r in results:
            sector = r.get("sector", "")
            if sector in sector_avg:
                alpha = r["return_5d"] - sector_avg[sector]
                r["sector_alpha"] = round(alpha, 1)
                if alpha > 5:
                    r["total_score"] = min(100, r["total_score"] + 3)
                elif alpha > 2:
                    r["total_score"] = min(100, r["total_score"] + 1)
            else:
                r["sector_alpha"] = None

        print(f"  📊 섹터 보너스 적용 ({len(sector_avg)}개 섹터)")

    def _enrich_top_results(self, results, top_n=100):
        """상위 종목: 개별 info 조회 → float ratio, 숏 이자율, 실적일, 섹터 보강"""
        candidates = results[:top_n]
        print(f"\n🔎 상위 {len(candidates)}종목 상세 정보 조회 중...")

        def fetch_info(r):
            try:
                info = yf.Ticker(r["ticker"]).info or {}
                return r["ticker"], info
            except Exception:
                return r["ticker"], {}

        enriched = {}
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(fetch_info, r): r["ticker"] for r in candidates}
            for f in as_completed(futs):
                try:
                    ticker, info = f.result()
                    enriched[ticker] = info
                except Exception:
                    pass

        short_count = 0
        earnings_count = 0

        for r in results:
            info = enriched.get(r["ticker"], {})
            if not info:
                continue

            # float ratio 업데이트
            fr_s, fr_d, fr_val = VolatilityAnalyzer.float_ratio(info)
            r["float_ratio"] = round(fr_val, 2) if fr_val is not None else r.get("float_ratio")

            # 섹터/산업 보강
            r["sector"] = html.escape(info.get("sector", r.get("sector", "")))
            r["industry"] = html.escape(info.get("industry", r.get("industry", "")))

            # yfinance 시총으로 보정
            yf_mcap = info.get("marketCap")
            if yf_mcap:
                r["market_cap"] = yf_mcap
                r["market_cap_fmt"] = format_market_cap(yf_mcap)

            # float ratio 보강 시 변동성 점수 재계산
            if fr_val is not None:
                old_vol = r["volatility_score"]
                new_vol = old_vol * 0.8 + fr_s * 0.2
                r["volatility_score"] = round(new_vol, 1)
                old_total = r["total_score"]
                r["total_score"] = round(
                    old_total + (new_vol - old_vol) * 0.15, 1
                )
                r["total_score"] = min(100, r["total_score"])

            # 플래그 업데이트
            if fr_val is not None and fr_val < 0.5 and "🎯 Low Float" not in r.get("flags", []):
                r.setdefault("flags", []).append("🎯 Low Float")

            # ===== 숏 이자율 =====
            short_pct = info.get("shortPercentOfFloat")
            if short_pct is not None:
                r["short_interest"] = round(short_pct * 100, 1)
                r["short_ratio"] = info.get("shortRatio")
                short_count += 1
                # 숏스퀴즈 보너스
                if short_pct >= 0.15 and r.get("accum_score", 0) >= 65:
                    r["total_score"] = min(100, r["total_score"] + 5)
                    if "🔥 숏스퀴즈 가능" not in r.get("flags", []):
                        r.setdefault("flags", []).append("🔥 숏스퀴즈 가능")
                elif short_pct >= 0.10:
                    if "📍 숏 비중↑" not in r.get("flags", []):
                        r.setdefault("flags", []).append("📍 숏 비중↑")

            # ===== 실적 발표 경고 =====
            try:
                earnings_ts = info.get("earningsTimestampStart") or info.get("earningsTimestamp")
                if earnings_ts:
                    earnings_date = datetime.fromtimestamp(earnings_ts)
                    days_until = (earnings_date - datetime.now()).days
                    if 0 <= days_until <= 14:
                        r["earnings_soon"] = True
                        r["earnings_days"] = days_until
                        r.setdefault("flags", []).append(f"📅 실적 D-{days_until}")
                        earnings_count += 1
                    elif -3 <= days_until < 0:
                        r["earnings_recent"] = True
                        r.setdefault("flags", []).append("📅 실적 완료")
            except Exception:
                pass

        results.sort(key=lambda x: x["total_score"], reverse=True)
        print(f"  ✅ 상세 정보 보강 완료 (숏데이터:{short_count}개, 실적경고:{earnings_count}개)")

    def run_full_scan(self):
        print("=" * 60)
        print("  🔍 급등 예측기 v4 - Low Cap US Stock Surge Detector")
        print("=" * 60)

        # 종목 수집
        print("\n📋 종목 수집 중...")
        all_tickers, self.metadata = UniverseFetcher.get_universe()
        total_universe = len(all_tickers)

        # SPY 포함 벌크 다운로드
        download_list = ["SPY"] + all_tickers
        print(f"\n📥 {total_universe}종목 + SPY 가격 데이터 일괄 다운로드...\n")
        t0 = time.time()
        all_data = self._bulk_download(download_list)

        # SPY 데이터 추출
        spy_df = all_data.pop("SPY", None)
        self.spy_close = spy_df["Close"] if spy_df is not None and not spy_df.empty else None
        if self.spy_close is not None:
            print(f"\n  ✅ SPY 데이터 로드 완료")
        else:
            print(f"\n  ⚠️ SPY 데이터 로드 실패")

        download_sec = time.time() - t0
        print(f"  📊 다운로드 완료: {len(all_data)}종목 ({download_sec:.0f}초)")
        print(f"\n🔍 {len(all_data)}종목 기술 분석 시작...\n")

        results = []
        failed = 0
        analyzed = 0

        for ticker, hist in all_data.items():
            analyzed += 1
            if analyzed % 200 == 0:
                print(f"  📊 분석 진행: {analyzed}/{len(all_data)}...")
            try:
                r = self.analyze_stock(ticker, hist)
                if r:
                    results.append(r)
                    if r["total_score"] >= 65:
                        print(f"    🎯 {r['name'][:25]:>25s} | {r['total_score']:5.1f}점 | {r['signal']} | {r['market_cap_fmt']}")
                else:
                    failed += 1
            except Exception:
                failed += 1

        elapsed = time.time() - t0
        print(f"\n⏱️ 분석 완료: {elapsed:.0f}초 (성공:{len(results)} 실패:{failed})")

        # 1차 정렬
        results.sort(key=lambda x: x["total_score"], reverse=True)

        # 섹터 상대강도 보너스
        self._add_sector_bonuses(results)

        # 상위 종목 상세 정보 보강 (숏 이자율, 실적 경고 포함)
        self._enrich_top_results(results, top_n=100)

        # 재정렬
        results.sort(key=lambda x: x["total_score"], reverse=True)

        # 시그널 지속성 & 신규 시그널
        new_signals = self.signal_tracker.get_new_signals(results)
        for r in results:
            r["signal_days"] = self.signal_tracker.get_persistence(r["ticker"])
            r["is_new"] = r["ticker"] in new_signals

        # 과거 적중률
        hit_rates = self.signal_tracker.compute_hit_rates()

        # 시그널 스냅샷 저장
        self.signal_tracker.save_snapshot(results)

        self.results = results

        elapsed_total = time.time() - t0
        self.market_summary = {
            "total_analyzed": len(results),
            "total_universe": total_universe,
            "surge_imminent": len([r for r in results if r["total_score"] >= 78]),
            "accumulating": len([r for r in results if r["total_score"] >= 68]),
            "watchlist": len([r for r in results if r["total_score"] >= 55]),
            "avg_score": round(np.mean([r["total_score"] for r in results]), 1) if results else 0,
            "low_float_count": len([r for r in results if (r.get("float_ratio") or 1) < 0.5]),
            "high_vol_count": len([r for r in results if (r.get("volatility_score") or 0) >= 70]),
            "new_signal_count": len([r for r in results if r.get("is_new") and r["total_score"] >= 55]),
            "short_squeeze_count": len([r for r in results if r.get("short_interest") and r["short_interest"] >= 15]),
            "hit_rates": hit_rates,
            "scan_sec": round(elapsed_total),
            "updated_at": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST"),
        }
        return results

    def save_results(self, path="data/analysis.json"):
        os.makedirs(os.path.dirname(path), exist_ok=True)

        # 데이터 최적화: 상위 500개 상세, 나머지 요약
        full_stocks = self.results[:500]
        minimal_stocks = []
        for r in self.results[500:]:
            minimal_stocks.append({
                "ticker": r["ticker"],
                "name": r["name"],
                "price": r["price"],
                "signal": r["signal"],
                "total_score": r["total_score"],
                "volatility_score": r.get("volatility_score"),
                "accum_score": r.get("accum_score"),
                "pattern_score": r.get("pattern_score"),
                "tech_score": r.get("tech_score"),
                "market_cap_fmt": r.get("market_cap_fmt"),
                "return_1d": r["return_1d"],
                "return_5d": r.get("return_5d"),
                "return_20d": r.get("return_20d"),
                "volume_ratio": r["volume_ratio"],
                "sector": r.get("sector", ""),
                "sparkline": r.get("sparkline"),
                "signal_days": r.get("signal_days", 0),
                "is_new": r.get("is_new", False),
            })

        out = {
            "version": "4.0",
            "focus": "low-cap-us-surge",
            "summary": self.market_summary,
            "stocks": full_stocks + minimal_stocks,
        }

        # JSON (readable)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

        # JS (minified for browser)
        js = path.replace(".json", ".js")
        with open(js, "w", encoding="utf-8") as f:
            f.write("var STOCK_DATA = ")
            json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
            f.write(";\n")

        json_size = os.path.getsize(path) / 1024 / 1024
        js_size = os.path.getsize(js) / 1024 / 1024
        print(f"💾 저장: {path} ({json_size:.1f}MB) + {js} ({js_size:.1f}MB)")

    def build_telegram_msg(self, top_n=15):
        kst = timezone(timedelta(hours=9))
        now = datetime.now(kst)
        s = self.market_summary

        msg = f"🔍 *급등 예측 리포트 v4*\n"
        msg += f"📅 {now.strftime('%Y-%m-%d %H:%M')} KST\n"
        msg += f"🎯 미국 소형주 (시총 < $2B)\n"
        msg += "━" * 25 + "\n\n"
        msg += f"📊 *스캔 결과* ({s['total_analyzed']}종목 분석)\n"
        msg += f"🔴 급등임박: {s['surge_imminent']}개 | 🟠 매집: {s['accumulating']}개 | 🟡 관심: {s['watchlist']}개\n"
        msg += f"🎯 Low Float: {s['low_float_count']}개 | 🌋 고변동: {s['high_vol_count']}개\n"
        msg += f"🆕 신규시그널: {s.get('new_signal_count', 0)}개 | 📍 숏스퀴즈후보: {s.get('short_squeeze_count', 0)}개\n"

        # 적중률 표시
        hr = s.get("hit_rates")
        if hr:
            msg += "\n📈 *과거 적중률*\n"
            for period, data in hr.items():
                msg += f"  {period}: 10%↑ {data['hit_10pct']}% | 5%↑ {data['hit_5pct']}% | 평균 {data['avg_return']:+.1f}% ({data['total']}건)\n"

        msg += "\n"

        surge = [r for r in self.results if r["total_score"] >= 78]
        if surge:
            msg += "🔴 *급등 임박*\n\n"
            for r in surge[:5]:
                new_tag = "🆕 " if r.get("is_new") else ""
                days_tag = f"[{r.get('signal_days', 0)}일]" if r.get("signal_days", 0) > 1 else ""
                msg += f"*{new_tag}{r['name']}* ({r['ticker']}) {r['total_score']}점 {days_tag}\n"
                msg += f"  시총:{r['market_cap_fmt']} | Vol:{r.get('volatility_score', '-')} Acc:{r['accum_score']} Pat:{r['pattern_score']} Tech:{r['tech_score']}\n"
                if r.get("short_interest"):
                    msg += f"  숏비중: {r['short_interest']:.1f}%\n"
                for fl in r.get("flags", [])[:2]:
                    msg += f"  {fl}\n"
                msg += "\n"

        accum = [r for r in self.results if 68 <= r["total_score"] < 78]
        if accum:
            msg += "🟠 *매집 진행*\n\n"
            for r in accum[:7]:
                new_tag = "🆕 " if r.get("is_new") else ""
                msg += f"*{new_tag}{r['name']}* ({r['ticker']}) {r['total_score']}점 | 5D:{r['return_5d']:+.1f}% | {r['market_cap_fmt']}\n"
                if r.get("flags"):
                    msg += f"  {r['flags'][0]}\n"

        msg += "\n" + "━" * 25 + "\n⚠️ 기술적 분석 참고자료. 투자 판단은 본인 책임."
        return msg

    def send_telegram(self, message):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("\n📱 텔레그램 미설정 - 미리보기:\n")
            print(message)
            return

        base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
        try:
            me = requests.get(f"{base}/getMe", timeout=10)
            if me.status_code != 200:
                print(f"❌ 텔레그램 봇 토큰 무효: {me.text}")
                print("   → BotFather에서 토큰을 재확인하고 GitHub Secrets를 업데이트하세요")
                return
            print(f"✅ 봇 확인: {me.json().get('result', {}).get('username', '?')}")
        except Exception as e:
            print(f"❌ 텔레그램 연결 실패: {e}")
            return

        url = f"{base}/sendMessage"
        parts, m = [], message
        while m:
            if len(m) <= 4096:
                parts.append(m)
                break
            i = m.rfind('\n', 0, 4096)
            if i == -1:
                i = 4096
            parts.append(m[:i])
            m = m[i:]
        for p in parts:
            try:
                r = requests.post(url, json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": p,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                }, timeout=15)
                if r.status_code == 200:
                    print("✅ 전송 완료!")
                else:
                    print(f"⚠️ Markdown 전송 실패 ({r.status_code}), 일반 텍스트로 재시도...")
                    r2 = requests.post(url, json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": p.replace("*", "").replace("_", ""),
                        "disable_web_page_preview": True,
                    }, timeout=15)
                    print("✅ 전송 완료 (텍스트)" if r2.status_code == 200 else f"❌ 실패: {r2.text}")
            except Exception as e:
                print(f"❌ 오류: {e}")


def main():
    p = PreSurgePredictor()
    results = p.run_full_scan()
    if not results:
        print("❌ 결과 없음")
        return
    p.save_results("data/analysis.json")
    p.send_telegram(p.build_telegram_msg())

    print("\n" + "=" * 60)
    print("  🏆 TOP 10 급등 후보 (미국 소형주)")
    print("=" * 60)
    for i, r in enumerate(results[:10], 1):
        new_tag = "🆕" if r.get("is_new") else "  "
        days = f"[{r['signal_days']}d]" if r.get("signal_days", 0) > 1 else "     "
        print(f"  {new_tag} {i:2d}. {r['name']:>25s} | {r['total_score']:5.1f}점 | {r['market_cap_fmt']:>8s} | {days} | {r['signal']}")
        for fl in r.get("flags", []):
            print(f"         {fl}")


if __name__ == "__main__":
    main()
