"""
급등 예측기 v2 - Pre-Surge Detector
핵심: 이미 오른 종목이 아니라, 매집 중이고 곧 터질 종목을 찾는다

[매집 감지 신호]
1. OBV 다이버전스: 가격은 횡보/하락인데 OBV는 상승 → 누군가 조용히 사고 있다
2. Chaikin Money Flow: 자금 유입 강도 측정
3. 거래량 건조 후 급증: 관심 없다가 갑자기 거래량 터지는 패턴
4. Accumulation/Distribution Line: 종가 위치 기반 매집/분산 판단

[돌파 직전 패턴]
5. 변동성 스퀴즈: 볼린저밴드 극도로 좁아짐 → 곧 방향성 폭발
6. 삼각수렴: 고점은 낮아지고 저점은 높아지는 수렴 패턴
7. 저항선 근접: 여러 번 맞고 내려온 가격대에 다시 접근
8. 컵앤핸들: 바닥 다지기 완료 후 돌파 준비
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import requests
import warnings
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

# ====== 설정 ======
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


class UniverseFetcher:
    """전체 종목 리스트 수집"""

    @staticmethod
    def get_us_universe():
        """미국 전체 종목 (S&P500 + 나스닥 + 소형 성장주)"""
        tickers = set()

        # S&P 500
        try:
            tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
            sp500 = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
            tickers.update(sp500)
            print(f"  ✅ S&P 500: {len(sp500)}종목")
        except Exception as e:
            print(f"  ⚠️ S&P 500 로드 실패: {e}")

        # 나스닥 100
        try:
            tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
            for t in tables:
                if "Ticker" in t.columns:
                    nasdaq = t["Ticker"].tolist()
                    tickers.update(nasdaq)
                    print(f"  ✅ NASDAQ 100: {len(nasdaq)}종목")
                    break
        except Exception as e:
            print(f"  ⚠️ NASDAQ 100 로드 실패: {e}")

        # 추가 관심 종목 (소형주, 테마주)
        extra = [
            "PL","RDW","RKLB","LUNR","ASTS","MNTS","BKSY","SATL","SPCE",
            "SMCI","SOUN","BBAI","IREN","CLSK","APLD",
            "SMR","NNE","OKLO","CEG","VST",
            "IONQ","RGTI","QUBT",
            "CRSP","NTLA","BEAM","EDIT",
            "SOFI","AFRM","UPST","NU",
            "HIMS","DUOL","CAVA","TOST",
            "SPY","QQQ","IWM","ARKK","XLF","XLE","XLK","SMH",
        ]
        tickers.update(extra)
        print(f"  📊 미국 총: {len(tickers)}종목")
        return sorted(list(tickers))

    @staticmethod
    def get_kr_universe():
        """한국 주요 종목"""
        return {
            "005930.KS":"삼성전자","000660.KS":"SK하이닉스","373220.KS":"LG에너지솔루션",
            "207940.KS":"삼성바이오로직스","005380.KS":"현대차","006400.KS":"삼성SDI",
            "051910.KS":"LG화학","035420.KS":"NAVER","035720.KS":"카카오",
            "000270.KS":"기아","068270.KS":"셀트리온","105560.KS":"KB금융",
            "055550.KS":"신한지주","012450.KS":"한화에어로스페이스","047810.KS":"한국항공우주",
            "299660.KS":"LIG넥스원","042700.KS":"한미반도체","009150.KS":"삼성전기",
            "028260.KS":"삼성물산","066570.KS":"LG전자","003670.KS":"포스코퓨처엠",
            "034020.KS":"두산에너빌리티","326030.KS":"SK바이오팜",
            "267260.KS":"HD현대일렉트릭","329180.KS":"HD현대중공업",
            "009540.KS":"HD한국조선해양","015760.KS":"한국전력",
            "036570.KS":"엔씨소프트","251270.KS":"넷마블",
            "247540.KQ":"에코프로비엠","403870.KQ":"HPSP","058470.KQ":"리노공업",
            "328130.KQ":"루닛","196170.KQ":"알테오젠","145020.KQ":"휴젤",
            "041510.KQ":"에스엠","035900.KQ":"JYP Ent.","352820.KQ":"하이브",
            "039030.KQ":"이오테크닉스","357780.KQ":"솔브레인",
            "036930.KQ":"주성엔지니어링","293490.KQ":"카카오게임즈",
        }


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
        seg = [(-30,-20), (-20,-10), (-10,None)]
        highs = [high.iloc[s:e].max() for s, e in seg]
        lows = [low.iloc[s:e].min() for s, e in seg]
        h_fall = highs[2] < highs[1] < highs[0]
        l_rise = lows[2] > lows[1] > lows[0]
        r1, r3 = highs[0]-lows[0], highs[2]-lows[2]
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
        handle = (r40.iloc[-10:].max() - close.iloc[-1]) / r40.iloc[-10:].max() * 100

        if rim_diff < 5 and 5 < depth < 30 and 0 < handle < 8:
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


class PreSurgePredictor:
    """급등 예측기 v2 메인"""

    def __init__(self):
        self.results = []
        self.market_summary = {}
        self.kr_names = {}

    def fetch_data(self, ticker, period="6mo"):
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period=period)
            if hist.empty or len(hist) < 30:
                return None
            info = {}
            try:
                info = stock.info or {}
            except:
                pass
            return {"ticker": ticker, "history": hist, "info": info}
        except:
            return None

    def analyze_stock(self, ticker):
        data = self.fetch_data(ticker)
        if data is None:
            return None

        hist = data["history"]
        info = data["info"]
        c, h, l, v = hist["Close"], hist["High"], hist["Low"], hist["Volume"]

        # 매집 감지 (40%)
        obv_s, obv_d = AccumulationDetector.obv_divergence(c, v)
        cmf_s, cmf_d = AccumulationDetector.chaikin_mf(h, l, c, v)
        vds_s, vds_d = AccumulationDetector.volume_dryup_spike(v)
        ad_s, ad_d = AccumulationDetector.ad_line(h, l, c, v)

        acc_items = [
            {"name":"OBV 다이버전스","score":obv_s,"value":obv_d,"w":30},
            {"name":"Chaikin MF","score":cmf_s,"value":cmf_d,"w":25},
            {"name":"거래량 건조→급증","score":vds_s,"value":vds_d,"w":25},
            {"name":"A/D Line","score":ad_s,"value":ad_d,"w":20},
        ]

        # 돌파 패턴 (35%)
        sq_s, sq_d = PatternDetector.bollinger_squeeze(c)
        rs_s, rs_d = PatternDetector.resistance_approach(h, c)
        tr_s, tr_d = PatternDetector.triangle_convergence(h, l, c)
        ch_s, ch_d = PatternDetector.cup_and_handle(c, v)
        ma_s, ma_d = PatternDetector.ma_tightening(c)
        hl_s, hl_d = PatternDetector.higher_lows(l)

        pat_items = [
            {"name":"볼린저 스퀴즈","score":sq_s,"value":sq_d,"w":20},
            {"name":"저항선 접근","score":rs_s,"value":rs_d,"w":20},
            {"name":"삼각수렴","score":tr_s,"value":tr_d,"w":15},
            {"name":"컵앤핸들","score":ch_s,"value":ch_d,"w":15},
            {"name":"이평선 밀집","score":ma_s,"value":ma_d,"w":15},
            {"name":"연속 저점↑","score":hl_s,"value":hl_d,"w":15},
        ]

        # 기술 지표 (25%)
        rsi_s, rsi_d = self._rsi(c)
        macd_s, macd_d = self._macd(c)
        mom_s, mom_d = self._momentum(c)

        tech_items = [
            {"name":"RSI","score":rsi_s,"value":rsi_d,"w":35},
            {"name":"MACD","score":macd_s,"value":macd_d,"w":35},
            {"name":"모멘텀","score":mom_s,"value":mom_d,"w":30},
        ]

        def wavg(items):
            tw = sum(i["w"] for i in items)
            return sum(i["score"] * i["w"] / tw for i in items)

        acc_avg = wavg(acc_items)
        pat_avg = wavg(pat_items)
        tech_avg = wavg(tech_items)

        total = acc_avg * 0.40 + pat_avg * 0.35 + tech_avg * 0.25

        # 보너스
        if acc_avg >= 70 and sq_s >= 70:
            total = min(100, total * 1.15)
        if acc_avg >= 65 and rs_s >= 70:
            total = min(100, total * 1.10)

        # 시그널
        if total >= 78:   sig = "🔴 급등 임박"
        elif total >= 68: sig = "🟠 매집 진행"
        elif total >= 55: sig = "🟡 관심"
        elif total >= 40: sig = "🔵 대기"
        else:             sig = "⚪ 관망"

        name = self.kr_names.get(ticker, info.get("shortName", ticker))
        mkt = "KR" if ".KS" in ticker or ".KQ" in ticker else "US"

        r1d = (c.iloc[-1]/c.iloc[-2]-1)*100 if len(c)>=2 else 0
        r5d = (c.iloc[-1]/c.iloc[-5]-1)*100 if len(c)>=5 else 0
        r20d = (c.iloc[-1]/c.iloc[-20]-1)*100 if len(c)>=20 else 0
        vr = float(v[-3:].mean()/v[-20:].mean()) if len(v)>=20 and v[-20:].mean()>0 else 1.0

        flags = []
        if acc_avg >= 70 and sq_s >= 70: flags.append("💥 매집+스퀴즈")
        if obv_s >= 75: flags.append("🕵️ OBV 매집")
        if sq_s >= 80: flags.append("🔥 변동성 폭발 임박")
        if rs_s >= 75: flags.append("🚪 저항선 돌파 임박")
        if vds_s >= 75: flags.append("⚡ 거래량 급증")
        if acc_avg >= 70: flags.append("📦 강한 매집")

        return {
            "ticker": ticker, "name": name, "market": mkt,
            "price": round(float(c.iloc[-1]), 2),
            "signal": sig, "total_score": round(total, 1),
            "accum_score": round(acc_avg, 1),
            "pattern_score": round(pat_avg, 1),
            "tech_score": round(tech_avg, 1),
            "return_1d": round(r1d, 2), "return_5d": round(r5d, 2), "return_20d": round(r20d, 2),
            "volume_ratio": round(vr, 2),
            "details": {
                "accumulation": [{"name":i["name"],"score":i["score"],"value":i["value"]} for i in acc_items],
                "pattern": [{"name":i["name"],"score":i["score"],"value":i["value"]} for i in pat_items],
                "technical": [{"name":i["name"],"score":i["score"],"value":i["value"]} for i in tech_items],
            },
            "sector": info.get("sector",""), "industry": info.get("industry",""),
            "market_cap": info.get("marketCap"), "per": info.get("trailingPE"),
            "flags": flags,
            "updated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        }

    def _rsi(self, close, period=14):
        if len(close) < period+1: return 50, "N/A"
        d = close.diff()
        g = d.where(d>0,0).rolling(period).mean()
        lo = (-d.where(d<0,0)).rolling(period).mean()
        rs = g / lo.replace(0, np.nan)
        rsi = 100 - (100/(1+rs))
        v = rsi.iloc[-1]
        if pd.isna(v): return 50, "N/A"
        if v < 30:   return 85, f"RSI:{v:.0f} 과매도"
        elif v < 45: return 70, f"RSI:{v:.0f} 반등구간"
        elif v < 55: return 55, f"RSI:{v:.0f} 중립"
        elif v < 70: return 45, f"RSI:{v:.0f} 상승중"
        return 20, f"RSI:{v:.0f} 과매수"

    def _macd(self, close):
        if len(close) < 26: return 50, "N/A"
        macd = close.ewm(span=12).mean() - close.ewm(span=26).mean()
        sig = macd.ewm(span=9).mean()
        h = macd - sig
        if h.iloc[-1] > 0 and h.iloc[-2] <= 0: return 90, "골든크로스!"
        elif h.iloc[-1] > 0 and h.iloc[-1] > h.iloc[-2]: return 70, "히스토그램↑"
        elif h.iloc[-1] > 0: return 55, "양수"
        elif h.iloc[-1] < 0 and h.iloc[-1] > h.iloc[-2]: return 60, "반등시도"
        return 30, "음수"

    def _momentum(self, close):
        if len(close) < 20: return 50, "N/A"
        r5 = (close.iloc[-1]/close.iloc[-5]-1)*100
        r10 = (close.iloc[-1]/close.iloc[-10]-1)*100
        if 0 < r5 < 3 and r10 > 0: return 75, f"적절상승 {r5:+.1f}%"
        elif -3 < r5 < 0 and r10 > 0: return 65, f"눌림목 {r5:+.1f}%"
        elif r5 > 5: return 35, f"이미상승 {r5:+.1f}%"
        elif r5 < -5: return 40, f"급락 {r5:+.1f}%"
        return 50, f"횡보 {r5:+.1f}%"

    def run_full_scan(self):
        print("=" * 60)
        print("  🔍 급등 예측기 v2 - Pre-Surge Full Scan")
        print("=" * 60)

        print("\n📋 종목 수집 중...")
        us_tickers = UniverseFetcher.get_us_universe()
        kr_data = UniverseFetcher.get_kr_universe()
        self.kr_names = kr_data
        all_tickers = us_tickers + list(kr_data.keys())
        print(f"\n🔍 총 {len(all_tickers)}종목 분석 시작...\n")

        results = []
        failed = 0
        t0 = time.time()

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(self.analyze_stock, t): t for t in all_tickers}
            for i, f in enumerate(as_completed(futs), 1):
                try:
                    r = f.result()
                    if r:
                        results.append(r)
                        if r["total_score"] >= 65:
                            print(f"  🎯 [{i}/{len(all_tickers)}] {r['name']:>25s} | {r['total_score']:5.1f}점 | {r['signal']}")
                            for fl in r.get("flags",[]):
                                print(f"      {fl}")
                    else:
                        failed += 1
                except:
                    failed += 1
                if i % 50 == 0:
                    print(f"  ... {i}/{len(all_tickers)} ({time.time()-t0:.0f}초)")

        elapsed = time.time() - t0
        print(f"\n⏱️ 완료: {elapsed:.0f}초 (성공:{len(results)} 실패:{failed})")

        results.sort(key=lambda x: x["total_score"], reverse=True)
        self.results = results

        us_r = [r for r in results if r["market"]=="US"]
        kr_r = [r for r in results if r["market"]=="KR"]
        self.market_summary = {
            "total_analyzed": len(results),
            "us_count": len(us_r), "kr_count": len(kr_r),
            "surge_imminent": len([r for r in results if r["total_score"]>=78]),
            "accumulating": len([r for r in results if r["total_score"]>=68]),
            "watchlist": len([r for r in results if r["total_score"]>=55]),
            "us_avg": round(np.mean([r["total_score"] for r in us_r]),1) if us_r else 0,
            "kr_avg": round(np.mean([r["total_score"] for r in kr_r]),1) if kr_r else 0,
            "scan_sec": round(elapsed),
            "updated_at": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST"),
        }
        return results

    def save_results(self, path="data/analysis.json"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        out = {"version":"2.0","focus":"pre-surge","summary":self.market_summary,"stocks":self.results}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        js = path.replace(".json", ".js")
        with open(js, "w", encoding="utf-8") as f:
            f.write("var STOCK_DATA = "); json.dump(out, f, ensure_ascii=False, indent=2); f.write(";\n")
        print(f"💾 저장: {path} + {js}")

    def build_telegram_msg(self, top_n=15):
        kst = timezone(timedelta(hours=9))
        now = datetime.now(kst)
        s = self.market_summary

        msg = f"🔍 *급등 예측 리포트 v2*\n📅 {now.strftime('%Y-%m-%d %H:%M')}\n"
        msg += "━" * 25 + "\n\n"
        msg += f"📊 *스캔 결과* ({s['total_analyzed']}종목)\n"
        msg += f"🔴 급등임박: {s['surge_imminent']}개 | 🟠 매집: {s['accumulating']}개 | 🟡 관심: {s['watchlist']}개\n\n"

        surge = [r for r in self.results if r["total_score"] >= 78]
        if surge:
            msg += "🔴 *급등 임박*\n\n"
            for r in surge[:5]:
                fl = "🇰🇷" if r["market"]=="KR" else "🇺🇸"
                t = r["ticker"].replace(".KS","").replace(".KQ","")
                msg += f"*{fl} {r['name']}* ({t}) {r['total_score']}점\n"
                msg += f"  매집:{r['accum_score']} 패턴:{r['pattern_score']} 기술:{r['tech_score']}\n"
                for f in r.get("flags",[])[:2]: msg += f"  {f}\n"
                msg += "\n"

        accum = [r for r in self.results if 68 <= r["total_score"] < 78]
        if accum:
            msg += "🟠 *매집 진행*\n\n"
            for r in accum[:7]:
                fl = "🇰🇷" if r["market"]=="KR" else "🇺🇸"
                t = r["ticker"].replace(".KS","").replace(".KQ","")
                msg += f"*{fl} {r['name']}* ({t}) {r['total_score']}점 | 5D:{r['return_5d']:+.1f}%\n"
                if r.get("flags"): msg += f"  {r['flags'][0]}\n"

        msg += "\n" + "━" * 25 + "\n⚠️ 기술적 분석 참고자료. 투자 판단은 본인 책임."
        return msg

    def send_telegram(self, message):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("\n📱 텔레그램 미설정 - 미리보기:\n"); print(message); return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        parts, m = [], message
        while m:
            if len(m) <= 4096: parts.append(m); break
            i = m.rfind('\n', 0, 4096)
            if i == -1: i = 4096
            parts.append(m[:i]); m = m[i:]
        for p in parts:
            try:
                r = requests.post(url, json={"chat_id":TELEGRAM_CHAT_ID,"text":p,"parse_mode":"Markdown","disable_web_page_preview":True}, timeout=15)
                print("✅ 전송 완료!" if r.status_code==200 else f"❌ 실패: {r.text}")
            except Exception as e: print(f"❌ 오류: {e}")


def main():
    p = PreSurgePredictor()
    results = p.run_full_scan()
    if not results: print("❌ 결과 없음"); return
    p.save_results("data/analysis.json")
    p.send_telegram(p.build_telegram_msg())

    print("\n" + "=" * 60 + "\n  🏆 TOP 10 급등 후보\n" + "=" * 60)
    for i, r in enumerate(results[:10], 1):
        fl = "🇰🇷" if r["market"]=="KR" else "🇺🇸"
        print(f"  {i:2d}. {fl} {r['name']:>25s} | {r['total_score']:5.1f}점 | 매집:{r['accum_score']:4.1f} 패턴:{r['pattern_score']:4.1f} | {r['signal']}")
        for f in r.get("flags",[]): print(f"      {f}")

if __name__ == "__main__":
    main()
