"""점수 함수 회귀 테스트 — `python test_scoring.py` 로 실행.

네트워크 호출은 하지 않지만 analyzer 를 임포트하므로 requirements.txt 설치가 필요하다.

여기 있는 검증들은 전부 실제로 발생했던 버그에서 나왔다.
  - 단조성: '낮을수록 좋은' 지표를 내림차순 테이블로 매핑해 중간 구간이
    최저점을 받던 버그 (부채비율 3.0배가 4.5배와 같은 8점)
  - 등급 일관성: 원시 점수로 등급을 매기고 반올림된 점수를 저장해
    64.0점인데 등급이 B 로 표시되던 버그
  - 데이터 왜곡: 일회성 이익·기저효과·통화 단위 불일치가 상위권을 차지하던 문제
"""

import sys

from analyzer import (FinancialHealth as H, Growth as G, Profitability as P,
                      QualityScreener, Valuation as V, grade_of, is_financial)

FAILS = []


def check(label, condition, detail=""):
    if condition:
        print(f"  \033[32m✓\033[0m {label}")
    else:
        print(f"  \033[31m✗\033[0m {label} {detail}")
        FAILS.append(label)


def monotonic(label, fn, xs, increasing):
    """점수가 단조롭게 변하는지 확인 (None 은 건너뛴다)."""
    scores = [fn(x) for x in xs]
    pairs = [(a, b) for a, b in zip(scores, scores[1:])
             if a is not None and b is not None]
    ok = all((b >= a) if increasing else (b <= a) for a, b in pairs)
    check(label, ok, f"-> {list(zip(xs, scores))}")


def test_monotonicity():
    print("\n[단조성] 낮을수록 좋은 지표")
    monotonic("부채비율", lambda x: H.debt_to_equity(x)[0],
              [10, 30, 84, 120, 200, 300, 450, 800], False)
    monotonic("PEG", lambda x: V.peg(x)[0], [0.5, 1.0, 1.5, 2.0, 3.0, 5.0], False)
    monotonic("순부채/EBITDA", lambda x: H.net_debt_ebitda(x * 100, 0, 100)[0],
              [0.5, 1.5, 2.5, 3.5, 5.0, 7.0], False)
    monotonic("PER(섹터상대)", lambda x: V._relative(x * 20, 20, "PER")[0],
              [0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.5], False)

    print("\n[단조성] 높을수록 좋은 지표")
    monotonic("ROE", lambda x: P.roe(x)[0],
              [-0.1, 0.02, 0.05, 0.10, 0.15, 0.22, 0.30], True)
    monotonic("영업이익률", lambda x: P.operating_margin(x)[0],
              [-0.05, 0.01, 0.04, 0.08, 0.15, 0.25, 0.35], True)
    monotonic("유동비율", lambda x: H.current_ratio(x)[0],
              [0.5, 0.9, 1.1, 1.3, 1.7, 2.2, 3.0], True)
    monotonic("매출성장", lambda x: G.revenue_growth(x)[0],
              [-0.2, -0.03, 0.01, 0.04, 0.10, 0.18, 0.35], True)


def test_units():
    print("\n[단위 변환] yfinance 는 debtToEquity 를 퍼센트로 준다")
    check("D/E 84.3% -> 0.84배", H.debt_to_equity(84.286)[1] == "부채비율 0.84배")
    check("D/E 447.6% -> 4.48배 최저점", H.debt_to_equity(447.6)[0] == 8)


def test_missing_vs_bad():
    print("\n['데이터 없음' 과 '나쁨' 의 구분]")
    check("은행 부채비율 결측 -> None(가중치 제외)", H.debt_to_equity(None)[0] is None)
    check("적자라 PER 없음 -> 감점", V.per(None, 20, False)[0] == 18)
    check("흑자인데 PER 결측 -> None", V.per(None, 20, True)[0] is None)
    check("적자 ROE -> 최저점", P.roe(-0.32)[0] == 5)


