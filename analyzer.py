"""
급등 예측기 v3 - Low Cap US Stock Surge Detector
핵심: 시총이 낮고 변동성이 높은 미국 소형주에서 급등 직전 종목을 찾는다

[분석 카테고리]
1. 변동성 프로파일 (15%) — ATR%, 역사적변동성, 유동비율, 상대거래량, 캔들범위
2. 매집 감지 (35%) — OBV 다이버전스, Chaikin MF, 거래량 건조→급증, A/D Line
3. 차트 패턴 (30%) — 볼린저스퀴즈, 저항선접근, 삼각수렴, 컵앤핸들, 이평선밀집,
                       저점상승, 베이스돌파, 포켓피봇, 갭분석, VWAP회복, 상대강도
4. 기술 모멘텀 (20%) — RSI, MACD, 모멘텀
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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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
        """Nasdaq Screener API로 소형주 수집 — 메타데이터(이름, 시총) 포함 반환"""
        metadata = {}  # {symbol: {"name": ..., "market_cap": ...}}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        offset = 0
        limit = 200
        total = None

        while True:
            url = (
                f"https://api.nasdaq.com/api/screener/stocks"
                f"?tableonly=true&limit={limit}&offset={offset}"
            )
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

                    if (symbol
                        and mcap is not None
                        and 0 < mcap <= max_market_cap
                        and not any(c in symbol for c in ['^', '/', '.'])
                    ):
                        metadata[symbol] = {"name": name, "market_cap": mcap}

                offset += limit
                if offset >= total:
                    break
                time.sleep(0.3)

            except Exception as e:
                print(f"  ⚠️ Nasdaq API 오류 (offset {offset}): {e}")
                break

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
                metadata[t] = {"name": t, "market_cap": None}

        for t in UniverseFetcher.EXTRA_TICKERS:
            if t not in metadata:
                metadata[t] = {"name": t, "market_cap": None}

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
        obv = [0]
        for i in range(1, len(close)):
            if close.iloc[i] > close.iloc[i-1]:
                obv.append(obv[-1] + volume.iloc[i])
            elif close.iloc[i] < close.iloc[i-1]:
                obv.append(obv[-1] - volume.iloc[i])
            else:
                obv.append(obv[-1])
        return pd.Series(obv, index=close.index)

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


# ====== 메인 엔진 ======

class PreSurgePredictor:
    """급등 예측기 v3 메인"""

    def __init__(self):
        self.results = []
        self.market_summary = {}
        self.spy_close = None
        self.metadata = {}  # Nasdaq API에서 받은 {ticker: {name, market_cap}}

    def analyze_stock(self, ticker, hist):
        """개별 종목 분석 — hist는 yf.download()로 받은 DataFrame"""
        if hist is None or hist.empty or len(hist) < 30:
            return None

        meta = self.metadata.get(ticker, {})
        c, h, l, v = hist["Close"], hist["High"], hist["Low"], hist["Volume"]
        o = hist["Open"]

        # NaN 제거된 유효 데이터 확인
        if c.dropna().empty or len(c.dropna()) < 30:
            return None

        # info는 enrichment 단계에서 채워짐 (벌크 다운로드에서는 없음)
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

        # ATR% 원시값 추출
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

        mcap = meta.get("market_cap") or info.get("marketCap")

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
            "return_1d": round(r1d, 2),
            "return_5d": round(r5d, 2),
            "return_20d": round(r20d, 2),
            "volume_ratio": round(vr, 2),
            "market_cap": mcap,
            "market_cap_fmt": format_market_cap(mcap),
            "float_ratio": round(fr_val, 2) if fr_val is not None else None,
            "atr_pct": atr_pct_val,
            "details": {
                "volatility": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in vol_items],
                "accumulation": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in acc_items],
                "pattern": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in pat_items],
                "technical": [{"name": i["name"], "score": i["score"], "value": i["value"]} for i in tech_items],
            },
            "sector": html.escape(info.get("sector", "")),
            "industry": html.escape(info.get("industry", "")),
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
                    # 단일 종목이면 MultiIndex가 아님
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

    def _enrich_top_results(self, results, top_n=100):
        """상위 종목에 대해 개별 info 조회하여 float ratio, sector 등 보강"""
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

        # 결과 보강 및 재채점
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

            # yfinance 시총으로 보정 (더 정확)
            yf_mcap = info.get("marketCap")
            if yf_mcap:
                r["market_cap"] = yf_mcap
                r["market_cap_fmt"] = format_market_cap(yf_mcap)

            # float ratio 보강 시 변동성 점수 재계산
            if fr_val is not None:
                old_vol = r["volatility_score"]
                # fr 비중 20%: 새 점수 = 기존의 80% + 새 fr의 20%
                new_vol = old_vol * 0.8 + fr_s * 0.2
                r["volatility_score"] = round(new_vol, 1)
                # 전체 점수 재계산
                old_total = r["total_score"]
                r["total_score"] = round(
                    old_total + (new_vol - old_vol) * 0.15, 1
                )
                r["total_score"] = min(100, r["total_score"])

            # 플래그 업데이트
            if fr_val is not None and fr_val < 0.5 and "🎯 Low Float" not in r.get("flags", []):
                r.setdefault("flags", []).append("🎯 Low Float")

        # 재정렬
        results.sort(key=lambda x: x["total_score"], reverse=True)
        print(f"  ✅ 상세 정보 보강 완료")

    def run_full_scan(self):
        print("=" * 60)
        print("  🔍 급등 예측기 v3 - Low Cap US Stock Surge Detector")
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

        results.sort(key=lambda x: x["total_score"], reverse=True)

        # 상위 종목 상세 정보 보강 (float ratio, sector 등)
        self._enrich_top_results(results, top_n=100)
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
            "scan_sec": round(elapsed_total),
            "updated_at": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST"),
        }
        return results

    def save_results(self, path="data/analysis.json"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        out = {
            "version": "3.0",
            "focus": "low-cap-us-surge",
            "summary": self.market_summary,
            "stocks": self.results,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        js = path.replace(".json", ".js")
        with open(js, "w", encoding="utf-8") as f:
            f.write("var STOCK_DATA = ")
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write(";\n")
        print(f"💾 저장: {path} + {js}")

    def build_telegram_msg(self, top_n=15):
        kst = timezone(timedelta(hours=9))
        now = datetime.now(kst)
        s = self.market_summary

        msg = f"🔍 *급등 예측 리포트 v3*\n"
        msg += f"📅 {now.strftime('%Y-%m-%d %H:%M')} KST\n"
        msg += f"🎯 미국 소형주 (시총 < $2B)\n"
        msg += "━" * 25 + "\n\n"
        msg += f"📊 *스캔 결과* ({s['total_analyzed']}종목 분석)\n"
        msg += f"🔴 급등임박: {s['surge_imminent']}개 | 🟠 매집: {s['accumulating']}개 | 🟡 관심: {s['watchlist']}개\n"
        msg += f"🎯 Low Float: {s['low_float_count']}개 | 🌋 고변동: {s['high_vol_count']}개\n\n"

        surge = [r for r in self.results if r["total_score"] >= 78]
        if surge:
            msg += "🔴 *급등 임박*\n\n"
            for r in surge[:5]:
                msg += f"*{r['name']}* ({r['ticker']}) {r['total_score']}점\n"
                msg += f"  시총:{r['market_cap_fmt']} | Vol:{r.get('volatility_score', '-')} Acc:{r['accum_score']} Pat:{r['pattern_score']} Tech:{r['tech_score']}\n"
                for fl in r.get("flags", [])[:2]:
                    msg += f"  {fl}\n"
                msg += "\n"

        accum = [r for r in self.results if 68 <= r["total_score"] < 78]
        if accum:
            msg += "🟠 *매집 진행*\n\n"
            for r in accum[:7]:
                msg += f"*{r['name']}* ({r['ticker']}) {r['total_score']}점 | 5D:{r['return_5d']:+.1f}% | {r['market_cap_fmt']}\n"
                if r.get("flags"):
                    msg += f"  {r['flags'][0]}\n"

        msg += "\n" + "━" * 25 + "\n⚠️ 기술적 분석 참고자료. 투자 판단은 본인 책임."
        return msg

    def send_telegram(self, message):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("\n📱 텔레그램 미설정 - 미리보기:\n")
            print(message)
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
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
                print("✅ 전송 완료!" if r.status_code == 200 else f"❌ 실패: {r.text}")
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
        print(f"  {i:2d}. {r['name']:>25s} | {r['total_score']:5.1f}점 | {r['market_cap_fmt']:>8s} | Vol:{r.get('volatility_score', '-'):>4} | {r['signal']}")
        for fl in r.get("flags", []):
            print(f"      {fl}")


if __name__ == "__main__":
    main()