def test_distortion_guards():
    print("\n[데이터 왜곡 가드]")
    check("순이익률 37.5% vs 영업 8.2% -> 영업 기준으로 하향",
          P.net_margin(0.375, 0.082)[0] == P.operating_margin(0.082)[0])
    check("정상 순15% vs 영업20% -> 그대로", P.net_margin(0.15, 0.20)[0] == 83)
    check("매출성장 8608% -> 기저효과 처리", G.revenue_growth(86.086)[0] == 60)
    check("매출성장 35% -> 만점 유지", G.revenue_growth(0.35)[0] == 95)
    check("ROIC 380% -> 상한 적용", P.roic(0.76, 1e8, 0, 1e8, 5.0, 0)[0] == 85)
    check("FCF수익률 410% -> 단위 불일치, 점수화 안 함",
          V.fcf_yield(4.1e9, 1e9)[0] is None)
    check("EV/EBITDA 0.6 -> 단위 불일치", V.ev_ebitda(0.6, 10)[0] is None)
    check("현금/시총 251%(비금융) -> 감점", H.cash_position(2.51e6, 1e6)[0] == 45)
    check("현금/시총 500%(금융) -> 해당없음",
          H.cash_position(5e9, 1e9, financial=True)[0] is None)


def test_grade_consistency():
    print("\n[등급 일관성] 저장되는 점수와 등급이 어긋나면 안 된다")
    ok = True
    for raw in (63.96, 71.97, 79.95, 55.96, 44.97, 80.04, 72.0):
        rounded = round(raw, 1)
        if grade_of(rounded) != grade_of(round(rounded, 1)):
            ok = False
    check("반올림 경계에서 등급 일치", ok)
    check("80.0 -> A+", grade_of(80.0) == "A+")
    check("79.9 -> A", grade_of(79.9) == "A")
    check("44.9 -> D", grade_of(44.9) == "D")


def test_sector_classification():
    print("\n[금융업 판별]")
    check("Finance 섹터", is_financial("Finance", ""))
    check("Real Estate 섹터", is_financial("Real Estate", ""))
    check("업종명에 Bank", is_financial("", "Major Banks"))
    check("일반 제조업은 아님", not is_financial("Technology", "Semiconductors"))


def test_flags():
    print("\n[플래그]")
    base = dict(net_margin=0.10, op_margin=0.12, revenue_growth=0.10,
                dividend_yield=None, debt_to_equity=50.0, is_financial=False,
                cash=1e8, market_cap=1e9, is_profitable=True, roe=0.25,
                fcf=2e8, revenue=1e9)
    cats = dict(profitability=80, health=75, growth=70, valuation=70)

    good = QualityScreener._flags(base, cats)
    check("정상 우량주는 올라운더", "⭐ 올라운더" in good)

    one_off = QualityScreener._flags(dict(base, net_margin=0.375, op_margin=0.082), cats)
    check("일회성 이익 경고", "⚠️ 일회성 이익 의심" in one_off)
    check("일회성 이익이면 올라운더 미부여", "⭐ 올라운더" not in one_off)
    check("경고가 맨 앞에 온다", one_off[0].startswith("⚠️"))

    bank = QualityScreener._flags(
        dict(base, is_financial=True, cash=5e9, debt_to_equity=None), cats)
    check("금융업 현금과다는 오탐 아님", "⚠️ 재무수치 이상" not in bank)

    base_eff = QualityScreener._flags(dict(base, revenue_growth=86.0), cats)
    check("기저효과면 고성장 미부여", "🚀 고성장" not in base_eff)


def test_weight_renormalization():
    print("\n[가중치 재정규화] 측정 불가 지표는 임의값 대신 제외")
    items = [{"name": "a", "score": 80, "w": 50},
             {"name": "b", "score": None, "w": 30},
             {"name": "c", "score": 60, "w": 20}]
    score, cov = QualityScreener._wavg(items)
    expected = (80 * 50 + 60 * 20) / 70
    check("결측 지표 제외 후 재정규화", abs(score - expected) < 1e-9,
          f"{score} != {expected}")
    check("커버리지 = 70/100", abs(cov - 0.7) < 1e-9)
    check("전부 결측이면 None", QualityScreener._wavg(
        [{"name": "a", "score": None, "w": 10}])[0] is None)


def main():
    print("=" * 56)
    print("  Quality Screener — 점수 함수 회귀 테스트")
    print("=" * 56)
    for fn in (test_monotonicity, test_units, test_missing_vs_bad,
               test_distortion_guards, test_grade_consistency,
               test_sector_classification, test_flags,
               test_weight_renormalization):
        fn()
    print("\n" + "=" * 56)
    if FAILS:
        print(f"  ❌ 실패 {len(FAILS)}건")
        for f in FAILS:
            print(f"     - {f}")
        return 1
    print("  ✅ 전체 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
