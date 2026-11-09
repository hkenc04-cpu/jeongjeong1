#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""적정주가 순위 도구 (통합판): 미국(SEC)·한국(DART) 상장사를 공시 10개년으로 점수화해 엑셀로 저장합니다.
실행하기.bat 으로 실행하세요. 한계: 정성(텍스트) 신호와 경영진 영역은 반영하지 않습니다. 순위는 참고용입니다.
"""
import csv, gzip, io, json, os, re, sys, time, zipfile, zlib, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import date, datetime
import statistics as st

# ===== 계산 엔진 (엑셀 양식과 같은 공식, 양식 예시 데이터로 소수점 10자리까지 일치 확인) =====

AREAS = ['해자', '이익의 질', '자본배분', '재무건전성', '경영진·공시 품질']
AREA_W = [0.30, 0.20, 0.20, 0.15, 0.15]
# id: (area_idx, floor, cap)
PARAMS = {
    'M1': (0, 0, 10), 'M2': (0, 0.40, 0.10), 'M3': (0, 0.08, 0.01), 'M4': (0, 0.05, 0.20),
    'Q1': (1, 0.6, 1.1), 'Q2': (1, 0.10, 0.00), 'Q3': (1, 4, 8),
    'C1': (2, 0.5, 1.5), 'C2': (2, 0.03, -0.02), 'C3': (2, 3, 10), 'C4': (2, 0.05, 0.25),
    'F1': (3, 5, 1), 'F2': (3, 2, 12), 'F3': (3, 5, 1),
    'G1': (4, 0.0, 0.05), 'G2': (4, 0.3, 0.9), 'G3': (4, 0, 5),
}
TIERS = [  # lower bound, label, margin, wacc add, perpetual growth cap
    (0, '투자 제외', None, 0.03, 0.02), (2, '보수적 검토', 0.40, 0.02, 0.02),
    (3, '양호', 0.30, 0.01, 0.025), (4, '우수', 0.25, 0.0, 0.03)]
# 미국 2,297종목으로 잰 '최근 4개년만 쓸 때 − 10개년' 평균 점수 차이(메뉴 9, 2026-10). 짧은 자료의 점수에서 빼서 보정한다.
SHORT_BIAS = {'M2': 0.53, 'M3': 1.46, 'Q3': 0.50, 'C2': 0.37, 'C3': 0.32, 'C4': 0.56}
ASSUME = dict(rf=0.042, erp=0.05, beta=1.0, tax=0.21, kd=0.055, hurdle=0.09, maint=1.0, g_cap=0.10, gT=0.03)


def get_betas(symbols, here):
    """월간 수익률 5년으로 S&P500 대비 베타를 구해 블룸 조정(0.67×원값+0.33), 0.5~2.0으로 제한. 30일간 보관."""
    ensure_yfinance()
    import yfinance as yf
    path = os.path.join(here, 'beta_cache.json')
    try:
        cache = json.load(open(path, encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    today = date.today()
    fresh = lambda s: s in cache and (today - date.fromisoformat(cache[s][1])).days < 30  # noqa: E731
    todo = [x for x in dict.fromkeys(symbols) if not fresh(x)]
    if todo:
        print(f'\n[베타] {len(todo)}개 종목의 5년 월간 주가로 베타를 계산합니다(처음에는 수 분 걸릴 수 있습니다)...')
    for i in range(0, len(todo), 80):
        chunk = todo[i:i + 80]
        try:
            df = yf.download(chunk + ['^GSPC'], period='5y', interval='1mo', auto_adjust=True, progress=False, threads=True)['Close']
            rets = df.pct_change()
            for x in chunk:
                if x not in rets:
                    continue
                pair = rets[[x, '^GSPC']].dropna()
                if len(pair) >= 36:
                    var = pair['^GSPC'].var()
                    if var > 0:
                        raw = pair[x].cov(pair['^GSPC']) / var
                        cache[x] = [round(min(2.0, max(0.5, 0.67 * raw + 0.33)), 3), today.isoformat()]
        except Exception as e:  # noqa: BLE001
            print(f'   베타 계산 일부 실패({type(e).__name__}): 해당 종목은 베타 1.0을 씁니다.')
            if 'RateLimit' in type(e).__name__ or 'Too Many' in str(e):
                break
        time.sleep(1.0)
    json.dump(cache, open(path, 'w', encoding='utf-8'))
    return {x: cache[x][0] for x in symbols if x in cache}


def add_scenarios(r, d, mcap_m, notes, A=None, exclude=()):
    """낙관 시나리오(1~5년 성장률 상한 15%, 최소 기대수익률 8%, 할인율 1%p 인하)의 적정 시총과 정상화 메모를 결과에 붙인다."""
    if r.get('base_note'):
        notes.append(r['base_note'])
    try:
        ro = evaluate(d, mcap_m, A={**(A or {}), 'g_cap': 0.15, 'hurdle': 0.08, 'r_shift': -0.01}, exclude=exclude)
        r['fv_opt'] = ro['fv_rep']
    except Exception:  # noqa: BLE001
        r['fv_opt'] = None
    # 이익 감소 추세 점검: 최근 2년 평균 순이익이 그 앞 3년 평균의 80% 미만이거나, 2년 연속 감소하며 매출도 줄었으면 표시(순위·판정은 바꾸지 않음)
    try:
        ix = [i for i, x in enumerate(d['revenue']) if x is not None and d['net_income'][i] is not None]
        ni_ = [d['net_income'][i] for i in ix]; rv_ = [d['revenue'][i] for i in ix]
        if len(ni_) >= 6:
            rec, prv = sum(ni_[-2:]) / 2, sum(ni_[-5:-2]) / 3
            fall = prv > 0 and rec < 0.8 * prv
            twice = ni_[-1] < ni_[-2] < ni_[-3] and rv_[-1] < rv_[-3]
            if fall or twice:
                r['trend_down'] = True
                why = f'최근 2년 순이익이 그 앞 3년 평균의 {rec / prv:.0%}' if fall else '순이익 2년 연속 감소·매출 감소'
                notes.append(f'⚠ 이익 감소 추세: {why}. 과거 평균 이익으로 계산한 적정가가 앞으로의 이익보다 높을 수 있음')
    except Exception:  # noqa: BLE001
        pass
    # 이익 정점 점검: 가능한 전체 연도 평균 이익으로 다시 계산해 크게 낮아지면 경고(순위·판정은 바꾸지 않음)
    try:
        if r.get('n', 0) >= 6 and r.get('fv_rep'):
            ra = evaluate(d, mcap_m, A={**(A or {}), 'base_mode': 'all'}, exclude=exclude)
            fa = ra['fv_rep']
            if fa is None or fa < 0.7 * r['fv_rep']:
                ratio = '산정 불가(평균 이익이 0 이하)' if fa is None else f'적정 시총이 현재 기준의 {fa / r["fv_rep"]:.0%}'
                gr = r.get('cagr') or 0
                tail = f' · 매출이 연 {gr:.0%} 성장해 성장의 영향일 가능성이 큼' if gr >= 0.08 else ' · 성장세가 크지 않은데도 높다면 경기 정점일 가능성'
                notes.append(f'⚠ 이익 급증: 최근 이익이 {r["n"]}개년 평균보다 높음(전체 평균 기준 {ratio}). 경기 정점이면 적정가 과대, 지속 성장이면 타당{tail}')
            r['fv_all'] = fa
    except Exception:  # noqa: BLE001
        pass


def clean(v):
    return None if v is None else float(v)


def score(v, f, c):
    return min(5.0, max(0.0, (v - f) / (c - f) * 5))


def evaluate(d, mcap_rep, A=None, include_wc=False, mcap10=None, extra=None, exclude=()):
    """d: company dict (arrays oldest->newest, None = missing). mcap_rep: market cap in REPORTING currency, millions."""
    A = {**ASSUME, **(A or {})}
    extra = extra or {}
    idx = [i for i, x in enumerate(d['revenue']) if x is not None and d['net_income'][i] is not None]
    n = len(idx)
    g = lambda k: [clean(d[k][i]) if d.get(k) else None for i in idx]
    rev, oi, ni, da, cfo, capex = g('revenue'), g('operating_income'), g('net_income'), g('dep_amort'), g('cfo'), g('capex')
    ta, cash, debt, eq = g('total_assets'), g('cash'), g('total_debt'), g('equity')
    sh, div, bb, wc, intr = g('shares_out'), g('dividends_paid'), g('buybacks'), g('wc_increase'), g('interest_expense')
    dbt_last = debt[-1] or 0
    ke = A['rf'] + A['beta'] * A['erp']
    wacc = (mcap_rep * ke + dbt_last * A['kd'] * (1 - A['tax'])) / (mcap_rep + dbt_last)
    nopat = [None if o is None else o * (1 - A['tax']) for o in oi]
    ic = [None if None in (eq[i], debt[i], cash[i]) else eq[i] + debt[i] - cash[i] for i in range(n)]
    roic = [None if (nopat[i] is None or ic[i] is None or ic[i] <= 0) else nopat[i] / ic[i] for i in range(n)]
    ok = [r for r in roic if r is not None]
    m = {}
    if ok:
        m['M1'] = sum(1 for r in ok if r - wacc > 0) * 10 / len(ok)
        m['M4'] = sum(ok) / len(ok)
    om = [None if (oi[i] is None or not rev[i]) else oi[i] / rev[i] for i in range(n)]
    omv = [x for x in om if x is not None]
    if len(omv) >= 2:
        mean = sum(omv) / len(omv)
        m['M2'] = 9.99 if mean <= 0 else st.stdev(omv) / mean
        drops = [om[i - 1] - om[i] for i in range(1, n) if om[i] is not None and om[i - 1] is not None]
        m['M3'] = max(0.0, max(drops)) if drops else 0.0
    if all(x is not None for x in cfo) and all(x is not None for x in ni):
        m['Q1'] = 0.0 if sum(ni) <= 0 else sum(cfo) / sum(ni)
        acc = [(ni[i] - cfo[i]) / ta[i] for i in range(n) if ta[i]]
        if acc:
            m['Q2'] = sum(acc) / len(acc)
    if n >= 2:
        up = sum(1 for i in range(1, n) if ni[i] > ni[i - 1])
        m['Q3'] = up * 9 / (n - 1)
    if mcap10 and sum(ni) > 0:
        m['C1'] = (mcap_rep - mcap10 + sum(x or 0 for x in div) + sum(x or 0 for x in bb)) / sum(ni)
    shv = [x for x in sh if x is not None]
    if len(shv) >= 2 and len(shv) == n and shv[0] > 0 and 'C2' not in exclude:
        m['C2'] = (shv[-1] / shv[0]) ** (1 / (len(shv) - 1)) - 1
    pay = [(div[i] or 0) + (bb[i] or 0) for i in range(n)]
    m['C3'] = sum(1 for p in pay if p > 0) * 10 / n
    k = 3 if n >= 10 else 2
    if all(x is not None for x in nopat) and all(x is not None for x in ic):
        dn = sum(nopat[-k:]) / k - sum(nopat[:k]) / k
        di = sum(ic[-k:]) / k - sum(ic[:k]) / k
        m['C4'] = (0.5 if dn >= 0 else 0.0) if di <= 0 else dn / di
    m['F1'] = 99.0 if cfo[-1] <= 0 else (debt[-1] - cash[-1]) / cfo[-1]
    if intr[-1] is not None:
        m['F2'] = 99.0 if intr[-1] <= 0 else oi[-1] / intr[-1]
    m['F3'] = 99.0 if ni[-1] <= 0 else debt[-1] / ni[-1]
    for key in ('G1', 'G2', 'G3'):
        if key in extra:
            m[key] = extra[key]
    sc = {k_: score(v, PARAMS[k_][1], PARAMS[k_][2]) for k_, v in m.items()}
    if not A.get('no_bias') and n <= 7:   # 짧은 자료(Yahoo 4~5개년 등)는 변동·낙폭·증가 지표가 후하게 나와 미국 10개년 대비 측정한 편향만큼 깎는다
        fac = 1.0 if n <= 5 else 0.5
        for k_, b_ in SHORT_BIAS.items():
            if k_ in sc:
                sc[k_] = max(0.0, sc[k_] - b_ * fac)
    area_sc, area_cnt = {}, {}
    for ai, a in enumerate(AREAS):
        vals = [sc[k_] for k_ in sc if PARAMS[k_][0] == ai]
        area_cnt[a] = (len(vals), sum(1 for k_ in PARAMS if PARAMS[k_][0] == ai))
        area_sc[a] = sum(vals) / len(vals) if vals else None
    wsum = sum(w for w, a in zip(AREA_W, AREAS) if area_sc[a] is not None)
    comp = sum(w * area_sc[a] for w, a in zip(AREA_W, AREAS) if area_sc[a] is not None) / wsum
    ti = max(i for i, t in enumerate(TIERS) if comp >= t[0])
    tier = TIERS[ti]
    # gates
    streak = 0
    for r in roic:
        streak = streak + 1 if (r is not None and r - wacc < 0) else 0
    gates = []
    if streak >= 5:
        gates.append(f'ROIC<WACC {streak}년 연속')
    if m['F1'] >= 6:
        gates.append('순차입금/영업CF ≥ 6배')
    # valuation
    oe = []
    for i in range(n):
        d_i, c_i = (da[i] or 0), (capex[i] or 0)
        if d_i <= 0:
            v = ni[i]                      # 감가상각 자료가 없으면 '유지보수 투자 ≈ 감가상각'으로 보고 순이익을 오너이익으로 사용
        else:
            maint = c_i if c_i <= d_i else d_i + 0.5 * (c_i - d_i)   # 유지보수 투자 = 감가상각 + 초과분의 50%(나머지는 성장투자)
            v = ni[i] + d_i - maint
        v -= ((wc[i] or 0) if include_wc else 0)
        oe.append(v)
    base = sum(oe[-3:]) / 3
    base_note = None
    bm = A.get('base_mode')
    if bm == 'min35' and n >= 5:        # 백테스트 비교용: 3년·5년 평균 중 항상 낮은 값
        base = min(base, sum(oe[-5:]) / 5)
    elif bm == 'all':                   # 백테스트 비교용: 가능한 전체 연도 평균
        base = sum(oe) / n
    elif bm is None and n >= 5:   # 이익 변동이 큰 경기순환형은 5년 평균과 3년 평균 중 낮은 값(정상화)으로 보수적으로 평가
        w5 = oe[-5:]
        m5 = sum(w5) / 5
        if m5 != 0 and st.pstdev(w5) / abs(m5) > 0.5:
            base_note = '오너이익 변동 큼(변동계수>0.5) → 5년·3년 평균 중 낮은 값 사용'
            base = min(base, m5)
    cagr = (rev[-1] / rev[0]) ** (1 / (n - 1)) - 1 if n >= 2 and rev[0] and rev[0] > 0 else 0.0
    g1 = A.get('g1_override', min(A['g_cap'], max(0.0, cagr)))
    g1 = min(g1, A['g_cap'])
    gT = min(A['gT'], tier[4])
    r = max(wacc, A['hurdle']) + tier[3] + A.get('r_shift', 0.0)
    fv, irr, path = None, None, None
    if base > 0:
        path, cur = [], base
        for t in range(1, 11):
            gt = g1 if t <= 5 else g1 + (gT - g1) * (t - 5) / 5
            cur = cur * (1 + gt)
            path.append(cur)

    def val(rr):
        pv_ = sum(x / (1 + rr) ** (t + 1) for t, x in enumerate(path))
        return pv_ + path[-1] * (1 + gT) / (rr - gT) / (1 + rr) ** 10
    if path:
        if r > gT:
            fv = val(r)
        lo, hi = gT + 0.002, 0.60      # 현재 시총을 정확히 만드는 할인율 = 현재가로 샀을 때의 내재 수익률
        if val(hi) >= mcap_rep:
            irr = hi
        elif val(lo) <= mcap_rep:
            irr = lo
        else:
            for _ in range(60):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if val(mid) > mcap_rep else (lo, mid)
            irr = (lo + hi) / 2
    return dict(n=n, metrics=m, scores=sc, area=area_sc, area_cnt=area_cnt, composite=comp, tier=tier, gates=gates,
                wacc=wacc, beta=A['beta'], r=r, g1=g1, gT=gT, base_oe=base, fv_rep=fv, irr=irr, base_note=base_note, roic=roic, om=om, cagr=cagr,
                coverage=len(sc) / len(PARAMS))


# ===== 수집·순위표 =====


TAGS = {
    'revenue': ('USD', ['Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax',
                        'RevenueFromContractWithCustomerIncludingAssessedTax', 'SalesRevenueNet', 'SalesRevenueGoodsNet']),
    'gross_profit': ('USD', ['GrossProfit']),
    'operating_income': ('USD', ['OperatingIncomeLoss']),
    'net_income': ('USD', ['NetIncomeLoss', 'NetIncomeLossAvailableToCommonStockholdersBasic', 'ProfitLoss']),
    'dep_amort': ('USD', ['DepreciationDepletionAndAmortization', 'DepreciationAndAmortization',
                          'DepreciationAmortizationAndAccretionNet', 'Depreciation']),
    'cfo': ('USD', ['NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations']),
    'capex': ('USD', ['PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets',
                      'PaymentsForCapitalImprovements', 'PaymentsToAcquireOtherPropertyPlantAndEquipment']),
    'costs_expenses': ('USD', ['CostsAndExpenses']),
    'pretax': ('USD', ['IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
                       'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments']),
    'total_assets': ('USD', ['Assets']),
    'cash': ('USD', ['CashAndCashEquivalentsAtCarryingValue', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents', 'Cash']),
    'total_debt': ('USD', ['DebtLongtermAndShorttermCombinedAmount', 'LongTermDebt', 'DebtInstrumentCarryingAmount', 'LongTermDebtNoncurrent']),
    'equity': ('USD', ['StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest']),
    'interest_expense': ('USD', ['InterestExpense', 'InterestExpenseDebt', 'InterestAndDebtExpense', 'InterestExpenseNonoperating']),
    'shares_out': ('shares', ['CommonStockSharesOutstanding', 'WeightedAverageNumberOfSharesOutstandingBasic', 'CommonStockSharesIssued']),
    'dividends_paid': ('USD', ['PaymentsOfDividends', 'PaymentsOfDividendsCommonStock']),
    'buybacks': ('USD', ['PaymentsForRepurchaseOfCommonStock']),
}
FIN_TAGS = ['Deposits', 'PremiumsEarnedNet', 'InterestAndDividendIncomeOperating', 'LiabilityForFuturePolicyBenefits']
REQUIRED = ['revenue', 'net_income', 'cfo', 'total_assets', 'equity']


class SecError(Exception):
    pass


_variant = [None]   # 한 번 성공한 접속 방식을 기억
BROWSER = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'


def _decode(raw, enc):
    enc = (enc or '').lower()
    try:
        if 'gzip' in enc or raw[:2] == b'\x1f\x8b':
            return gzip.decompress(raw)
        if 'deflate' in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw


def _urllib_get(url, ua):
    h = {'User-Agent': ua, 'Accept-Encoding': 'gzip, deflate', 'Accept': 'application/json,text/plain,*/*',
         'Accept-Language': 'en-US,en;q=0.9', 'Connection': 'close'}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
            return r.status, _decode(r.read(), r.headers.get('Content-Encoding'))
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read(), e.headers.get('Content-Encoding'))


def _curl_get(url, ua):
    # Windows 10/11에 들어있는 curl.exe는 파이썬과 다른 방식으로 접속해서 통과되는 경우가 있다.
    import subprocess
    p = subprocess.run(['curl', '-sS', '-L', '--compressed', '-A', ua, '-H', 'Accept: application/json',
                        '-w', '\n%{http_code}', '--max-time', '90', url], capture_output=True, timeout=120)
    if p.returncode != 0:
        raise OSError(p.stderr.decode('utf-8', 'ignore')[:200])
    body, _, code = p.stdout.rpartition(b'\n')
    return int(code or 0), body


def _ps_get(url, ua):
    import subprocess
    cmd = ("$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
           f"try{{(Invoke-WebRequest -UseBasicParsing -Uri '{url}' -UserAgent '{ua}').Content}}"
           "catch{Write-Output ('HTTPFAIL ' + [int]$_.Exception.Response.StatusCode)}")
    p = subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True, timeout=180)
    out = p.stdout.decode('utf-8', 'ignore').strip()
    if out.startswith('HTTPFAIL') or not out:
        return int(out.split()[-1]) if out.startswith('HTTPFAIL') and out.split()[-1].isdigit() else 0, out.encode()
    return 200, out.encode('utf-8')


def _methods(ua):
    return [
        ('python/기본', lambda u: _urllib_get(u, ua)),
        ('python/Mozilla형', lambda u: _urllib_get(u, f'Mozilla/5.0 (compatible; {ua})')),
        ('curl.exe', lambda u: _curl_get(u, ua)),
        ('curl.exe/브라우저형', lambda u: _curl_get(u, f'{BROWSER} {ua}')),
        ('PowerShell', lambda u: _ps_get(u, ua)),
    ]


def _brief(body):
    t = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body[:4000].decode('utf-8', 'ignore'))).strip()
    return ''.join(c for c in t if c.isprintable())[:250]


def http_json(url, ua, raw=False):
    ms = _methods(ua)
    order = list(range(len(ms)))
    if _variant[0] is not None:
        order.remove(_variant[0]); order.insert(0, _variant[0])
    tried = []
    for i in order:
        name, fn = ms[i]
        for attempt in range(3):
            try:
                code, body = fn(url)
            except (OSError, ValueError) + subprocess_errors() as e:
                if attempt < 2 and isinstance(e, (TimeoutError, ConnectionError, urllib.error.URLError)):
                    time.sleep(5 * (attempt + 1)); continue   # 일시적 연결 끊김은 잠시 쉬고 재시도
                tried.append(f'[{name}] 실행 불가: {str(e)[:120]}'); break
            if code == 200:
                if raw:
                    _variant[0] = i
                    return body
                try:
                    data = json.loads(body.decode('utf-8'))
                except ValueError:
                    tried.append(f'[{name}] 200 이지만 JSON이 아님: {_brief(body)}'); break
                _variant[0] = i
                return data
            if code in (429, 503) and attempt < 2:
                time.sleep(10 * (attempt + 1)); continue
            tried.append(f'[{name}] HTTP {code} / 서버 응답: {_brief(body)}')
            if 'Rate Threshold' in _brief(body) or 'Rate Threshold' in body.decode('utf-8', 'ignore')[:3000]:
                raise SecError('RATE')
            if code not in (403, 0):
                raise SecError('\n'.join(tried))
            break
    raise SecError('\n'.join(tried))


def subprocess_errors():
    import subprocess
    return (subprocess.SubprocessError, FileNotFoundError)


def cached(path, url, ua, delay=0.35):
    if os.path.exists(path):
        return json.load(open(path, encoding='utf-8'))
    data = http_json(url, ua)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(data, open(path, 'w', encoding='utf-8'))
    time.sleep(delay)  # SEC: 초당 10건 이하
    return data


def get_facts(taxo, key, unit):
    c = taxo.get('us-gaap', {}).get(key)
    return [] if c is None else c.get('units', {}).get(unit, [])


def normalize(facts):
    out = []
    for f in facts:
        if f.get('form') not in ('10-K', '10-K/A', '10-KT'):
            continue
        end = datetime.strptime(f['end'], '%Y-%m-%d').date()
        st = datetime.strptime(f['start'], '%Y-%m-%d').date() if f.get('start') else None
        if st is not None and not (340 <= (end - st).days <= 380):
            continue
        out.append((end, f['val'], datetime.strptime(f.get('filed', '1900-01-01'), '%Y-%m-%d').date()))
    return out


def value_for_year(data, yr):
    c = [r for r in data if r[0].year == yr]
    if not c:
        return None
    mx = max(r[2] for r in c)
    return [r for r in c if r[2] == mx][0][1]


def best_generic(taxo, subs, unit='USD', bad=('Deferred', 'Unearned', 'Cost', 'Expense', 'Percent', 'Gain', 'Tax', 'Receivable', 'Remaining', 'Segment', 'Lease')):
    best, bkey = None, None
    for tag in taxo.get('us-gaap', {}):
        if any(x in tag for x in subs) and not any(b in tag for b in bad):
            d = normalize(get_facts(taxo, tag, unit))
            if not d:
                continue
            last = max(r[0] for r in d)
            lv = max(abs(r[1]) for r in d if r[0] == last)
            key = (last, lv)
            if bkey is None or key > bkey:
                best, bkey = tag, key
    return best


def extract(facts_json, n_years=10):
    taxo = facts_json.get('facts', {})
    if 'us-gaap' not in taxo:
        return None, 'us-gaap 공시 없음(외국 기업 20-F 등)'
    if any(t in taxo['us-gaap'] for t in FIN_TAGS):
        return None, '금융업으로 추정(이 모델은 비금융 기업용)'
    series, used = {}, {}
    for key, (unit, tags) in TAGS.items():
        cand = [(t, normalize(get_facts(taxo, t, unit))) for t in tags]
        series[key] = cand
    for gk, subs in (('revenue', ('Revenue',)), ('net_income', ('NetIncomeLoss', 'ProfitLoss'))):
        if not any(value_for_year(d_, y_) is not None for _, d_ in series[gk] for y_ in range(date.today().year - 3, date.today().year + 1)):
            g_tag = best_generic(taxo, subs)
            if g_tag:
                series[gk].append((g_tag, normalize(get_facts(taxo, g_tag, 'USD'))))
    years_with = [y for y in range(1990, date.today().year + 1)
                  if all(any(value_for_year(d, y) is not None for _, d in series[k]) for k in ('revenue', 'net_income'))]
    if not years_with:
        return None, 'USD 기준 매출·순이익 연간 데이터 없음'
    last = max(years_with)
    yrs = list(range(last - n_years + 1, last + 1))
    out = {'years': yrs}
    for key, (unit, tags) in TAGS.items():
        vals, tagused = [], set()
        for y in yrs:
            pick = None
            for t, d in series[key]:
                v = value_for_year(d, y)
                if v is not None:
                    pick = (t, v)
                    break
            vals.append(None if pick is None else pick[1] / 1e6)
            if pick:
                tagused.add(pick[0])
        out[key] = vals
        used[key] = sorted(tagused)
    return (out, used), None


def check_and_fill(d, used, min_years=5):
    """데이터 충분성 검사 + 투명한 보정. 보정 내역은 note로 남긴다."""
    notes = []
    for k in REQUIRED:
        if sum(1 for x in d[k] if x is not None) < min_years:
            return None, [f'필수 항목 {min_years}년 미만: {k}']
    if d['cfo'][-1] is None:
        return None, ['최신 연도 영업현금흐름 없음']
    if all(x is None for x in d['total_debt']):
        d['total_debt'] = [0.0] * 10
        notes.append('차입금 태그 없음 → 0(무차입) 가정')
    else:
        d['total_debt'] = [x if x is not None else 0.0 for x in d['total_debt']]
    if all(x is None for x in d['operating_income']):
        ce, pt, ie = d.get('costs_expenses'), d.get('pretax'), d.get('interest_expense')
        if ce and any(x is not None for x in ce):
            d['operating_income'] = [None if (a is None or b is None) else a - b for a, b in zip(d['revenue'], ce)]
            notes.append('영업이익 태그 없음 → 매출 − 총비용(CostsAndExpenses)으로 대체')
        elif pt and any(x is not None for x in pt):
            d['operating_income'] = [None if a is None else a + (b or 0) for a, b in zip(pt, ie or [None] * 10)]
            notes.append('영업이익 태그 없음 → 세전이익 + 이자비용으로 근사')
        else:
            return None, ['영업이익 태그 없음(대체 계산 불가)']
        if all(x is None for x in d['operating_income']):
            return None, ['영업이익 태그 없음(대체 계산 불가)']
    labels = {'capex': '설비투자', 'dividends_paid': '배당', 'buybacks': '자사주 매입', 'dep_amort': '감가상각'}
    for k, lab in labels.items():
        miss = sum(1 for x in d[k] if x is None)
        if miss == 10:
            notes.append(f'{lab} 태그 없음 → 전 기간 0 처리' + (' (오너이익 과대 가능)' if k == 'capex' else ' (오너이익은 순이익으로 대체)' if k == 'dep_amort' else ''))
        elif miss:
            notes.append(f'{lab} {miss}개 연도 누락 → 0 처리')
        d[k] = [x if x is not None else 0.0 for x in d[k]]
    return d, notes


def split_jump(shares):
    v = [x for x in shares if x]
    for a, b in zip(v, v[1:]):
        r = b / a
        if r >= 1.9 or r <= 0.52:
            return True
    return False


def read_csv(path):
    rows = list(csv.reader(open(path, encoding='utf-8-sig')))
    hdr = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(hdr):
                if n in h:
                    return i
        return None
    ci_name, ci_sym, ci_cap = col('name'), col('symbol', 'ticker'), col('marketcap', 'market cap')
    ci_px = col('price')
    if None in (ci_name, ci_sym, ci_cap):
        sys.exit(f'CSV 열을 찾지 못했습니다. 헤더: {rows[0]}')
    out = []
    for r in rows[1:]:
        try:
            cap = float(str(r[ci_cap]).replace(',', '').replace('$', ''))
        except ValueError:
            continue
        px = None
        if ci_px is not None:
            try:
                px = float(str(r[ci_px]).replace(',', '').replace('$', '')) or None
            except (ValueError, IndexError):
                px = None
        import html as _html
        out.append((r[ci_sym].strip().upper(), _html.unescape(r[ci_name]).strip(), cap, px))
    return out


def to_millions(cap):
    # 시총 표(companiesmarketcap)는 달러 단위. 100만 이상이면 달러로 보고 백만달러로 환산, 그 미만이면 이미 백만 단위로 간주.
    return cap / 1e6 if cap >= 1e6 else cap


def sanity_split(ranked, excluded):
    """적정 시총이 현재 시총의 20배를 넘는 종목은 통화·단위 오류나 극단적 소형주일 가능성이 커서 순위에서 빼고 사유를 남긴다."""
    ok, ex = [], list(excluded)
    for r in ranked:
        fv, mc = r.get('fv_usd'), r.get('mcap')
        if fv and mc and fv / mc > 20:
            ex.append((r['ticker'], r['name'], f'자료 이상 의심: 적정 시총이 현재 시총의 {fv / mc:,.0f}배(통화·단위 오류 또는 극단적 소형주) → 순위에서 제외'))
        else:
            ok.append(r)
    return ok, ex


def write_xlsx(path, ranked, excluded, asof, title=None, scope_note=None, limits=None, fx=1.0, cur='$'):
    ranked, excluded = sanity_split(ranked, excluded)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter as L
    FN = 'Arial'
    hdr = PatternFill('solid', fgColor='1F3864'); sec = PatternFill('solid', fgColor='D9E1F2')
    thin = Side(style='thin', color='BFBFBF'); bd = Border(left=thin, right=thin, top=thin, bottom=thin)

    def cell(ws, ref, v, color='000000', bold=False, fmt=None, wrap=False, align=None, fill=None):
        c = ws[ref]; c.value = v; c.font = Font(name=FN, size=10, bold=bold, color=color)
        if fmt: c.number_format = fmt
        c.alignment = Alignment(wrap_text=wrap, vertical='center', horizontal=align)
        if fill: c.fill = fill
        c.border = bd
        return c

    def head(ws, row, labels):
        for i, t in enumerate(labels):
            c = ws.cell(row=row, column=1 + i, value=t)
            c.font = Font(name=FN, size=10, bold=True, color='FFFFFF'); c.fill = hdr; c.border = bd
            c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    wb = Workbook(); ws = wb.active; ws.title = '순위표'
    nf = sum(1 for _, _, why in excluded if ('SEC 미등록' in why or '20-F' in why))
    ws['A1'] = (title or f'시총 상위 종목 중 미국 SEC 10-K 공시 기업 모델 순위 ({asof} 산출, SEC 10개년)')
    ws['A3'] = scope_note or (f'※ 이 표는 세계 전체가 아니라 미국 SEC에 10-K를 제출하는 기업만 대상입니다. 조회 {len(ranked) + len(excluded)}종목 중 순위 산출 {len(ranked)}종목, '
                f'미국 외 상장·외국 기업 {nf}종목, 금융업·데이터 부족 등 기타 {len(excluded) - nf}종목은 제외했습니다. (제외 사유는 "제외·데이터부족" 시트)')
    ws['A3'].font = Font(name=FN, size=10, bold=True, color='1F3864'); ws.merge_cells('A3:Q3'); ws['A1'].font = Font(name=FN, size=14, bold=True)
    ws['A2'] = ('⚠ 참고용: 이 순위는 "사업 질·재무 위험 점검표" 순서입니다(백테스트에서 점수는 수익률 예측력이 확인되지 않았습니다). 가격 판단은 [괴리율순위]·내재 수익률을 참고하세요. 정성(텍스트)·경영진 영역 미반영. 파란 글씨 = 입력값, 검정 = 수식. 자세한 한계는 "가정·한계" 시트.')
    ws['A2'].font = Font(name=FN, size=9, italic=True, color='C00000'); ws.merge_cells('A2:Q2')
    for i, (nm, w) in enumerate([('해자', .30), ('이익의 질', .20), ('자본배분', .20), ('재무건전성', .15)]):
        cell(ws, f'{L(5 + i)}5', nm, bold=True, align='center', fill=sec); cell(ws, f'{L(5 + i)}6', w, '0000FF', fmt='0%', align='center')
    cell(ws, 'A6', '가중치(경영진 영역 제외 후 재정규화)', bold=True)
    for i, (lo, nm, mg) in enumerate([(0, '투자 제외', 0), (2, '보수적 검토', .40), (3, '양호', .30), (4, '우수', .25)]):
        cell(ws, f'K{5 + i}', lo, '0000FF', fmt='0.0', align='center'); cell(ws, f'L{5 + i}', nm, '0000FF', align='center')
        cell(ws, f'M{5 + i}', mg, '0000FF', fmt='0%', align='center')
    cols = ['순위', '티커', '종목', '종합점수', '등급', '해자', '이익의 질', '자본배분', '재무건전성', '적정 시총 ($B)', '현재 시총 ($B)',
            '괴리율', '안전마진', '매수 기준 시총 ($B)', '판정', '게이트 경고', '데이터 연수 / 지표 수',
            f'현재가 ({cur})', f'적정주가 ({cur})', f'매수 기준가 ({cur})', '적정주가 괴리율', '주식 수 (백만주)', '내재 수익률 (현재가로 매수 시 연)', '낙관 적정 시총 ($B)', f'낙관 적정주가 ({cur})', '낙관 괴리율', '적용 베타']
    # 컬럼 재배치: 점수 4개는 F~I
    head(ws, 11, cols)
    r0 = 12
    for i, r in enumerate(ranked):
        row = r0 + i; a = r['area']
        cell(ws, f'A{row}', f'=RANK(D{row},$D${r0}:$D${r0 + len(ranked) - 1},0)', fmt='0', align='center')
        cell(ws, f'B{row}', r['ticker'], bold=True, align='center'); cell(ws, f'C{row}', r['name'])
        cell(ws, f'D{row}', f'=SUMPRODUCT($E$6:$H$6,F{row}:I{row})/SUM($E$6:$H$6)', fmt='0.00', bold=True, align='center')
        cell(ws, f'E{row}', f'=LOOKUP(D{row},$K$5:$K$8,$L$5:$L$8)', align='center')
        for j, ak in enumerate(['해자', '이익의 질', '자본배분', '재무건전성']):
            cell(ws, f'{L(6 + j)}{row}', a[ak], '0000FF', fmt='0.00', align='center')
        cell(ws, f'J{row}', r['fv_usd'] / 1000 if r['fv_usd'] is not None else '산정 불가', '0000FF', fmt='#,##0', align='center')
        cell(ws, f'K{row}', r['mcap'] / 1000, '0000FF', fmt='#,##0', align='center')
        cell(ws, f'L{row}', f'=IF(ISNUMBER(J{row}),J{row}/K{row}-1,"-")', fmt='0%;-0%;0%', align='center')
        cell(ws, f'M{row}', f'=IF(D{row}<$K$6,"-",LOOKUP(D{row},$K$5:$K$8,$M$5:$M$8))', fmt='0%', align='center')
        cell(ws, f'N{row}', f'=IF(AND(ISNUMBER(J{row}),ISNUMBER(M{row})),J{row}*(1-M{row}),"-")', fmt='#,##0', align='center')
        cell(ws, f'O{row}', f'=IF(D{row}<$K$6,"투자 제외",IF(NOT(ISNUMBER(J{row})),"산정 불가",IF(K{row}<=N{row},"매수 기준 이하","매수 기준 초과")))', bold=True, align='center')
        cell(ws, f'P{row}', '; '.join(r['gates']) or '-', wrap=True)
        cell(ws, f'Q{row}', f"{r['n']}년 / {round(r['coverage'] * 17)}개", '0000FF', align='center')
        cell(ws, f'V{row}', r['shares_m'] if r.get('shares_m') else 'n/a', '0000FF', fmt='#,##0.0', align='center')
        mul = '*$T$4' if fx != 1 else ''
        pf = '#,##0' if fx != 1 else '#,##0.00'
        cell(ws, f'R{row}', f'=IF(ISNUMBER(V{row}),K{row}*1000/V{row}{mul},"-")', fmt=pf, bold=True, align='center')
        cell(ws, f'S{row}', f'=IF(AND(ISNUMBER(J{row}),ISNUMBER(V{row})),J{row}*1000/V{row}{mul},"-")', fmt=pf, bold=True, align='center')
        cell(ws, f'T{row}', f'=IF(AND(ISNUMBER(N{row}),ISNUMBER(V{row})),N{row}*1000/V{row}{mul},"-")', fmt=pf, align='center')
        cell(ws, f'U{row}', f'=IF(AND(ISNUMBER(S{row}),ISNUMBER(R{row})),S{row}/R{row}-1,"-")', fmt='0%;-0%;0%', align='center')
        cell(ws, f'W{row}', r['irr'] if r.get('irr') is not None else '산정 불가', '0000FF', fmt='0.0%', bold=True, align='center')
        fo = r.get('fv_opt')
        cell(ws, f'X{row}', fo / 1000 if fo is not None else '산정 불가', '0000FF', fmt='#,##0', align='center')
        cell(ws, f'Y{row}', f'=IF(AND(ISNUMBER(X{row}),ISNUMBER(V{row})),X{row}*1000/V{row}{mul},"-")', fmt=pf, align='center')
        cell(ws, f'Z{row}', f'=IF(AND(ISNUMBER(Y{row}),ISNUMBER(R{row})),Y{row}/R{row}-1,"-")', fmt='0%;-0%;0%', align='center')
        cell(ws, f'AA{row}', r.get('beta', 1.0), '0000FF', fmt='0.00', align='center')
    if fx != 1:
        cell(ws, 'S4', '환율(원/$)', bold=True, align='center', fill=sec); cell(ws, 'T4', fx, '0000FF', fmt='#,##0.0', align='center')
        cell(ws, 'S5', '가격(R~T열)은 시총(USD)×환율로 원화 환산', align='left')
    ws.freeze_panes = 'D12'
    for k, w in {'A': 7, 'B': 9, 'C': 28, 'D': 10, 'E': 12, 'F': 9, 'G': 10, 'H': 10, 'I': 11, 'J': 14, 'K': 14, 'L': 10, 'M': 10, 'N': 14, 'O': 16, 'P': 30, 'Q': 14, 'R': 12, 'S': 14, 'T': 14, 'U': 14, 'V': 14, 'W': 16, 'X': 14, 'Y': 14, 'Z': 12, 'AA': 10}.items():
        ws.column_dimensions[k].width = w

    # ---- 매수 근접도 시트: 현재가 ÷ 매수 기준가 가 1에 가까울수록(또는 낮을수록) 매수 구간에 가깝다 ----
    wn = wb.create_sheet('매수근접도', 1)
    BANDS = [(1.00, '① 매수 구간 (현재가 ≤ 매수 기준가)', 'C6EFCE'), (1.10, '② 매우 근접 (기준가의 100~110%)', 'E2F0D9'),
             (1.25, '③ 근접 (110~125%)', 'FFF2CC'), (1.50, '④ 다소 멂 (125~150%)', 'FCE4D6'), (1e18, '⑤ 멂 (150% 초과)', 'F8CBAD')]
    SPECIAL = ['⑥ 투자 제외 (점수 2 미만)', '⑦ 적정주가 산정 불가']
    items = []
    for i, r in enumerate(ranked):
        sm = r['shares_m']; mg = r['tier'][2]
        if r['fv_usd'] is None or not sm:
            items.append((2, 0.0, i, r)); continue
        if mg is None:
            items.append((1, 0.0, i, r)); continue
        ratio = (r['mcap'] / sm) / (r['fv_usd'] / sm * (1 - mg))
        items.append((0, ratio, i, r))
    items.sort(key=lambda t: (t[0], t[1]))
    wn['A1'] = f'매수 기준가 근접도 분류 ({asof})'; wn['A1'].font = Font(name=FN, size=14, bold=True)
    wn['A2'] = ('근접도 = 현재가 ÷ 매수 기준가 (100% 이하면 매수 구간, 숫자가 작을수록 더 싸다). '
                '"하락 필요율" = 매수 기준가에 닿기까지 더 내려야 하는 비율. 참고용이며 투자 권유가 아닙니다.')
    wn['A2'].font = Font(name=FN, size=9, italic=True, color='C00000'); wn.merge_cells('A2:J2')
    head(wn, 4, ['구간', '종목 수'])
    last = 8 + len(items) + 8
    names = [b[1] for b in BANDS] + SPECIAL
    for k, nm in enumerate(names):
        fl = PatternFill('solid', fgColor=(BANDS[k][2] if k < len(BANDS) else 'D9D9D9'))
        cell(wn, f'A{5 + k}', nm, fill=fl); cell(wn, f'B{5 + k}', f'=COUNTIF($A$17:$A${last},A{5 + k})', fmt='0', align='center')
    head(wn, 16, ['구간', '티커', '종목', '종합점수', '등급', f'현재가 ({cur})', f'매수 기준가 ({cur})', f'적정주가 ({cur})', '근접도 (현재가÷기준가)', '하락 필요율'])
    for k, (g, ratio, i, r) in enumerate(items):
        row = 17 + k; sr = 12 + i  # 순위표의 해당 행
        if g == 0:
            bi = next(j for j, b in enumerate(BANDS) if ratio <= b[0] + 1e-12)
            lab, fl = BANDS[bi][1], PatternFill('solid', fgColor=BANDS[bi][2])
        else:
            lab, fl = SPECIAL[g - 1], PatternFill('solid', fgColor='D9D9D9')
        cell(wn, f'A{row}', lab, fill=fl); cell(wn, f'B{row}', f"='순위표'!B{sr}", bold=True, align='center')
        cell(wn, f'C{row}', f"='순위표'!C{sr}"); cell(wn, f'D{row}', f"='순위표'!D{sr}", fmt='0.00', align='center')
        cell(wn, f'E{row}', f"='순위표'!E{sr}", align='center')
        cell(wn, f'F{row}', f"='순위표'!R{sr}", fmt='#,##0.00', align='center')
        cell(wn, f'G{row}', f"='순위표'!T{sr}", fmt='#,##0.00', align='center')
        cell(wn, f'H{row}', f"='순위표'!S{sr}", fmt='#,##0.00', align='center')
        cell(wn, f'I{row}', f'=IF(AND(ISNUMBER(F{row}),ISNUMBER(G{row})),F{row}/G{row},"-")', fmt='0%', bold=True, align='center')
        cell(wn, f'J{row}', f'=IF(ISNUMBER(I{row}),MAX(0,1-1/I{row}),"-")', fmt='0%', align='center')
    for kk, w in {'A': 38, 'B': 10, 'C': 28, 'D': 10, 'E': 12, 'F': 13, 'G': 15, 'H': 13, 'I': 20, 'J': 13}.items():
        wn.column_dimensions[kk].width = w
    wn.freeze_panes = 'A17'

    # ---- 괴리율 순위 시트: (적정주가 ÷ 현재가 − 1) 큰 순 = 저평가가 큰 순 ----
    wg = wb.create_sheet('괴리율순위', 2)
    gl = []
    for i, r in enumerate(ranked):
        sm = r.get('shares_m')
        if r['fv_usd'] is None or not sm:
            gl.append((1, 0.0, i, r))
        else:
            gl.append((0, -(r['fv_usd'] / r['mcap'] - 1), i, r))
    gl.sort(key=lambda t: (t[0], t[1]))
    wg['A1'] = f'적정주가 괴리율 순위 ({asof})'; wg['A1'].font = Font(name=FN, size=14, bold=True)
    wg['A2'] = ('괴리율 = 적정주가 ÷ 현재가 − 1.  +값이면 현재가가 적정주가보다 싸다(저평가), −값이면 비싸다. '
                '괴리율만 크고 종합점수가 낮은 종목은 "투자 제외·보수적 검토" 등급과 게이트 경고를 반드시 함께 확인하세요. 참고용이며 투자 권유가 아닙니다.')
    wg['A2'].font = Font(name=FN, size=9, italic=True, color='C00000'); wg.merge_cells('A2:K2')
    head(wg, 4, ['괴리율 순위', '티커', '종목', f'현재가 ({cur})', f'적정주가 ({cur})', '괴리율', '종합점수', '등급', f'매수 기준가 ({cur})', '판정', '게이트 경고', '내재 수익률'])
    n_g = len(gl)
    for k, (g, _, i, r) in enumerate(gl):
        row = 5 + k; sr = 12 + i
        cell(wg, f'A{row}', f'=IF(ISNUMBER(F{row}),RANK(F{row},$F$5:$F${4 + n_g},0),"-")', fmt='0', align='center')
        cell(wg, f'B{row}', f"='순위표'!B{sr}", bold=True, align='center'); cell(wg, f'C{row}', f"='순위표'!C{sr}")
        cell(wg, f'D{row}', f"='순위표'!R{sr}", fmt='#,##0.00', align='center')
        cell(wg, f'E{row}', f"='순위표'!S{sr}", fmt='#,##0.00', align='center')
        cell(wg, f'F{row}', f"='순위표'!U{sr}", fmt='0%;-0%;0%', bold=True, align='center')
        cell(wg, f'G{row}', f"='순위표'!D{sr}", fmt='0.00', align='center'); cell(wg, f'H{row}', f"='순위표'!E{sr}", align='center')
        cell(wg, f'I{row}', f"='순위표'!T{sr}", fmt='#,##0.00', align='center'); cell(wg, f'J{row}', f"='순위표'!O{sr}", align='center')
        cell(wg, f'K{row}', f"='순위표'!P{sr}", wrap=True)
        cell(wg, f'L{row}', f"='순위표'!W{sr}", fmt='0.0%', align='center')
    for kk, w in {'A': 11, 'B': 10, 'C': 28, 'D': 13, 'E': 14, 'F': 11, 'G': 10, 'H': 12, 'I': 15, 'J': 16, 'K': 34, 'L': 13}.items():
        wg.column_dimensions[kk].width = w
    wg.freeze_panes = 'D5'

    # ---- 핵심 후보 시트: 신뢰할 만한 자료(8년 이상)·시총 $1B 이상·우수/양호·게이트 없음·매수 기준 이하 ----
    wk = wb.create_sheet('핵심후보', 1)
    kc = []
    for i, r in enumerate(ranked):
        mg = r['tier'][2]
        if (r['tier'][1] in ('우수', '양호') and mg is not None and r['fv_usd'] and r['mcap'] >= 1000 and r['n'] >= 8
                and not r['gates'] and r['mcap'] <= r['fv_usd'] * (1 - mg)):
            fa = r.get('fv_all')
            growth = (r.get('cagr') or 0) >= 0.08          # 매출이 빠르게 큰 회사는 평균 이익이 낮은 것이 정점이 아니라 성장 때문
            peak = (('fv_all' in r) and (fa is None or fa < 0.7 * r['fv_usd']) and not growth) or bool(r.get('trend_down'))
            dual = bool(fa) and r['mcap'] <= fa * (1 - mg)
            boe = r.get('base_oe'); lowm = bool(boe and boe > 0 and r['mcap'] / boe < 8)
            grp = 2 if peak else (3 if lowm else (0 if dual else 1))
            kc.append((grp, -(r['fv_usd'] / r['mcap'] - 1), i, r, fa, peak, dual))
    kc.sort(key=lambda t: (t[0], t[1]))
    try:
        GLJ = ['A 이중 통과', 'B 최근 이익 기준만', 'C 정점·쇠퇴 의심', 'D 시장 불신(8배 미만)']
        json.dump([dict(grp=GLJ[t[0]], ticker=t[3]['ticker'], name=t[3]['name'], gap=t[3]['fv_usd'] / t[3]['mcap'] - 1,
                        gap_avg=(t[4] / t[3]['mcap'] - 1) if t[4] else None, score=t[3]['composite'], tier=t[3]['tier'][1],
                        mult=(t[3]['mcap'] / t[3]['base_oe']) if t[3].get('base_oe') and t[3]['base_oe'] > 0 else None,
                        trend_down=bool(t[3].get('trend_down')), n=t[3]['n']) for t in kc],
                  open(os.path.splitext(path)[0] + '_핵심후보.json', 'w', encoding='utf-8'), ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    n_dual = sum(1 for t in kc if t[0] == 0)
    wk['A1'] = f'핵심 후보 ({asof}) — 자료 8년 이상 · 시총 $1B 이상 · 우수/양호 · 게이트 경고 없음 · 현재가가 매수 기준가 이하'
    wk['A1'].font = Font(name=FN, size=13, bold=True)
    wk['A2'] = (f'{len(kc)}종목 중 [A 이중 통과] {n_dual}종목. 이중 통과 = 최근 3년 이익 기준에서도, 전 기간 평균 이익 기준에서도 매수 기준 이하(이익이 정점이어서 싸 보이는 착시 가능성이 낮음). '
                '[B 최근 이익 기준만 통과]는 평균 이익으로 보면 비싸므로 경기 정점 여부를 확인하세요. [C 정점·쇠퇴 의심]은 (성장주가 아닌데) 전체 평균 적정가가 현재 기준의 70% 미만이거나 순이익이 감소 추세인 종목, [D 시장 불신]은 시총이 오너이익의 8배 미만인 종목(시장이 쇠퇴·감익을 가격에 반영 중일 가능성)입니다. '
                '먼저 사업보고서를 읽어볼 목록이지 매수 목록이 아닙니다. 참고용이며 투자 권유가 아닙니다.')
    wk['A2'].font = Font(name=FN, size=9, italic=True, color='C00000'); wk.merge_cells('A2:P2')
    head(wk, 4, ['구분', '순번', '티커', '종목', '종합점수', '등급', f'현재가 ({cur})', f'적정주가 ({cur})', '괴리율', '전체평균 기준 적정주가', '전체평균 기준 괴리율', '정점 이익 주의', '내재 수익률', '데이터 연수 / 지표 수', '시총÷오너이익 (배)', '해석 메모'])
    GL = {0: 'A 이중 통과', 1: 'B 최근 이익 기준만', 2: 'C 정점·쇠퇴 의심', 3: 'D 시장 불신(8배 미만)'}
    GF = {0: 'C6EFCE', 1: 'FFF2CC', 2: 'F8CBAD', 3: 'D9D9D9'}
    for k, (grp, _, i, r, fa, peak, dual) in enumerate(kc):
        row = 5 + k; sr = 12 + i
        cell(wk, f'A{row}', GL[grp], fill=PatternFill('solid', fgColor=GF[grp]))
        cell(wk, f'B{row}', k + 1, align='center'); cell(wk, f'C{row}', f"='순위표'!B{sr}", bold=True, align='center'); cell(wk, f'D{row}', f"='순위표'!C{sr}")
        cell(wk, f'E{row}', f"='순위표'!D{sr}", fmt='0.00', align='center'); cell(wk, f'F{row}', f"='순위표'!E{sr}", align='center')
        cell(wk, f'G{row}', f"='순위표'!R{sr}", fmt='#,##0.00', align='center'); cell(wk, f'H{row}', f"='순위표'!S{sr}", fmt='#,##0.00', align='center')
        cell(wk, f'I{row}', f"='순위표'!U{sr}", fmt='0%;-0%;0%', bold=True, align='center')
        if fa:
            cell(wk, f'K{row}', fa / r['mcap'] - 1, '0000FF', fmt='0%;-0%;0%', align='center')
            cell(wk, f'J{row}', f'=IF(ISNUMBER(G{row}),G{row}*(1+K{row}),"-")', fmt='#,##0.00', align='center')
        else:
            cell(wk, f'J{row}', 'n/a', align='center'); cell(wk, f'K{row}', 'n/a', align='center')
        cell(wk, f'L{row}', '⚠ 감소추세' if r.get('trend_down') else ('⚠ 있음' if peak else '-'), 'C00000' if peak else '000000', align='center')
        cell(wk, f'M{row}', f"='순위표'!W{sr}", fmt='0.0%', align='center'); cell(wk, f'N{row}', f"='순위표'!Q{sr}", align='center')
        boe = r.get('base_oe'); mult = (r['mcap'] / boe) if boe and boe > 0 else None
        memo = []
        if mult is not None and mult < 8:
            memo.append(f'시장이 오너이익의 {mult:.1f}배만 지불 → 이익 감소·구조적 쇠퇴를 가격에 반영 중일 가능성')
        if r['fv_usd'] / r['mcap'] - 1 > 1.0:
            memo.append('괴리율 100% 초과: 모델 한계 영역(검증 안 됨), 원인을 먼저 찾기')
        cell(wk, f'O{row}', mult if mult is not None else 'n/a', '0000FF', fmt='0.0', align='center')
        cell(wk, f'P{row}', '; '.join(memo) or '-', wrap=True)
    for kk, w in {'A': 18, 'B': 7, 'C': 10, 'D': 30, 'E': 10, 'F': 10, 'G': 13, 'H': 14, 'I': 10, 'J': 16, 'K': 14, 'L': 12, 'M': 12, 'N': 20, 'O': 14, 'P': 60}.items():
        wk.column_dimensions[kk].width = w
    wk.freeze_panes = 'E5'

    w2 = wb.create_sheet('지표상세')
    mids = ['M1', 'M2', 'M3', 'M4', 'Q1', 'Q2', 'Q3', 'C1', 'C2', 'C3', 'C4', 'F1', 'F2', 'F3']
    head(w2, 1, ['티커'] + [f'{m} {s}' for m in mids for s in ('값', '점수')])
    for i, r in enumerate(ranked):
        row = 2 + i; cell(w2, f'A{row}', r['ticker'], bold=True)
        for j, m in enumerate(mids):
            v, s = r['metrics'].get(m), r['scores'].get(m)
            cell(w2, f'{L(2 + 2 * j)}{row}', v if v is not None else 'n/a', '0000FF', fmt='0.000', align='center')
            cell(w2, f'{L(3 + 2 * j)}{row}', s if s is not None else 'n/a', '0000FF', fmt='0.00', align='center')
    w2.freeze_panes = 'B2'

    w3 = wb.create_sheet('제외·데이터부족')
    head(w3, 1, ['티커', '종목', '사유'])
    for i, (t, nm, why) in enumerate(excluded):
        cell(w3, f'A{2 + i}', t, bold=True); cell(w3, f'B{2 + i}', nm); cell(w3, f'C{2 + i}', why, wrap=True)
    w3.column_dimensions['A'].width = 10; w3.column_dimensions['B'].width = 32; w3.column_dimensions['C'].width = 90

    w4 = wb.create_sheet('종목별 주의')
    head(w4, 1, ['티커', '종목', '보정·주의 내역'])
    for i, r in enumerate(ranked):
        cell(w4, f'A{2 + i}', r['ticker'], bold=True); cell(w4, f'B{2 + i}', r['name']); cell(w4, f'C{2 + i}', '; '.join(r['notes']) or '-', wrap=True)
    w4.column_dimensions['A'].width = 10; w4.column_dimensions['B'].width = 32; w4.column_dimensions['C'].width = 100

    w5 = wb.create_sheet('가정·한계')
    lines = ['■ 범위: 세계 전체가 아니라 미국 SEC에 10-K를 내는 기업만 분석합니다(한국·유럽·일본·중국 상장사와 20-F 제출 외국 기업은 제외). 해외 종목은 별도 단계(예: 한국 DART)가 필요합니다.',
             '■ 데이터: SEC EDGAR XBRL 연간(10-K) 공시, 최근 10개 회계연도(종료일 기준 연도). 시총은 companiesmarketcap.com CSV 값.',
             '■ 가정(전 종목 동일): 무위험수익률 4.2%, 주식위험프리미엄 5%, 베타 1.0, 세율 21%, 차입금 이자율 5.5%, 최소 기대수익률 9%, 등급별 할인율 가산·성장률 상한.',
             '■ 오너이익 = 순이익 + 감가상각 − 유지보수 투자 (운전자본 제외, 최근 3년 평균). 유지보수 투자 = 감가상각 + (설비투자 − 감가상각)의 50%. 감가상각 자료가 없으면 순이익을 사용. 1~5년 성장률 = 매출 CAGR을 0~10%로 제한.',
             '■ 미반영: 정성(텍스트) 신호, 경영진·공시 영역(내부자 지분·가이던스·감사의견), 10년 전 시총(C1), 국가별 금리 차이.',
             '■ 한계: 차입금 태그는 회사마다 달라 누락·부정확할 수 있습니다(누락 시 무차입 가정, 종목별 주의 시트 참조). 주식 분할이 의심되는 종목은 주식 수 변화 지표(C2)를 제외했습니다.',
             '■ 현재가·적정주가: 시총 ÷ 주가로 주식 수를 구해 적정 시총을 나눈 값입니다(CSV에 주가가 없으면 공시 주식 수 사용). 적정주가는 보수적 가정의 근사치이며 매수 기준가는 안전마진을 뺀 값입니다.',
             '■ 금융업·외국 기업(20-F)·데이터 5년 미만 종목은 순위에서 제외했습니다. 순위는 참고용이며 투자 권유가 아닙니다.']
    if limits:
        lines = limits
    lines = list(lines) + [
        '■ 내재 수익률: 오늘 시총으로 샀을 때 같은 이익 경로가 실현된다면 얻는 연 수익률(현재 시총을 정확히 만드는 할인율). 최소 기대수익률(9%)보다 높으면 싸고 낮으면 비싸다는 뜻입니다.',
        '■ 낙관 시나리오: 1~5년 성장률 상한 15%, 최소 기대수익률 8%, 할인율 1%p 인하로 다시 계산한 적정 시총(기준 계산이 보수적이라 범위를 함께 봅니다).',
        '■ 베타: 기본은 모든 종목 1.0. 시작 질문에서 "함"을 고르면 5년 월간 수익률로 S&P500 대비 베타를 구해(블룸 조정, 0.5~2.0) 자기자본비용(무위험+베타×위험프리미엄)에 반영합니다. 최소 기대수익률 9%가 하한이라 베타가 낮아도 할인율은 9% 아래로 내려가지 않습니다. 해외 종목도 S&P500 기준이라 현지 시장 베타와 다를 수 있습니다.',
        '■ 백테스트 요약(미국, 메뉴 7, 2021~2026 구간, 시총 상위 약 1,100개): ① 종합점수·영역·지표 점수는 이후 수익률과 우연을 넘는 양(+)의 관계가 없었고, 소형주에서는 오히려 점수 낮은 쪽이 더 올랐습니다(생존 편향 큼). 대형·중형에서는 무관했습니다. ② 적정가÷시총과 내재 수익률은 모든 구간에서 수익률과 약한 양(+)의 방향(순위상관 +0.04~+0.16)이었으나 통계적으로 구분되지 않았습니다. ③ 점수 최하위 20%는 -40% 이상 낙폭 비율이 다른 구간의 약 2배였습니다.',
        '■ 따라서: 점수는 수익률 예측기가 아니라 위험·사업 질 점검표로, 적정주가·괴리율은 "지금 가격에서 기대되는 수익률(내재 수익률)을 보는 도구"로 읽으세요. 검증 구간이 최근 5년뿐이고 구간들이 서로 독립이 아니며 생존 편향이 있어 결론이 아니라 참고입니다.',
        '■ 오너이익 정상화: 최근 5년 오너이익의 변동계수가 0.5를 넘는 경기순환형은 3년 평균과 5년 평균 중 낮은 값을 씁니다(해당 종목은 [종목별 주의]에 표시).']
    for i, t in enumerate(lines):
        c = w5.cell(row=1 + i, column=1, value=t); c.font = Font(name=FN, size=10); c.alignment = Alignment(wrap_text=True, vertical='top')
    w5.column_dimensions['A'].width = 150
    for s in wb.worksheets:
        s.sheet_view.showGridLines = False
    wb.save(path)


def ask(prompt, default=None):
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ''
    return v or default


def download_csv(path):
    req = urllib.request.Request('https://companiesmarketcap.com/?download=csv',
                                 headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if b'<html' in data[:1000].lower() or b',' not in data[:300]:
        raise ValueError('CSV가 아닌 페이지가 내려왔습니다')
    open(path, 'wb').write(data)


# ===== 텍스트 신호 (사업보고서 10-K 본문, 미국) =====
NEG_WORDS = set('''adverse adversely against breach burden cancel cancellation claims closure concern concerns curtail damage damages decline declined declines
decrease decreased default defaults deficiency delay delayed delays delinquent deteriorate deterioration difficult difficulties diminish disadvantage disrupt
disruption disruptions dispute disputes downturn failure failures fail failed fraud harm harmful impair impaired impairment impairments inability ineffective insolvency
instability investigation investigations lawsuit lawsuits litigation lose loss losses negative obstacle penalty penalties problem problems recall recalls restructuring
severe shortage shortages shutdown slowdown suspend suspended terminate terminated termination threat threats unable unfavorable violation violations volatile weak weakness
weaknesses worsen worsened writedown writedowns unsuccessful lawsuit adversely reduced reduction reductions'''.split())
UNC_WORDS = set('''approximately assume assumed assumption assumptions believe believes could depend depends depending doubt estimate estimated estimates exposure exposures fluctuate
fluctuates fluctuation fluctuations indefinite may maybe might perhaps possible possibly predict predictions probable risk risks risky seems sometimes speculate tentative
uncertain uncertainties uncertainty unknown unpredictable unsettled vary varies volatility anticipate anticipated'''.split())
LIQ_RE = re.compile(r'liquidity|borrow|credit facilit|indebtedness|\bdebt\b|refinanc|covenant', re.I)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']*")
SENT_SPLIT = re.compile(r'(?<=[\.\?\!])\s+(?=[A-Z])')

TEXT_ITEMS = [  # id, 영역, 질문, 규칙 종류
    ('T1', '해자', '무형자산(브랜드·특허·라이선스) 해자 서술', 'kw'),
    ('T2', '해자', '전환비용 서술', 'kw'),
    ('T3', '해자', '네트워크 효과 서술', 'kw'),
    ('T4', '해자', '원가 우위·규모의 경제 서술', 'kw'),
    ('T6', '자본배분', '자본배분 원칙(투자·인수·환원 우선순위) 설명', 'cap'),
    ('T7', '자본배분', '과거 실수·기대 미달 인정', 'kw'),
    ('T8', '자본배분', '자사주 매입을 가치·가격과 연결해 설명', 'bb'),
    ('T9', '경영진·공시 품질', '사업을 쉬운 문장으로 설명 (평균 문장 길이)', 'len'),
    ('T12', '경영진·공시 품질', '특수관계자 거래 공시', 'kw'),
]
KW = {
    'T1': r"brand (recognition|loyalty|strength|equity)|patent portfolio|proprietary (technology|rights)|strong brand|trademark",
    'T2': r"switching costs?",
    'T3': r"network effects?",
    'T4': r"cost advantage|low[- ]cost (producer|provider|leader|structure)|economies of scale|scale advantage",
    'T7': r"\bmistakes?\b|\berred\b|lessons learned|did not (meet|achieve) (our )?(expectations|goals)|fell short",
    'T12': r"related[- ]party transactions?|related persons?",
}
JUDGE_SCORE = {'예': 5.0, '부분': 2.5, '아니오': 0.0}
TONE_FLOOR, TONE_CAP = 0.25, -0.25   # 파라미터 시트와 동일: 전년 대비 +25% 악화 0점, -25% 개선 5점


def html_to_text(raw):
    t = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else raw
    t = re.sub(r'(?is)<ix:header>.*?</ix:header>', ' ', t)
    t = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', t)
    t = re.sub(r'(?i)</(p|div|tr|li|h\d|br)\s*>|<br\s*/?>', '\n', t)
    t = re.sub(r'<[^>]+>', ' ', t)
    import html as _h
    t = _h.unescape(t).replace('\xa0', ' ')
    return re.sub(r'[ \t\r\f\v]+', ' ', t)


def _section(text, start_pat, end_pat, need_end=False, max_len=400000):
    """start_pat 뒤부터 end_pat 앞까지 중 가장 긴 구간. need_end=True면 끝 표지를 실제로 찾은 구간만 인정(본문 참조 문구에서 시작해 주석까지 흘러가는 것을 막음)."""
    best = ''
    for m in re.finditer(start_pat, text, re.I):
        e = re.search(end_pat, text[m.end(): m.end() + max_len], re.I)
        if e is None and need_end:
            continue
        seg = text[m.end(): m.end() + e.start()] if e else text[m.end(): m.end() + 200000]
        if len(seg) > len(best):
            best = seg
    return best


def _tone(words):
    n = len(words)
    if n < 500:
        return None
    k = sum(1 for w in words if w in NEG_WORDS or w in UNC_WORDS)
    return k / n


def analyze_text(text):
    """10-K 본문 → 통계(작은 dict). 근거 문장은 원문 그대로 발췌."""
    s1a = _section(text, r'item\s*1a\.?\s*[\-–—:]?\s*risk\s*factors', r'item\s*1b')
    s7 = _section(text, r'item\s*7\.?\s*[\-–—:]?\s*management.s\s*discussion', r'item\s*7a')
    s1 = _section(text, r'item\s*1\.?\s*[\-–—:]?\s*business', r'item\s*1a')
    tone_src = (s1a + ' ' + s7) if len(s1a) + len(s7) > 5000 else text
    words = [w.lower() for w in WORD_RE.findall(tone_src)]
    out = {'tone': _tone(words), 'tone_src': 'Item 1A+7' if tone_src is not text else '문서 전체'}
    liq = [s for s in SENT_SPLIT.split(s7 or text) if LIQ_RE.search(s)]
    lw = [w.lower() for s in liq for w in WORD_RE.findall(s)]
    out['liq_tone'] = _tone(lw)
    base = s1 if len(s1) > 3000 else text
    sents = [s for s in SENT_SPLIT.split(re.sub(r'\s+', ' ', base)) if len(WORD_RE.findall(s)) >= 4]
    if sents:
        out['avg_len'] = sum(len(WORD_RE.findall(s)) for s in sents) / len(sents)
        out['len_src'] = 'Item 1' if base is s1 else '문서 전체'
    allsents = [s.strip() for s in SENT_SPLIT.split(re.sub(r'\s+', ' ', text)) if 20 < len(s) < 700]

    def first(pat, extra=None):
        for s in allsents:
            if re.search(pat, s, re.I) and (extra is None or re.search(extra, s, re.I)):
                return s[:320]
        return None
    ev = {}
    for k, pat in KW.items():
        ev[k] = first(pat)
    ev['cap_prio'] = first(r'capital allocation', r'priorit|first|framework|principle')
    ev['cap_any'] = first(r'capital allocation')
    ev['bb_any'] = first(r'repurchase|buyback')
    ev['bb_val'] = first(r'repurchase|buyback', r'intrinsic value|undervalued|below (our )?(estimate|intrinsic)')
    ev['bb_soft'] = first(r'repurchase|buyback', r'opportunistic|attractive|valuation|market conditions and (the )?price')
    out['ev'] = ev
    return out


def judge(stats):
    """항목별 (판정, 근거) — 키워드는 자기서술 여부 확인이 필요해 '부분'까지만, 근거 없으면 '불명'."""
    ev, res = stats['ev'], {}
    for tid, _, _, kind in TEXT_ITEMS:
        if kind == 'kw':
            res[tid] = ('부분', ev[tid]) if ev.get(tid) else ('불명', None)
        elif kind == 'cap':
            res[tid] = ('예', ev['cap_prio']) if ev.get('cap_prio') else (('부분', ev['cap_any']) if ev.get('cap_any') else ('불명', None))
        elif kind == 'bb':
            if ev.get('bb_val'):
                res[tid] = ('예', ev['bb_val'])
            elif ev.get('bb_soft'):
                res[tid] = ('부분', ev['bb_soft'])
            elif ev.get('bb_any'):
                res[tid] = ('아니오', ev['bb_any'])
            else:
                res[tid] = ('불명', None)
        elif kind == 'len':
            a = stats.get('avg_len')
            if a is None:
                res[tid] = ('불명', None)
            else:
                v = '예' if a <= 22 else '부분' if a <= 28 else '아니오'
                res[tid] = (v, f"평균 문장 길이 {a:.1f}단어 ({stats.get('len_src')} 기준; 22 이하 예, 28 이하 부분)")
    return res


def tone_score(cur, prev):
    if cur is None or not prev:
        return None, None
    ch = cur / prev - 1
    return ch, min(5.0, max(0.0, (ch - TONE_FLOOR) / (TONE_CAP - TONE_FLOOR) * 5))


def list_10k(cik, ua, cache):
    sub = cached(os.path.join(cache, f'SUB{cik}.json'), f'https://data.sec.gov/submissions/CIK{cik}.json', ua)
    rec = sub.get('filings', {}).get('recent', {})
    rows = [(f, a, d, r) for f, a, d, r in zip(rec.get('form', []), rec.get('accessionNumber', []),
                                              rec.get('primaryDocument', []), rec.get('filingDate', [])) if f == '10-K']
    rows.sort(key=lambda x: x[3], reverse=True)
    return rows[:2]


def text_stats_for(cik, acc, doc, ua, cache):
    p = os.path.join(cache, f'TXT{cik}_{acc}.json')
    if os.path.exists(p):
        return json.load(open(p, encoding='utf-8'))
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
    raw = http_json(url, ua, raw=True)
    st_ = analyze_text(html_to_text(raw))
    json.dump(st_, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
    time.sleep(0.35)
    return st_


def text_signals(cik, ua, cache):
    """-> dict(items{tid:(판정,근거)}, tone(ch,score), liq(ch,score), source, area{영역:점수}, 확보율)"""
    fl = list_10k(cik, ua, cache)
    if not fl:
        return None
    cur = text_stats_for(cik, fl[0][1], fl[0][2], ua, cache)
    prev = text_stats_for(cik, fl[1][1], fl[1][2], ua, cache) if len(fl) > 1 else {}
    src = f'10-K 제출 {fl[0][3]} (접수번호 {fl[0][1]})'
    items = judge(cur)
    t_ch, t_sc = tone_score(cur.get('tone'), prev.get('tone'))
    l_ch, l_sc = tone_score(cur.get('liq_tone'), prev.get('liq_tone'))
    pools = {}
    for tid, area, _, _ in TEXT_ITEMS:
        v = items[tid][0]
        if v in JUDGE_SCORE:
            pools.setdefault(area, []).append(JUDGE_SCORE[v])
    if t_sc is not None:
        pools.setdefault('이익의 질', []).append(t_sc)
    if l_sc is not None:
        pools.setdefault('재무건전성', []).append(l_sc)
    n_all = len(TEXT_ITEMS) + 2
    n_ok = sum(len(v) for v in pools.values())
    return dict(items=items, tone=(t_ch, t_sc, cur.get('tone'), prev.get('tone')), liq=(l_ch, l_sc), source=src,
                area={a: sum(v) / len(v) for a, v in pools.items()}, coverage=n_ok / n_all)


def blend(r, ts):
    """정량 영역 점수와 텍스트 영역 점수를 파라미터 시트의 '텍스트 비중(w)'으로 합산."""
    W = {'해자': .2, '이익의 질': .2, '자본배분': .1, '재무건전성': .1, '경영진·공시 품질': .4}
    base = dict(zip(AREAS, AREA_W))
    tot = wsum = 0.0
    areas = {}
    for a in AREAS:
        q, t = r['area'].get(a), ts['area'].get(a)
        if q is not None and t is not None:
            v = (1 - W[a]) * q + W[a] * t
        else:
            v = q if q is not None else t
        areas[a] = v
        if v is not None:
            tot += base[a] * v; wsum += base[a]
    comp = tot / wsum if wsum else None
    ti = max(i for i, t in enumerate(TIERS) if comp >= t[0]) if comp is not None else 0
    warn = None
    if ts['tone'][0] is not None and ts['tone'][0] >= 0.25 and (r['area'].get('이익의 질') or 9) <= 1.5:
        warn = '어조 악화(+25% 이상) + 이익의 질 낮음'
    return comp, TIERS[ti][1], areas, warn


def add_text_sheets(path, ranked, results):
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = load_workbook(path)
    hdr = PatternFill('solid', fgColor='1F3864')
    def head(ws, row, labels):
        for i, t in enumerate(labels):
            c = ws.cell(row=row, column=1 + i, value=t); c.font = Font(name='맑은 고딕', size=10, bold=True, color='FFFFFF')
            c.fill = hdr; c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    ws = wb.create_sheet('텍스트신호', 1)
    ws['A1'] = '사업보고서(10-K) 텍스트 신호 반영 점수'; ws['A1'].font = Font(name='맑은 고딕', size=14, bold=True)
    ws['A2'] = ('키워드·문장길이·어조 사전으로 자동 산출한 근사치입니다(AI 판단 아님). 키워드 항목은 "회사의 자기 서술인지"를 가려내지 못해 최대 "부분"까지만 줍니다. '
                '정량+텍스트 종합 = 영역별 (1-w)×정량 + w×텍스트 (w: 해자 20%, 이익의 질 20%, 자본배분 10%, 재무건전성 10%, 경영진·공시 40%). 근거 문장은 [텍스트근거] 시트.')
    ws['A2'].font = Font(name='맑은 고딕', size=9, italic=True, color='C00000'); ws.merge_cells('A2:N2')
    head(ws, 4, ['티커', '종목', '정량 종합', '정량+텍스트 종합', '등급(반영 후)', '순위(반영 후)', '해자(텍스트)', '이익의 질(텍스트)', '자본배분(텍스트)',
                 '재무건전성(텍스트)', '경영진·공시(텍스트)', '근거 확보율', '어조 변화율', '경고'])
    rows = []
    for r in ranked:
        ts = results.get(r['ticker'])
        if not ts:
            continue
        comp, tier, areas, warn = blend(r, ts)
        rows.append((r, ts, comp, tier, warn))
    rows.sort(key=lambda x: -(x[2] or 0))
    for k, (r, ts, comp, tier, warn) in enumerate(rows):
        row = 5 + k
        vals = [r['ticker'], r['name'], r['composite'], comp, tier, k + 1] + [ts['area'].get(a) for a in
               ['해자', '이익의 질', '자본배분', '재무건전성', '경영진·공시 품질']] + [ts['coverage'], ts['tone'][0], warn or '-']
        fm = [None, None, '0.00', '0.00', None, '0', '0.00', '0.00', '0.00', '0.00', '0.00', '0%', '0%;-0%;0%', None]
        for j, (v, f) in enumerate(zip(vals, fm)):
            c = ws.cell(row=row, column=1 + j, value=v if v is not None else '-')
            c.font = Font(name='맑은 고딕', size=10, color='0000FF' if j >= 2 and j not in (4,) else '000000')
            if f: c.number_format = f
    for kk, w in zip('ABCDEFGHIJKLMN', [10, 28, 10, 14, 14, 10, 12, 14, 14, 14, 16, 11, 12, 34]):
        ws.column_dimensions[kk].width = w
    ws.freeze_panes = 'C5'
    w2 = wb.create_sheet('텍스트근거', 2)
    head(w2, 1, ['티커', '번호', '영역', '항목', '판정', '근거 문장 (원문 발췌)', '출처'])
    row = 2
    qmap = {t[0]: t for t in TEXT_ITEMS}
    for r, ts, *_ in rows:
        for tid, (v, ev) in ts['items'].items():
            _, area, q, _k = qmap[tid]
            for j, val in enumerate([r['ticker'], tid, area, q, v, ev or '-', ts['source']]):
                c = w2.cell(row=row, column=1 + j, value=val); c.font = Font(name='맑은 고딕', size=10)
                c.alignment = Alignment(wrap_text=True, vertical='top')
            row += 1
        for tid, area, q, val in [('N1', '이익의 질', '위험요인·MD&A 부정+불확실 단어 비율', ts['tone']), ('N2', '재무건전성', '유동성·차입 문장의 부정+불확실 단어 비율', None)]:
            if tid == 'N1':
                ev = f"당해 {val[2]:.2%} / 전년 {val[3]:.2%} → 변화율 {val[0]:+.0%}" if val[0] is not None else '-'
                v = '유효' if val[0] is not None else '불명'
            else:
                ev = f"변화율 {ts['liq'][0]:+.0%}" if ts['liq'][0] is not None else '-'
                v = '유효' if ts['liq'][0] is not None else '불명'
            for j, x in enumerate([r['ticker'], tid, area, q, v, ev, ts['source']]):
                c = w2.cell(row=row, column=1 + j, value=x); c.font = Font(name='맑은 고딕', size=10)
                c.alignment = Alignment(wrap_text=True, vertical='top')
            row += 1
    for kk, w in zip('ABCDEFG', [10, 7, 14, 36, 8, 90, 38]):
        w2.column_dimensions[kk].width = w
    w2.freeze_panes = 'A2'
    for s in (ws, w2):
        s.sheet_view.showGridLines = False
    wb.save(path)


def run_text_stage(path, ranked, ua, cache, k):
    results, tried = {}, 0
    print(f'\n[텍스트] 순위 상위 {min(k, len(ranked))}종목의 최근 10-K 2건을 내려받아 분석합니다(종목당 수 초~수십 초).')
    for r in ranked[:k]:
        tried += 1
        try:
            ts = text_signals(r['cik'], ua, cache)
        except SecError as e:
            if str(e) == 'RATE':
                print('   SEC가 요청 과다로 잠시 차단했습니다. 지금까지 분석한 종목까지만 반영합니다. (30분~1시간 뒤 재실행하면 이어집니다)')
                break
            print(f"   {r['ticker']}: 문서 조회 실패 - 건너뜀"); continue
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"   {r['ticker']}: 분석 실패({str(e)[:60]}) - 건너뜀"); continue
        if ts:
            results[r['ticker']] = ts
            print(f"   [{tried}/{min(k, len(ranked))}] {r['ticker']:6s} 텍스트 근거 확보율 {ts['coverage']:.0%}")
    if results:
        add_text_sheets(path, ranked, results)
        print(f'   텍스트 신호 {len(results)}종목을 엑셀의 [텍스트신호]·[텍스트근거] 시트에 추가했습니다.')
    return results


def run_us(here, email, top, csv_path, ua, cache, only=None, text_top=0, use_beta=False):
    out = os.path.join(here, f'{"관심종목_미국" if only else "미국상장_모델순위"}_{date.today():%Y%m%d}.xlsx')
    comps = read_csv(csv_path)[:top] if only is None else [c for c in read_csv(csv_path) if c[0] in only]
    print(f'\n대상 {len(comps)}개 종목을 처리합니다. 처음에는 몇 분 걸릴 수 있습니다(받은 자료는 저장되어 다음부터 빠릅니다).\n')
    betas = get_betas([c[0] for c in comps], here) if use_beta else {}
    try:
        tick = cached(os.path.join(cache, 'company_tickers.json'), 'https://www.sec.gov/files/company_tickers.json', ua)
    except (SecError, urllib.error.URLError, OSError) as e:
        if str(e) == 'RATE':
            sys.exit('\nSEC가 이 PC의 인터넷 주소(IP)를 "요청이 너무 많다"며 잠시 차단했습니다.\n'
                     '이메일이나 프로그램 문제가 아닙니다. 지금 계속 다시 실행하면 차단이 더 길어집니다.\n'
                     '  -> 이 창을 닫고 30분~1시간 뒤에 딱 한 번만 다시 실행하세요. (보통 10분~수 시간 안에 풀립니다)\n'
                     '  -> 급하면 휴대폰 핫스팟 등 다른 인터넷으로 바꿔 실행하세요.\n'
                     '  -> 풀린 뒤에는 초당 3건 이하로 천천히 받으므로 다시 막히지 않습니다.')
        diag = os.path.join(here, 'sec_error.txt')
        open(diag, 'w', encoding='utf-8').write(f'{datetime.now():%Y-%m-%d %H:%M:%S}\n{e}\n')
        sys.exit('\nSEC 서버가 접속을 거부했거나 연결되지 않았습니다.\n'
                 f'--- 상세 ---\n{e}\n------------\n'
                 '확인해 보세요:\n'
                 '  1) 같은 PC에서 브라우저로 https://www.sec.gov/files/company_tickers.json 이 열리는지\n'
                 '  2) 휴대폰 핫스팟 등 다른 인터넷으로 바꿔 시도 (회사·공용 망, VPN은 차단될 수 있음)\n'
                 '  3) 10분쯤 기다린 뒤 다시 시도 (요청이 몰리면 SEC가 잠시 막습니다)\n'
                 f'위 상세 내용은 {diag} 에도 저장했습니다. 안 풀리면 이 파일 내용을 알려 주세요.')
    t2cik = {v['ticker'].upper(): str(v['cik_str']).zfill(10) for v in tick.values()}
    ranked, excluded = [], []
    ext_dir = os.path.join(cache, 'ext'); os.makedirs(ext_dir, exist_ok=True)
    stopped = None
    try:
        for i, (sym, name, cap, px) in enumerate(comps, 1):
            if i % 100 == 0:
                print(f'   ... {i}/{len(comps)} 처리 중 (현재 순위 산출 {len(ranked)}종목)')
            cik = t2cik.get(sym) or t2cik.get(sym.replace('-', '.'))
            if not cik:
                excluded.append((sym, name, 'SEC 미등록(미국 외 상장 또는 외국 기업)')); continue
            try:
                ep = os.path.join(ext_dir, f'{cik}.json')
                if os.path.exists(ep):
                    cj = json.load(open(ep, encoding='utf-8'))
                    res, why = (tuple(cj[0]) if cj[0] else None), cj[1]
                else:
                    rawp = os.path.join(cache, f'CIK{cik}.json')
                    if os.path.exists(rawp):
                        fj = json.load(open(rawp, encoding='utf-8'))
                    else:
                        fj = http_json(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json', ua)
                        time.sleep(0.35)  # SEC: 초당 10건 이하
                    res, why = extract(fj)
                    json.dump([list(res) if res else None, why], open(ep, 'w', encoding='utf-8'), ensure_ascii=False)
                    del fj
            except SecError as e:
                if str(e) == 'RATE':
                    stopped = f'{sym} 처리 중 SEC가 요청 과다로 잠시 차단했습니다.'; break
                excluded.append((sym, name, f'SEC 조회 실패: {str(e)[:200]}')); continue
            except (urllib.error.URLError, OSError) as e:
                excluded.append((sym, name, f'SEC 조회 실패: {str(e)[:200]}')); continue
            except Exception as e:  # noqa: BLE001
                excluded.append((sym, name, f'자료 분석 오류: {str(e)[:150]}')); continue
            if res is None:
                excluded.append((sym, name, why)); continue
            try:
                d, used = res
                d, notes = check_and_fill(d, used)
                if d is None:
                    excluded.append((sym, name, '; '.join(notes))); continue
                excl = ('C2',) if split_jump(d['shares_out']) else ()
                if excl:
                    notes.append('주식 수 급변(분할 의심) → C2 제외')
                mcap_m = to_millions(cap)
                bt = betas.get(sym)
                A_ = {'beta': bt} if bt else None
                r = evaluate(d, mcap_m, A=A_, exclude=excl)
            except Exception as e:  # noqa: BLE001
                excluded.append((sym, name, f'계산 실패: {str(e)[:150]}')); continue
            if px:
                shares_m = mcap_m / px  # 시총 ÷ 주가 = 현재 주식 수(백만주)
            else:
                sv = [x for x in d['shares_out'] if x]
                shares_m = sv[-1] if sv else None
                notes.append('CSV에 주가가 없어 최근 공시의 주식 수로 주가를 환산')
            add_scenarios(r, d, mcap_m, notes, A=A_, exclude=excl)
            r.update(ticker=sym, name=name, mcap=mcap_m, fv_usd=r['fv_rep'], notes=notes, shares_m=shares_m, cik=cik)
            ranked.append(r)
            print(f'[{i}/{len(comps)}] {sym:6s} 점수 {r["composite"]:.2f}')
    except KeyboardInterrupt:
        stopped = '사용자가 중단했습니다.'
    if stopped:
        print(f'\n※ {stopped} 지금까지 처리한 {len(ranked)}종목으로 결과를 저장합니다.'
              '\n   (받은 자료는 저장되어 있어, 다시 실행하면 이어서 빠르게 진행됩니다. SEC 차단이면 30분~1시간 뒤에 한 번만 재실행하세요.)')
    ranked.sort(key=lambda x: -x['composite'])
    if not ranked:
        sys.exit('\n순위를 산출한 종목이 없어 결과 파일을 만들지 않았습니다. 위 안내를 확인하고 다시 실행하세요.')
    write_xlsx(out, ranked, excluded, date.today().isoformat(),
               title=(f'관심종목 미국 SEC 10-K 공시 기업 모델 분석 ({date.today().isoformat()} 산출)' if only else None))
    if text_top and ranked:
        run_text_stage(out, ranked, ua, cache, text_top)
    nf = sum(1 for _, _, why in excluded if ('SEC 미등록' in why or '20-F' in why))
    print(f'\n완료: 순위 {len(ranked)}종목, 제외·데이터부족 {len(excluded)}종목 (이 중 미국 외·외국 기업 {nf}종목)')
    print('※ 이 도구는 미국 SEC 10-K 공시 기업만 분석합니다. 해외(한국·유럽·일본·중국 등) 종목은 포함되지 않습니다.')
    print(f'결과 파일: {out}')
    if ranked:
        print('\n상위 10:')
        for k, r in enumerate(ranked[:10], 1):
            print(f"  {k:2d}. {r['ticker']:6s} {r['name'][:26]:26s} 점수 {r['composite']:.2f} ({r['tier'][1]})"
                  + (f"  현재가 ${r['mcap'] / r['shares_m']:,.2f}" if r.get('shares_m') else '')
                  + (f"  적정가 ${r['fv_usd'] / r['shares_m']:,.2f}" if r.get('shares_m') and r['fv_usd'] else ''))
    try:
        os.startfile(out)  # Windows: 결과 엑셀 자동 열기
    except (AttributeError, OSError):
        pass
    return ranked, excluded


DART = 'https://opendart.fss.or.kr/api/'
KR_ASSUME = dict(rf=0.035, erp=0.06, beta=1.0, tax=0.24, kd=0.05, hurdle=0.09)
FIN_WORDS = ['금융', '은행', '보험', '증권', '카드', '캐피탈', '생명', '화재', '해상', '저축', '투자증권', '리츠']


class DartError(Exception):
    pass


def dart_get(endpoint, params, binary=False):
    url = DART + endpoint + '?' + urllib.parse.urlencode(params)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=60) as r:
                raw = r.read()
            return raw if binary else json.loads(raw.decode('utf-8'))
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise DartError(f'DART 접속 실패: {last}')


def load_corp_codes(key, cache):
    path = os.path.join(cache, 'corp_codes.json')
    if os.path.exists(path):
        return json.load(open(path, encoding='utf-8'))
    raw = dart_get('corpCode.xml', {'crtfc_key': key}, binary=True)
    if raw[:2] != b'PK':  # zip 이 아니면 오류 메시지(XML/JSON)
        msg = re.sub(r'<[^>]+>', ' ', raw.decode('utf-8', 'ignore')).strip()[:200]
        raise DartError(f'인증키 확인 필요: {msg}')
    z = zipfile.ZipFile(io.BytesIO(raw))
    root = ET.fromstring(z.read(z.namelist()[0]))
    out = {}
    for it in root.iter('list'):
        sc = (it.findtext('stock_code') or '').strip()
        if sc:
            out[sc] = [it.findtext('corp_code').strip(), (it.findtext('corp_name') or '').strip()]
    json.dump(out, open(path, 'w', encoding='utf-8'), ensure_ascii=False)
    return out


def fetch_fs(key, corp, year, cache):
    """(rows, fs_div) 또는 (None, 사유). 연결(CFS) 우선, 없으면 개별(OFS)."""
    for fs in ('CFS', 'OFS'):
        p = os.path.join(cache, f'{corp}_{year}_{fs}.json')
        if os.path.exists(p):
            js = json.load(open(p, encoding='utf-8'))
        else:
            js = dart_get('fnlttSinglAcntAll.json', {'crtfc_key': key, 'corp_code': corp, 'bsns_year': year,
                                                     'reprt_code': '11011', 'fs_div': fs})
            st = js.get('status')
            if st in ('000', '013'):  # 정상 / 데이터 없음 만 저장
                json.dump(js, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
            time.sleep(0.12)
        st = js.get('status')
        if st == '000' and js.get('list'):
            return js['list'], fs
        if st == '020':
            raise DartError('QUOTA')
        if st in ('010', '011', '012'):
            raise DartError(f'KEY:{js.get("message")}')
        if st not in ('000', '013'):
            return None, f'DART 응답 {st} {js.get("message")}'
    return None, '사업보고서 없음'


# ---------- 계정 매칭 ----------
def nm_norm(t):
    t = re.sub(r'\s+', '', t or '')
    return re.sub(r'^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX0-9\.\)\(]+', '', t)


def num(x):
    if x is None:
        return None
    x = str(x).replace(',', '').strip()
    if x in ('', '-'):
        return None
    try:
        return float(x)
    except ValueError:
        return None


COLS = ('thstrm_amount', 'frmtrm_amount', 'bfefrmtrm_amount')  # 당기, 전기, 전전기


def vals(row):
    return [num(row.get(c)) for c in COLS]


def find(rows, sj, ids=(), names=(), contains=(), exclude=()):
    """우선순위: account_id → 정확한 계정명 → 포함 문자열. 첫 번째 매칭 행의 [당기,전기,전전기]."""
    pool = [r for r in rows if r.get('sj_div') in sj]
    for i in ids:
        for r in pool:
            if r.get('account_id') == i and any(v is not None for v in vals(r)):
                return vals(r), r.get('account_id')
    for n in names:
        for r in pool:
            if nm_norm(r.get('account_nm')) == n and any(v is not None for v in vals(r)):
                return vals(r), r.get('account_nm')
    for c in contains:
        for r in pool:
            nn = nm_norm(r.get('account_nm'))
            if c in nn and not any(e in nn for e in exclude) and any(v is not None for v in vals(r)):
                return vals(r), r.get('account_nm')
    return None, None


def add(a, b):
    return [None if (x is None and y is None) else (x or 0) + (y or 0) for x, y in zip(a, b)]


def sum_names(rows, sj, tests):
    """계정명이 tests 중 하나와 정확히 일치하는 모든 행(서로 다른 이름)을 합산."""
    seen, tot = set(), [None, None, None]
    for r in rows:
        if r.get('sj_div') not in sj:
            continue
        nn = nm_norm(r.get('account_nm'))
        if nn in tests and nn not in seen and any(v is not None for v in vals(r)):
            seen.add(nn); tot = add(tot, vals(r))
    return (tot if seen else None), sorted(seen)


DEBT_NAMES = {'단기차입금', '장기차입금', '사채', '유동성장기부채', '유동성장기차입금', '유동성사채', '전환사채', '단기사채',
              '유동성전환사채', '차입금', '장기사채', '단기금융차입금', '장기금융차입금', '유동성장기사채', '사채및장기차입금', '장기차입금및사채'}
DEBT_IDS = {'ifrs-full_ShorttermBorrowings', 'ifrs-full_CurrentPortionOfLongtermBorrowings',
            'ifrs-full_CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings', 'ifrs-full_LongtermBorrowings',
            'ifrs-full_NoncurrentPortionOfNoncurrentBondsIssued', 'ifrs-full_NoncurrentPortionOfNoncurrentLoansReceived',
            'ifrs-full_CurrentPortionOfNoncurrentBondsIssued'}


def sum_debt(rows, tests):
    """재무상태표의 차입금 계정 합산: 표준 계정 ID(유동·비유동 차입금·사채) 또는 계정명이 맞는 모든 행.
    이름이 같아도 ID·금액이 다른 행(유동 '차입금' + 비유동 '차입금')은 따로 더한다(리스부채 제외)."""
    seen, tot, labels = set(), [None, None, None], []
    for r in rows:
        if r.get('sj_div') != 'BS':
            continue
        nn = nm_norm(r.get('account_nm'))
        v = vals(r)
        if not any(x is not None for x in v):
            continue
        if r.get('account_id') in DEBT_IDS or nn in tests:
            key = (r.get('account_id'), nn, tuple(v))
            if key in seen:
                continue
            seen.add(key); tot = add(tot, v)
            if nn not in labels:
                labels.append(nn)
    return (tot if seen else None), sorted(labels)


def extract_year_block(rows):
    """한 번의 응답(3개 회계연도)에서 항목별 [당기,전기,전전기] 추출."""
    o, used = {}, {}
    o['revenue'], used['revenue'] = find(rows, ('IS', 'CIS'), ['ifrs-full_Revenue', 'ifrs_Revenue', 'dart_Revenue'],
                                         ['매출액', '수익(매출액)', '영업수익', '매출', '수익', '매출및지분법손익'])
    o['operating_income'], _ = find(rows, ('IS', 'CIS'), ['dart_OperatingIncomeLoss'],
                                    ['영업이익', '영업이익(손실)', '영업손익', '영업손실'])
    ni, ni_src = find(rows, ('IS', 'CIS'), ['ifrs-full_ProfitLossAttributableToOwnersOfParent', 'ifrs_ProfitLossAttributableToOwnersOfParent'],
                      ['지배기업소유주지분', '지배기업의소유주에게귀속되는당기순이익', '지배기업소유주지분순이익'])
    if ni is None:
        ni, ni_src = find(rows, ('IS', 'CIS'), ['ifrs-full_ProfitLoss', 'ifrs_ProfitLoss'],
                          ['당기순이익', '당기순이익(손실)', '연결당기순이익', '연결당기순이익(손실)', '당기순손익', '반기순이익'])
    o['net_income'] = ni
    # 감가상각: 한 줄로 합친 계정이 있으면 그것만, 없으면 감가상각비 + 무형자산상각비 (+사용권자산) 합산
    both = None
    for r in rows:
        if r.get('sj_div') == 'CF':
            nn = nm_norm(r.get('account_nm'))
            if '감가상각' in nn and '무형' in nn and any(v is not None for v in vals(r)):
                both = vals(r); break
    if both is not None:
        o['dep_amort'] = both
    else:
        tot, seen = [None, None, None], set()
        for r in rows:
            if r.get('sj_div') != 'CF':
                continue
            nn = nm_norm(r.get('account_nm'))
            if (nn.endswith('감가상각비') or nn.endswith('무형자산상각비') or nn.endswith('상각비')) and '충당' not in nn \
                    and nn not in seen and any(v is not None for v in vals(r)):
                seen.add(nn); tot = add(tot, vals(r))
        o['dep_amort'] = tot if seen else None
    o['cfo'], _ = find(rows, ('CF',), ['ifrs-full_CashFlowsFromUsedInOperatingActivities', 'ifrs_CashFlowsFromUsedInOperatingActivities'],
                       ['영업활동현금흐름', '영업활동으로인한현금흐름', '영업활동순현금흐름', '영업활동으로인한순현금흐름'])
    cap, _ = find(rows, ('CF',), ['ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities'],
                  ['유형자산의취득', '유형자산취득', '토지의취득'], ['유형자산의취득'], ['처분', '무형'])
    o['capex'] = None if cap is None else [None if v is None else abs(v) for v in cap]
    o['total_assets'], _ = find(rows, ('BS',), ['ifrs-full_Assets', 'ifrs_Assets'], ['자산총계'])
    o['equity'], _ = find(rows, ('BS',), ['ifrs-full_Equity', 'ifrs_Equity'], ['자본총계'])
    o['cash'], _ = find(rows, ('BS',), ['ifrs-full_CashAndCashEquivalents', 'ifrs_CashAndCashEquivalents'], ['현금및현금성자산'])
    debt_names = DEBT_NAMES
    o['total_debt'], debt_used = sum_debt(rows, debt_names)
    ie, _ = find(rows, ('IS', 'CIS'), ['ifrs-full_InterestExpense'], ['이자비용'])
    if ie is None:
        ie, _ = find(rows, ('IS', 'CIS'), (), ['금융원가', '금융비용'])
    o['interest_expense'] = None if ie is None else [None if v is None else abs(v) for v in ie]
    dv, _ = find(rows, ('CF',), ['ifrs-full_DividendsPaidClassifiedAsFinancingActivities'],
                 ['배당금의지급', '배당금지급', '배당금의지급(우선주포함)'], ['배당금'], ['수취', '수입', '미지급'])
    o['dividends_paid'] = None if dv is None else [None if v is None else abs(v) for v in dv]
    bb, _ = find(rows, ('CF',), ['ifrs-full_PaymentsToAcquireOrRedeemEntitysShares'],
                 ['자기주식의취득', '자기주식취득'], ['자기주식의취득'], ['처분'])
    o['buybacks'] = None if bb is None else [None if v is None else abs(v) for v in bb]
    return o, debt_used


def build_company(key, corp, cache, fx, n_years=10):
    """-> (d, notes) 또는 (None, [사유]). 금액은 USD 백만 단위로 환산."""
    today = date.today()
    L = today.year - 1 if today.month >= 4 else today.year - 2
    rows0, fs = fetch_fs(key, corp, L, cache)
    if rows0 is None and fs == '사업보고서 없음':
        L -= 1
        rows0, fs = fetch_fs(key, corp, L, cache)
    if rows0 is None:
        return None, [fs]
    blocks = {}
    notes = [f'DART {"연결" if fs == "CFS" else "개별"}재무제표 기준(사업보고서)']
    debt_names_seen = set()
    for base in (L, L - 3, L - 6, L - 9):
        rows, fs_b = (rows0, fs) if base == L else fetch_fs(key, corp, base, cache)
        if rows is None:
            continue
        o, du = extract_year_block(rows)
        debt_names_seen.update(du)
        for k, v in o.items():
            if v is None:
                continue
            for j, yy in enumerate((base, base - 1, base - 2)):
                blocks.setdefault(k, {})[yy] = v[j]
    yrs = list(range(L - n_years + 1, L + 1))
    d = {'years': yrs, 'gross_profit': [None] * n_years, 'shares_out': [None] * n_years}
    for k in ('revenue', 'operating_income', 'net_income', 'dep_amort', 'cfo', 'capex', 'total_assets', 'equity', 'cash',
              'total_debt', 'interest_expense', 'dividends_paid', 'buybacks'):
        m = blocks.get(k, {})
        d[k] = [None if m.get(y) is None else m[y] / 1e6 / fx for y in yrs]  # 원 → 백만원 → 백만달러
    if debt_names_seen:
        notes.append('차입금 = ' + ', '.join(sorted(debt_names_seen)) + ' 합산(리스부채 제외)')
    if d['interest_expense'][-1] is None:
        notes.append('이자비용 계정 없음 → F2(이자보상배율) 제외')
    return d, notes


def kr_fill_da(d, sym, fx):
    """한국 감가상각 보정 → 안내 문구 목록.
    ① 최근 3개년에 감가상각이 비어 있으면 Yahoo 값으로 빈 해를 채운다(DART 현금흐름표에는 주석에만 있는 경우가 많음).
    ② 감가상각이 설비투자를 넘으면 설비투자로 제한한다: Yahoo·DART 감가상각에는 리스 사용권·무형자산 상각이 섞여 있는데
       DART 설비투자는 유형자산 취득만이라, 그대로 두면 오너이익이 순이익보다 크게 부풀 수 있다(오너이익 ≤ 순이익)."""
    notes = []
    da = d['dep_amort']
    if any(x is None for x in da[-3:]):
        try:
            ensure_yfinance()
            import yfinance as yf
            m = _row(yf.Ticker(sym).cashflow, WORLD_ROWS['dep_amort'][0])
        except (Exception, SystemExit):  # noqa: BLE001
            m = None
        time.sleep(0.3)
        if m is None:
            notes.append('감가상각: Yahoo에서 가져오지 못해 빈 연도는 순이익으로 대체(오너이익 과대 가능)')
        else:
            n = 0
            for i, y in enumerate(d['years']):
                if da[i] is None and m.get(y) is not None:
                    da[i] = abs(m[y]) / 1e6 / fx
                    n += 1
            notes.append(f'감가상각: DART에 없는 해를 Yahoo {n}개년으로 보충(나머지 연도는 순이익으로 대체)' if n else
                         '감가상각: DART·Yahoo 모두에 없어 순이익으로 대체(오너이익 과대 가능)')
    cap_ = d.get('capex', [])
    cut = 0
    for i in range(len(da)):
        if da[i] is not None and i < len(cap_) and cap_[i] is not None and da[i] > abs(cap_[i]):
            da[i] = abs(cap_[i]); cut += 1
    if cut:
        notes.append(f'감가상각이 설비투자보다 큰 {cut}개 연도는 설비투자로 제한(리스·무형 상각 혼입 가능성, 오너이익 ≤ 순이익)')
    return notes


def run_kr(here, key, fx, top, csv_path, only=None, use_beta=False):
    allc = read_csv(csv_path)
    comps = [c for c in allc if re.fullmatch(r'\d{6}\.(KS|KQ)', c[0])]
    comps = comps[:top] if only is None else [c for c in comps if c[0][:6] in only]
    if not comps:
        sys.exit('companies.csv에 한국 종목(심볼이 005930.KS 형식)이 없습니다. 시총 표 전체(전 세계)를 받았는지 확인하세요.')
    cache = os.path.join(here, 'dart_cache'); os.makedirs(cache, exist_ok=True)
    betas = get_betas([c[0] for c in comps], here) if use_beta else {}
    print(f'\n한국 종목 {len(comps)}개를 처리합니다. (받은 자료는 저장되어 다음부터 빠릅니다)\n')
    try:
        corp = load_corp_codes(key, cache)
    except DartError as e:
        sys.exit(f'\n{e}')
    ranked, excluded = [], []
    for i, (sym, name, cap, px) in enumerate(comps, 1):
        code = sym[:6]
        if code not in corp:
            base_code = code[:5] + '0'
            why = (f'우선주로 보임(보통주 코드 {base_code} — 보통주로 따로 조회하세요)' if code[-1] != '0' and base_code in corp
                   else 'DART 기업 목록에 없음')
            excluded.append((sym, name, why)); continue
        cc, kname = corp[code]
        if any(w in kname for w in FIN_WORDS):
            excluded.append((sym, name, '금융업으로 추정(이 모델은 비금융 기업용)')); continue
        try:
            d, notes = build_company(key, cc, cache, fx)
        except DartError as e:
            m = str(e)
            if m == 'QUOTA':
                print(f'\n※ {sym} 처리 중 DART 일일 사용 한도에 도달했습니다. 지금까지 {len(ranked)}종목으로 결과를 저장합니다. 내일 다시 실행하면 이어서 진행합니다.')
                break
            if m.startswith('KEY'):
                sys.exit(f'\nDART 인증키 오류: {m[4:]}\n키를 다시 확인하세요. (설정.json 을 지우면 다시 입력할 수 있습니다)')
            excluded.append((sym, name, m[:200])); continue
        except Exception as e:  # noqa: BLE001
            excluded.append((sym, name, f'자료 분석 오류: {str(e)[:150]}')); continue
        if d is None:
            excluded.append((sym, name, '; '.join(notes))); continue
        notes = notes + kr_fill_da(d, sym, fx)
        d, notes2 = check_and_fill(d, {})
        if d is None:
            excluded.append((sym, name, '; '.join(notes2))); continue
        notes = notes + notes2
        mcap_m = to_millions(cap)
        try:
            A_ = {**KR_ASSUME, **({'beta': betas[sym]} if betas.get(sym) else {})}
            r = evaluate(d, mcap_m, A=A_)
        except Exception as e:  # noqa: BLE001
            excluded.append((sym, name, f'계산 실패: {e}')); continue
        shares_m = (mcap_m / px) if px else None
        if not px:
            notes.append('CSV에 주가가 없어 주당 값 계산 불가')
        ni3 = [x for x in d['net_income'][-3:] if x is not None]
        if ni3 and sum(ni3) > 0 and r.get('base_oe') is not None:
            notes.append(f'오너이익 기준값 = 순이익 3년 평균의 {r["base_oe"] / (sum(ni3) / len(ni3)):.0%}')
        add_scenarios(r, d, mcap_m, notes, A=A_)
        r.update(ticker=sym, name=name, mcap=mcap_m, fv_usd=r['fv_rep'], notes=notes, shares_m=shares_m)
        ranked.append(r)
        print(f'[{i}/{len(comps)}] {sym:10s} {name[:14]:14s} 점수 {r["composite"]:.2f}')
    ranked.sort(key=lambda x: -x['composite'])
    if not ranked:
        sys.exit('\n순위를 산출한 한국 종목이 없어 결과 파일을 만들지 않았습니다.')
    out = os.path.join(here, f'{"관심종목_한국" if only else "한국상장_모델순위"}_{date.today():%Y%m%d}.xlsx')
    title = f'{"관심종목 한국" if only else "시총 상위 한국 상장사"} 모델 분석 ({date.today().isoformat()} 산출, DART 사업보고서 최대 10개년)'
    note = (f'※ DART 사업보고서(연결 우선) 기준, 조회 {len(ranked) + len(excluded)}종목 중 순위 산출 {len(ranked)}종목, 제외 {len(excluded)}종목(금융업·데이터 부족 등, "제외·데이터부족" 시트). '
            f'시총은 USD 표를 환율 {fx:,.0f}원/$로 환산한 원화 기준 가격입니다.')
    limits = [
        '■ 범위: 시총 표(companiesmarketcap.com)의 한국 상장 종목 중 DART에 사업보고서가 있는 비금융 기업. 미국판과 같은 점수 엔진을 씁니다.',
        '■ 데이터: DART OpenAPI 단일회사 전체 재무제표(사업보고서). 연결 우선, 없으면 개별. 계정은 K-IFRS 계정 ID·계정명으로 매칭하므로 회사별 표기가 달라 누락될 수 있습니다(종목별 주의·지표 수 참조).',
        f'■ 가정(한국용): 무위험수익률 {KR_ASSUME["rf"]:.1%}, 주식위험프리미엄 {KR_ASSUME["erp"]:.0%}, 베타 1.0, 세율 {KR_ASSUME["tax"]:.0%}, 차입금 이자율 {KR_ASSUME["kd"]:.0%}, 최소 기대수익률 9%.',
        f'■ 환율: {fx:,.1f}원/$ 한 값으로 전 기간을 환산합니다(점수·괴리율은 환율과 무관, 시총·주가 표시에만 영향). 원 단위 가격은 시총(USD)×환율 ÷ 주식 수입니다.',
        '■ 오너이익 = 순이익(지배주주) + 감가상각 − 유지보수 투자 (유지보수 투자 = 감가상각 + 초과 설비투자의 50%). DART 현금흐름표에 감가상각이 없으면 순이익을 오너이익으로 사용합니다. 성장률은 매출 CAGR 0~10%로 제한.',
        '■ 미반영: 정성(텍스트) 신호, 경영진·공시 영역, 10년 전 시총(C1), 주식 수 변화(C2). 한국 특유의 지배구조 할인·우선주·지주회사 중복은 반영하지 않습니다.',
        '■ 한계: 차입금·감가상각·배당·자사주는 계정명 매칭이라 부정확할 수 있습니다. 순위는 참고용이며 투자 권유가 아닙니다.']
    write_xlsx(out, ranked, excluded, date.today().isoformat(), title=title, scope_note=note, limits=limits, fx=fx, cur='원')
    print(f'\n완료: 순위 {len(ranked)}종목, 제외 {len(excluded)}종목\n결과 파일: {out}')
    for k, r in enumerate(ranked[:10], 1):
        pr = f"  현재가 {r['mcap'] / r['shares_m'] * fx:,.0f}원" if r.get('shares_m') else ''
        fv = f"  적정가 {r['fv_usd'] / r['shares_m'] * fx:,.0f}원" if r.get('shares_m') and r['fv_usd'] else ''
        print(f"  {k:2d}. {r['ticker']:10s} {r['name'][:14]:14s} 점수 {r['composite']:.2f} ({r['tier'][1]}){pr}{fv}")
    try:
        os.startfile(out)
    except (AttributeError, OSError):
        pass
    return ranked, excluded


def ensure_csv(here, path):
    if os.path.exists(path):
        return
    print('\n시가총액 표(companies.csv)가 없어 자동으로 내려받아 봅니다...')
    try:
        download_csv(path); print('   내려받았습니다.')
    except Exception as e:  # noqa: BLE001
        print(f'   자동 다운로드 실패({e}).\n   직접 받아 주세요: 브라우저로 companiesmarketcap.com 접속 → 표 위의 다운로드(CSV) 아이콘 클릭')
        print(f'   → 받은 파일 이름을 companies.csv 로 바꿔 이 폴더({here})에 넣으세요.')
        ask('   넣었으면 Enter를 누르세요: ')
        if not os.path.exists(path):
            sys.exit('companies.csv를 찾지 못해 종료합니다.')


# ===== 세계 그 밖의 국가 (Yahoo Finance 기반 근사: 최근 4~5개 연간 재무) =====
WORLD_ROWS = {
    'revenue': (['Total Revenue', 'Operating Revenue'], 'is'),
    'operating_income': (['Operating Income', 'Total Operating Income As Reported', 'EBIT'], 'is'),
    'net_income': (['Net Income Common Stockholders', 'Net Income', 'Net Income From Continuing Operation Net Minority Interest'], 'is'),
    'interest_expense': (['Interest Expense', 'Interest Expense Non Operating'], 'is'),
    'dep_amort': (['Depreciation And Amortization', 'Depreciation Amortization Depletion', 'Depreciation'], 'cf'),
    'cfo': (['Operating Cash Flow', 'Cash Flow From Continuing Operating Activities'], 'cf'),
    'capex': (['Capital Expenditure', 'Purchase Of PPE'], 'cf'),
    'dividends_paid': (['Cash Dividends Paid', 'Common Stock Dividend Paid'], 'cf'),
    'buybacks': (['Repurchase Of Capital Stock', 'Common Stock Payments'], 'cf'),
    'total_assets': (['Total Assets'], 'bs'),
    'cash': (['Cash And Cash Equivalents', 'Cash Cash Equivalents And Short Term Investments'], 'bs'),
    'total_debt': (['Total Debt'], 'bs'),
    'equity': (['Stockholders Equity', 'Common Stock Equity', 'Total Equity Gross Minority Interest'], 'bs'),
    'shares_out': (['Ordinary Shares Number', 'Share Issued'], 'bs'),
}
ABS_KEYS = {'capex', 'dividends_paid', 'buybacks', 'interest_expense'}
FIN_LABELS = ('Deposits', 'Net Loan', 'Loans Receivable', 'Policyholder', 'Insurance Contract', 'Benefits And Claims')
COUNTRY_CUR = {'Japan': 'JPY', 'China': 'CNY', 'Hong Kong': 'HKD', 'Taiwan': 'TWD', 'United Kingdom': 'GBP', 'Germany': 'EUR', 'France': 'EUR',
               'Netherlands': 'EUR', 'Switzerland': 'CHF', 'Denmark': 'DKK', 'Sweden': 'SEK', 'Canada': 'CAD', 'Australia': 'AUD',
               'India': 'INR', 'Saudi Arabia': 'SAR', 'Brazil': 'BRL', 'Spain': 'EUR', 'Italy': 'EUR', 'Ireland': 'EUR', 'Singapore': 'SGD'}


def read_countries(path):
    rows = list(csv.reader(open(path, encoding='utf-8-sig')))
    hdr = [h.strip().lower() for h in rows[0]]
    isym = next((i for i, h in enumerate(hdr) if 'symbol' in h or 'ticker' in h), None)
    ictr = next((i for i, h in enumerate(hdr) if 'country' in h), None)
    if isym is None or ictr is None:
        return {}
    return {r[isym].strip().upper(): r[ictr].strip() for r in rows[1:] if len(r) > max(isym, ictr)}


def ensure_yfinance():
    try:
        import yfinance  # noqa: F401
        return
    except ImportError:
        pass
    print('\n[세계] Yahoo Finance 라이브러리(yfinance)가 없어 설치합니다. 처음 한 번만, 1~3분 걸립니다...')
    import subprocess
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', 'yfinance'])
    try:
        import yfinance  # noqa: F401
    except ImportError:
        sys.exit('yfinance 설치에 실패했습니다. 명령 창에서  pip install yfinance  를 직접 실행해 보세요.')


def _row(df, labels):
    if df is None or getattr(df, 'empty', True):
        return {}
    for lab in labels:
        if lab in df.index:
            ser = df.loc[lab]
            out = {}
            for col, v in ser.items():
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if f == f:  # NaN 제외
                    out[col.year] = f
            if out:
                return out
    return {}


def fx_to_usd(yf, cur, cache):
    if cur in (None, '', 'USD'):
        return 1.0
    if cur in cache:
        return cache[cur]
    rate = None
    for sym, inv in ((f'{cur}USD=X', False), (f'USD{cur}=X', True)):
        try:
            h = yf.Ticker(sym).history(period='5d')
            if len(h):
                v = float(h['Close'].iloc[-1])
                rate = (1 / v) if inv else v
                break
        except Exception:  # noqa: BLE001
            continue
    cache[cur] = rate
    return rate


def world_extract(yf, sym, country, fxcache):
    """-> (d, notes) 또는 (None, [사유]). 금액은 USD 백만."""
    t = yf.Ticker(sym)
    inc, bs, cf = t.income_stmt, t.balance_sheet, t.cashflow
    if getattr(inc, 'empty', True):
        return None, ['Yahoo에 재무제표 없음']
    if bs is not None and not getattr(bs, 'empty', True) and any(any(k in str(i) for k in FIN_LABELS) for i in bs.index):
        return None, ['금융업으로 추정(이 모델은 비금융 기업용)']
    cur = None
    try:
        info = t.info or {}
        cur = info.get('financialCurrency')
        if info.get('sector') == 'Financial Services':
            return None, ['금융·보험업(Yahoo 업종 분류): 이 모델은 비금융 기업용이라 제외']
    except Exception:  # noqa: BLE001
        cur = None
    cur = cur or COUNTRY_CUR.get(country)
    if not cur:
        return None, ['재무제표 통화를 알 수 없음']
    rate = fx_to_usd(yf, cur, fxcache)
    if not rate:
        return None, [f'환율({cur}→USD) 조회 실패']
    src = {'is': inc, 'bs': bs, 'cf': cf}
    raw = {k: _row(src[w], labels) for k, (labels, w) in WORLD_ROWS.items()}
    years = sorted(raw['revenue'])
    if not years:
        return None, ['매출 자료 없음']
    L = years[-1]
    yrs = list(range(L - 9, L + 1))
    d = {'years': yrs, 'gross_profit': [None] * 10}
    for k, m in raw.items():
        vals = []
        for y in yrs:
            v = m.get(y)
            if v is None:
                vals.append(None); continue
            if k == 'shares_out':
                vals.append(v / 1e6)
            else:
                if k == 'buybacks':
                    v = -v if v < 0 else 0.0     # 자사주 매입은 현금 유출(음수)만
                elif k in ABS_KEYS:
                    v = abs(v)
                vals.append(v * rate / 1e6)
        d[k] = vals
    n_have = sum(1 for y in yrs if raw['revenue'].get(y) is not None)
    notes = [f'Yahoo Finance 연간 재무 {n_have}개년({cur}→USD 현재환율 {rate:.4g}) · 근사치']
    return d, notes


FIN_NAME_RE = re.compile(r'insurance|assurance|reinsurance|\bbank\b|bancorp|bancshares|banco\b|banque|\bfinancial\b|\bsecurities\b|\bbrokers?\b|\binsurer', re.I)
HIGH_INFLATION = {'Turkey', 'Argentina', 'Venezuela', 'Egypt', 'Nigeria', 'Pakistan', 'Sri Lanka', 'Ghana', 'Ukraine', 'Russia', 'Lebanon', 'Iran', 'Zimbabwe', 'Ethiopia', 'Malawi'}


def run_world(here, csv_path, top, only=None, use_beta=False):
    ensure_yfinance()
    import yfinance as yf
    ctry = read_countries(csv_path)
    out = os.path.join(here, f'세계기타국가_모델순위_{date.today():%Y%m%d}.xlsx')
    allc = read_csv(csv_path)
    comps = [c for c in allc if ctry.get(c[0], '') not in ('USA', 'United States', 'South Korea', '')]
    comps = comps[:top] if only is None else [c for c in comps if c[0] in only]
    if not comps:
        print('\n[세계] 시총 표에 미국·한국 이외 종목(국가 열 필요)이 없습니다.'); return [], []
    cache = os.path.join(here, 'world_cache'); os.makedirs(cache, exist_ok=True)
    betas = get_betas([c[0] for c in comps], here) if use_beta else {}
    print(f'\n[세계] 미국·한국 이외 {len(comps)}개 종목을 Yahoo Finance로 분석합니다(최근 4~5개년, 근사치). 종목당 1~3초.\n')
    ranked, excluded, fxcache, stopped = [], [], {}, None
    try:
        for i, (sym, name, cap, px) in enumerate(comps, 1):
            if i % 50 == 0:
                print(f'   ... {i}/{len(comps)} 처리 중 (현재 순위 산출 {len(ranked)}종목)')
            if FIN_NAME_RE.search(name or ''):
                excluded.append((sym, name, '금융·보험업(회사명 기준): 이 모델은 비금융 기업용이라 제외')); continue
            if ctry.get(sym) in HIGH_INFLATION:
                excluded.append((sym, name, f'고인플레이션 국가({ctry.get(sym)}): 명목 이익·환율 왜곡으로 이 모델의 적정가가 부정확해 제외')); continue
            ep = os.path.join(cache, re.sub(r'[^A-Za-z0-9._-]', '_', sym) + '.json')
            try:
                if os.path.exists(ep):
                    d, notes = json.load(open(ep, encoding='utf-8'))
                else:
                    d, notes = world_extract(yf, sym, ctry.get(sym), fxcache)
                    json.dump([d, notes], open(ep, 'w', encoding='utf-8'), ensure_ascii=False)
                    time.sleep(0.6)
            except Exception as e:  # noqa: BLE001
                msg = f'{type(e).__name__} {e}'
                if 'RateLimit' in msg or 'Too Many Requests' in msg or '429' in msg:
                    stopped = f'{sym} 처리 중 Yahoo가 요청 과다로 잠시 차단했습니다.'; break
                excluded.append((sym, name, f'조회 실패: {msg[:150]}')); continue
            if d is None:
                excluded.append((sym, name, '; '.join(notes))); continue
            try:
                d, notes2 = check_and_fill(d, {}, min_years=4)
                if d is None:
                    excluded.append((sym, name, '; '.join(notes2))); continue
                notes = notes + notes2
                mcap_m = to_millions(cap)
                A_ = {'beta': betas[sym]} if betas.get(sym) else None
                r = evaluate(d, mcap_m, A=A_)
            except Exception as e:  # noqa: BLE001
                excluded.append((sym, name, f'계산 실패: {str(e)[:150]}')); continue
            shares_m = (mcap_m / px) if px else None
            label = f"{name} ({ctry.get(sym, '')})"
            add_scenarios(r, d, mcap_m, notes, A=A_)
            r.update(ticker=sym, name=label, mcap=mcap_m, fv_usd=r['fv_rep'], notes=notes, shares_m=shares_m)
            ranked.append(r)
            print(f'[{i}/{len(comps)}] {sym:10s} {name[:16]:16s} 점수 {r["composite"]:.2f}')
    except KeyboardInterrupt:
        stopped = '사용자가 중단했습니다.'
    if stopped:
        print(f'\n※ {stopped} 지금까지 {len(ranked)}종목으로 결과를 저장합니다. 받은 자료는 보관되어 다시 실행하면 이어집니다(Yahoo 차단이면 1시간쯤 뒤에).')
    ranked.sort(key=lambda x: -x['composite'])
    if not ranked:
        print('\n순위를 산출한 세계(기타) 종목이 없어 결과 파일을 만들지 않았습니다.'); return [], excluded
    limits = [
        '■ 범위: 시총 표(companiesmarketcap.com)에서 미국·한국을 뺀 나라의 종목. 미국은 SEC(10개년), 한국은 DART가 더 정확하니 그쪽 결과를 우선하세요.',
        '■ 데이터: Yahoo Finance(비공식) 연간 재무제표 최근 4~5개년. 연수가 짧아 점수는 연수에 맞춰 보정하며(해자 연수·이익 성장 연수 환산), 미국·한국 결과보다 신뢰도가 낮습니다.',
        '■ 통화: 재무제표 통화를 현재 환율로 USD 환산(전 기간 같은 환율). 시총·주가는 시총 표의 USD 값입니다. ADR·복수 상장은 주식 수가 어긋날 수 있습니다.',
        '■ 가정(전 국가 동일): 무위험수익률 4.2%, 주식위험프리미엄 5%, 베타 1.0, 세율 21%. 저금리국(일본·스위스)은 할인율이 높게, 고금리 신흥국은 낮게 잡혀 적정가가 왜곡될 수 있습니다.',
        '■ 오너이익 = 순이익 + 감가상각 − 유지보수 투자 (유지보수 투자 = 감가상각 + 초과 설비투자의 50%).',
        '■ 한계: 회계기준(IFRS/현지 기준)·계정 정의 차이, Yahoo 자료의 누락·오류 가능. 금융업 제외. 순위는 참고용이며 투자 권유가 아닙니다.']
    title = f'세계(미국·한국 제외) 시총 상위 종목 모델 순위 ({date.today().isoformat()} 산출, Yahoo Finance 4~5개년 근사)'
    note = (f'※ Yahoo Finance 기반 근사치입니다. 조회 {len(ranked) + len(excluded)}종목 중 순위 산출 {len(ranked)}종목, 제외 {len(excluded)}종목("제외·데이터부족" 시트). '
            '국가별 금리·세율 차이를 반영하지 않았고 데이터가 4~5개년뿐이라 신뢰도가 낮습니다.')
    write_xlsx(out, ranked, excluded, date.today().isoformat(), title=title, scope_note=note, limits=limits)
    print(f'\n완료: 순위 {len(ranked)}종목, 제외 {len(excluded)}종목\n결과 파일: {out}')
    for k, r in enumerate(ranked[:10], 1):
        print(f"  {k:2d}. {r['ticker']:10s} {r['name'][:24]:24s} 점수 {r['composite']:.2f} ({r['tier'][1]})")
    try:
        os.startfile(out)
    except (AttributeError, OSError):
        pass
    return ranked, excluded


# ===== 백테스트: 과거 시점의 점수로 그 뒤 수익률을 비교 (미국) =====
def load_extracted(cik, cache, ua):
    """run_us와 같은 방식으로 SEC 자료에서 추출한 10개년 재무(res, why)를 가져온다(보관본 우선)."""
    ext_dir = os.path.join(cache, 'ext'); os.makedirs(ext_dir, exist_ok=True)
    ep = os.path.join(ext_dir, f'{cik}.json')
    if os.path.exists(ep):
        cj = json.load(open(ep, encoding='utf-8'))
        return (tuple(cj[0]) if cj[0] else None), cj[1]
    rawp = os.path.join(cache, f'CIK{cik}.json')
    if os.path.exists(rawp):
        fj = json.load(open(rawp, encoding='utf-8'))
    else:
        fj = http_json(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json', ua)
        time.sleep(0.35)
    res, why = extract(fj)
    json.dump([list(res) if res else None, why], open(ep, 'w', encoding='utf-8'), ensure_ascii=False)
    return res, why


def truncate_asof(d, T):
    """회계연도 T 까지의 자료만 남긴다(그 시점에 알 수 있던 정보)."""
    keep = [i for i, y in enumerate(d['years']) if y <= T]
    out = {}
    for k, v in d.items():
        out[k] = [v[i] for i in keep] if isinstance(v, list) and len(v) == len(d['years']) else v
    return out


def run_backtest(here, email, top, horizon, csv_path, ua, cache):
    ensure_yfinance()
    import yfinance as yf
    import pandas as pd
    comps = read_csv(csv_path)[:top]
    tick = cached(os.path.join(cache, 'company_tickers.json'), 'https://www.sec.gov/files/company_tickers.json', ua)
    t2cik = {v['ticker'].upper(): str(v['cik_str']).zfill(10) for v in tick.values()}
    L = date.today().year - 1 if date.today().month >= 4 else date.today().year - 2
    T = L - horizon                       # 점수를 매기는 시점의 마지막 회계연도
    dec = date(T + 1, 3, 31)              # 그 10-K가 공개돼 있을 시점(다음 해 3월 말)
    print(f'\n[백테스트] 회계연도 {T}까지의 공시로 점수를 매기고, {dec} 가격으로 산 뒤 지금까지의 수익률을 비교합니다. (대상 상위 {len(comps)}종목)')
    rows, skipped = [], []
    cand = []
    bench = None
    for sym, name, cap, px in comps:
        cik = t2cik.get(sym) or t2cik.get(sym.replace('-', '.'))
        if not cik:
            continue
        try:
            res, why = load_extracted(cik, cache, ua)
        except (SecError, urllib.error.URLError, OSError) as e:
            if str(e) == 'RATE':
                print('   SEC가 요청 과다로 잠시 차단했습니다. 지금까지 받은 종목으로 진행합니다.'); break
            continue
        if res is None:
            continue
        d, used = res
        d, notes = check_and_fill(d, used)
        if d is None or T not in d['years']:
            continue
        dT = truncate_asof(d, T)
        if sum(1 for x in dT['revenue'] if x is not None) < 5:
            continue
        cand.append((sym, name, dT))
    print(f'   과거 점수를 계산할 수 있는 종목: {len(cand)}개. 주가를 내려받습니다...')
    start = f'{T + 1}-01-01'
    for i in range(0, len(cand), 60):
        chunk = cand[i:i + 60]
        syms = [c[0] for c in chunk]
        try:
            df = yf.download(syms + ['^GSPC'], start=start, interval='1mo', auto_adjust=False, progress=False, threads=True)
        except Exception as e:  # noqa: BLE001
            print(f'   주가 내려받기 실패({type(e).__name__}) - 이 묶음은 건너뜁니다'); continue
        close, adj = df['Close'], df['Adj Close']
        for sym, name, dT in chunk:
            try:
                c = close[sym].dropna(); a = adj[sym].dropna()
                c0 = c[(c.index.year == T + 1) & (c.index.month == 3)]
                if c0.empty or a.empty:
                    skipped.append((sym, '주가 없음')); continue
                px0, a0, a1 = float(c0.iloc[-1]), float(a[a.index <= c0.index[-1]].iloc[-1]), float(a.iloc[-1])
                yrs = (a.index[-1].to_pydatetime().date() - c0.index[-1].to_pydatetime().date()).days / 365.25 + 1 / 12
                sh = [x for x in dT['shares_out'] if x]
                if not sh:
                    skipped.append((sym, '당시 주식 수 없음')); continue
                try:
                    sp_ = yf.Ticker(sym).splits
                    fac = 1.0
                    for dt_, ratio in sp_.items():
                        if dt_.date() > dec and ratio:
                            fac *= float(ratio)
                except Exception:  # noqa: BLE001
                    fac = 1.0
                mcap0 = px0 * sh[-1] * fac        # 백만 달러(주가는 분할 반영, 주식 수는 분할 보정)
                r = evaluate(dT, mcap0, A={'no_bias': True})
                r35 = evaluate(dT, mcap0, A={'base_mode': 'min35', 'no_bias': True})
                rall = evaluate(dT, mcap0, A={'base_mode': 'all', 'no_bias': True})
                mg = r['tier'][2]
                fv = r['fv_rep']
                cagr = (a1 / a0) ** (1 / yrs) - 1
                pth = a[a.index >= c0.index[-1]]
                mdd = float((pth / pth.cummax() - 1).min()) if len(pth) > 1 else None   # 월간 가격 기준 최대 낙폭(일간보다 작게 나옴)
                rows.append(dict(ticker=sym, name=name, score=r['composite'], tier=r['tier'][1], mcap0=mcap0,
                                 fv_ratio=(fv / mcap0) if fv else None,
                                 fv35=(r35['fv_rep'] / mcap0) if r35['fv_rep'] else None, fv_all=(rall['fv_rep'] / mcap0) if rall['fv_rep'] else None,
                                 buy=(bool(fv and mg and fv * (1 - mg) >= mcap0)), cagr=cagr, yrs=yrs, irr=r.get('irr'), mdd=mdd,
                                 **{f's_{k_}': v_ for k_, v_ in r['scores'].items()},
                                 **{f'a_{k_}': v_ for k_, v_ in r['area'].items() if v_ is not None}))
            except Exception as e:  # noqa: BLE001
                skipped.append((sym, f'{type(e).__name__}: {str(e)[:60]}'))
        try:
            m_ = close['^GSPC'].dropna(); ma = adj['^GSPC'].dropna()
            m0 = float(ma[ma.index <= m_[(m_.index.year == T + 1) & (m_.index.month == 3)].index[-1]].iloc[-1])
            bench = (float(ma.iloc[-1]) / m0) ** (1 / (((ma.index[-1].to_pydatetime().date() - date(T + 1, 3, 31)).days / 365.25) + 1 / 12)) - 1
        except Exception:  # noqa: BLE001
            bench = None
        time.sleep(1.0)
    if len(rows) < 10:
        sys.exit(f'\n비교할 종목이 {len(rows)}개뿐이라 백테스트를 만들지 않았습니다. 종목 수를 늘리거나(예: 300) 주가 내려받기 오류를 확인하세요.')
    df = pd.DataFrame(rows)
    write_backtest(here, df, skipped, T, dec, horizon, bench)


def write_backtest(here, df, skipped, T, dec, horizon, bench):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    out = os.path.join(here, f'백테스트_미국_{horizon}년_{date.today():%Y%m%d}.xlsx')
    wb = Workbook(); ws = wb.active; ws.title = '요약'
    F = lambda **k: Font(name='맑은 고딕', size=10, **k)  # noqa: E731
    hdr = PatternFill('solid', fgColor='1F3864')

    def head(w, row, labels):
        for i, t in enumerate(labels):
            c = w.cell(row=row, column=1 + i, value=t); c.font = F(bold=True, color='FFFFFF'); c.fill = hdr
            c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    ws['A1'] = f'백테스트 (미국): 회계연도 {T}까지의 공시로 점수 → {dec} 매수 → 오늘까지 연환산 수익률(배당 포함)'
    ws['A1'].font = Font(name='맑은 고딕', size=14, bold=True)
    spear = df['score'].rank().corr(df['cagr'].rank())
    spear2 = df['fv_ratio'].rank().corr(df['cagr'].rank()) if df['fv_ratio'].notna().sum() > 5 else None
    ws['A2'] = (f'종목 {len(df)}개 · 평균 보유 {df["yrs"].mean():.1f}년 · 점수↔수익률 순위상관 {spear:+.2f}'
                + (f' · (적정가÷당시가격)↔수익률 순위상관 {spear2:+.2f}' if spear2 is not None else '')
                + (f' · S&P500 연 {bench:.1%}' if bench is not None else ''))
    ws['A2'].font = F(bold=True, color='1F3864')
    ws['A3'] = ('⚠ 생존 편향: 지금도 시총 상위로 살아남은 종목만 포함되어 전체 수익률이 실제보다 좋게 보이고, 탈락·상장폐지 종목은 빠져 있습니다. '
                '그래서 절대 수익률보다 "점수 구간 간 차이"(높은 점수가 낮은 점수보다 나았는가)만 의미가 있습니다. 5년 표본은 시장 국면(금리·AI 장세)에 크게 좌우됩니다.')
    ws['A3'].font = F(italic=True, color='C00000'); ws.merge_cells('A3:H3'); ws['A3'].alignment = Alignment(wrap_text=True, vertical='top'); ws.row_dimensions[3].height = 48
    head(ws, 5, ['구분', '종목 수', '평균 연수익률', '중앙값 연수익률', '시장(S&P500) 초과 비율', '평균 초과수익(%p)'])
    row = 6

    def block(title, groups):
        nonlocal row
        c = ws.cell(row=row, column=1, value=title); c.font = F(bold=True); c.fill = PatternFill('solid', fgColor='D9E1F2'); row += 1
        for lab, sub in groups:
            if len(sub) == 0:
                continue
            vals = [lab, len(sub), sub['cagr'].mean(), sub['cagr'].median(),
                    (sub['cagr'] > bench).mean() if bench is not None else None,
                    (sub['cagr'].mean() - bench) * 100 if bench is not None else None]
            for j, v in enumerate(vals):
                c = ws.cell(row=row, column=1 + j, value=v if v is not None else '-'); c.font = F()
                if j in (2, 3, 4): c.number_format = '0.0%'
                if j == 5: c.number_format = '+0.0;-0.0'
            row += 1
    q = pd_qcut(df['score'], 5)
    block('A. 종합점수 5분위 (1=최저 … 5=최고)', [(f'{k}분위', df[q == k]) for k in range(1, 6)])
    block('B. 등급', [(t, df[df['tier'] == t]) for t in ['우수', '양호', '보수적 검토', '투자 제외']])
    block('C. 당시 가격 vs 적정가 (적정가÷당시 시총)', [('적정가 ≥ 시총 (싸다)', df[df['fv_ratio'] >= 1]),
                                                   ('0.5 ≤ 비율 < 1', df[(df['fv_ratio'] >= 0.5) & (df['fv_ratio'] < 1)]),
                                                   ('비율 < 0.5 (많이 비쌈)', df[df['fv_ratio'] < 0.5]), ('적정가 산정 불가', df[df['fv_ratio'].isna()])])
    block('D. 매수 기준 충족 여부 (가격 ≤ 안전마진 반영 기준가)', [('충족', df[df['buy']]), ('미충족', df[~df['buy']])])
    for kk, w in zip('ABCDEF', [44, 10, 15, 15, 20, 18]):
        ws.column_dimensions[kk].width = w
    w2 = wb.create_sheet('종목별')
    head(w2, 1, ['티커', '종목', f'점수({T}년 말 기준)', '등급', '당시 시총 ($M)', '적정가÷시총', '매수 기준 충족', '연환산 수익률', '보유 연수'])
    for i, r in enumerate(df.sort_values('score', ascending=False).itertuples(), 2):
        vals = [r.ticker, r.name, r.score, r.tier, r.mcap0, r.fv_ratio, '예' if r.buy else '아니오', r.cagr, r.yrs]
        fm = [None, None, '0.00', None, '#,##0', '0.00', None, '0.0%', '0.0']
        for j, (v, f) in enumerate(zip(vals, fm)):
            c = w2.cell(row=i, column=1 + j, value=v if v == v and v is not None else '-'); c.font = F()
            if f: c.number_format = f
    for kk, w in zip('ABCDEFGHI', [10, 28, 16, 12, 14, 12, 14, 14, 10]):
        w2.column_dimensions[kk].width = w
    wz = wb.create_sheet('크기·위험 검증')
    wz['A1'] = '크기·위험 검증: (1) 같은 크기 종목끼리 비교해도 점수 효과가 같은가  (2) 점수가 높은 종목이 덜 떨어졌는가'
    wz['A1'].font = Font(name='맑은 고딕', size=12, bold=True)
    wz['A2'] = ('시가총액 구간은 매수 시점 시총으로 3등분했습니다. 최대 낙폭은 월간 가격 기준이라 실제(일간)보다 작게 나옵니다. '
                '구간별 표본이 작으면(종목 수 참고) 해석하지 마세요.')
    wz['A2'].font = F(italic=True, color='C00000'); wz.merge_cells('A2:I2'); wz['A2'].alignment = Alignment(wrap_text=True, vertical='top'); wz.row_dimensions[2].height = 32
    head(wz, 4, ['구분', '종목 수', '순위상관(점수↔수익률)', '점수 상위 1/3 평균 연수익률', '점수 하위 1/3 평균 연수익률', '차이(%p)', '상위 1/3 평균 최대낙폭', '하위 1/3 평균 최대낙폭', '판정'])
    zr = 5

    def zrow(lab, sub):
        nonlocal zr
        if len(sub) < 12:
            vals = [lab, len(sub)] + ['-'] * 6 + ['표본 부족']
        else:
            rho = sub['score'].rank().corr(sub['cagr'].rank()); k3 = len(sub) // 3
            srt = sub.sort_values('score', kind='mergesort'); lo_, hi_ = srt.iloc[:k3], srt.iloc[-k3:]
            th = 2 / len(sub) ** 0.5
            vals = [lab, len(sub), rho, hi_['cagr'].mean(), lo_['cagr'].mean(), (hi_['cagr'].mean() - lo_['cagr'].mean()) * 100,
                    hi_['mdd'].mean(), lo_['mdd'].mean(), '의미 있음(+)' if rho > th else ('의미 있음(−, 역방향)' if rho < -th else '구분 안 됨')]
        for j, (v, f) in enumerate(zip(vals, [None, None, '+0.00;-0.00', '0.0%', '0.0%', '+0.0;-0.0', '0.0%', '0.0%', None])):
            c = wz.cell(row=zr, column=1 + j, value=v if (v is not None and v == v) else '-'); c.font = F()
            if f and not isinstance(v, str): c.number_format = f
        zr += 1
    c = wz.cell(row=zr, column=1, value='A. 시가총액 구간별 (같은 크기끼리 점수 효과)'); c.font = F(bold=True); c.fill = PatternFill('solid', fgColor='D9E1F2'); zr += 1
    qs = pd_qcut(df['mcap0'], 3)
    zrow('전체', df)
    for k_, nm_ in ((3, '대형(상위 1/3)'), (2, '중형'), (1, '소형(하위 1/3)')):
        zrow(nm_, df[qs == k_])
    c = wz.cell(row=zr, column=1, value='B. 점수 5분위별 위험 (낙폭·손실 비율)'); c.font = F(bold=True); c.fill = PatternFill('solid', fgColor='D9E1F2'); zr += 1
    head(wz, zr, ['구분', '종목 수', '평균 최대낙폭', '최대낙폭 -40% 이하 비율', '연수익률 하위 10% 경계', '수익률 음수 비율']); zr += 1
    q5 = pd_qcut(df['score'], 5)
    for k_ in range(1, 6):
        sub = df[q5 == k_]
        vals = [f'{k_}분위', len(sub), sub['mdd'].mean(), (sub['mdd'] <= -0.40).mean(), sub['cagr'].quantile(0.10), (sub['cagr'] < 0).mean()]
        for j, (v, f) in enumerate(zip(vals, [None, None, '0.0%', '0.0%', '0.0%', '0.0%'])):
            c = wz.cell(row=zr, column=1 + j, value=v if (v is not None and v == v) else '-'); c.font = F()
            if f: c.number_format = f
        zr += 1
    for kk, w in zip('ABCDEFGHI', [34, 10, 16, 18, 18, 12, 18, 18, 20]):
        wz.column_dimensions[kk].width = w
    wv = wb.create_sheet('가치평가 검증')
    wv['A1'] = '가치평가 검증: 적정가가 시총보다 높을수록(싸 보일수록), 내재 수익률이 높을수록 실제 수익률도 높았는가'
    wv['A1'].font = Font(name='맑은 고딕', size=12, bold=True)
    wv['A2'] = ('시가총액 구간은 매수 시점 시총 3등분입니다. ⚠ 소형 구간은 "지금 시총 상위로 올라온" 종목만 남은 생존 편향이 가장 커서 신뢰하지 마세요. '
                '판단은 대형·중형 결과 위주로 하세요. 적정가 산정 불가 종목은 제외하고 계산합니다.')
    wv['A2'].font = F(italic=True, color='C00000'); wv.merge_cells('A2:H2'); wv['A2'].alignment = Alignment(wrap_text=True, vertical='top'); wv.row_dimensions[2].height = 44
    head(wv, 4, ['구분', '종목 수', '적정가÷시총 ↔ 수익률 순위상관', '판정', '내재 수익률 ↔ 수익률 순위상관', '판정', '내재 수익률 상위 1/3 평균 연수익률', '내재 수익률 하위 1/3 평균 연수익률'])
    vr = 5
    qv = pd_qcut(df['mcap0'], 3)

    def judge_(rho, n):
        if rho != rho or n < 12:
            return '표본 부족'
        th = 2 / n ** 0.5
        return '의미 있음(+)' if rho > th else ('의미 있음(−, 역방향)' if rho < -th else '구분 안 됨')

    def vrow(lab, sub):
        nonlocal vr
        a_ = sub[sub['fv_ratio'].notna()]; b_ = sub[sub['irr'].notna()]
        r1 = a_['fv_ratio'].rank().corr(a_['cagr'].rank()) if len(a_) >= 12 else None
        r2 = b_['irr'].rank().corr(b_['cagr'].rank()) if len(b_) >= 12 else None
        hi_m = lo_m = None
        if len(b_) >= 12:
            k3 = len(b_) // 3; sb = b_.sort_values('irr', kind='mergesort')
            lo_m, hi_m = sb['cagr'].iloc[:k3].mean(), sb['cagr'].iloc[-k3:].mean()
        vals = [lab, f'{len(a_)} / {len(b_)}', r1, judge_(r1, len(a_)) if r1 is not None else '표본 부족', r2, judge_(r2, len(b_)) if r2 is not None else '표본 부족', hi_m, lo_m]
        for j, (v, f) in enumerate(zip(vals, [None, None, '+0.00;-0.00', None, '+0.00;-0.00', None, '0.0%', '0.0%'])):
            c = wv.cell(row=vr, column=1 + j, value=v if (v is not None and v == v) else '-'); c.font = F()
            if f and not isinstance(v, str): c.number_format = f
        vr += 1
    c = wv.cell(row=vr, column=1, value='A. 순위상관 (종목 수는 적정가 / 내재수익률 기준)'); c.font = F(bold=True); c.fill = PatternFill('solid', fgColor='D9E1F2'); vr += 1
    vrow('전체', df)
    for k_, nm_ in ((3, '대형(상위 1/3)'), (2, '중형'), (1, '소형(하위 1/3) ⚠편향 큼')):
        vrow(nm_, df[qv == k_])
    vr += 1
    c = wv.cell(row=vr, column=1, value='B. 적정가÷시총 구간별 평균 연수익률 (구간: 적정가≥시총 / 0.5~1 / 0.5 미만)'); c.font = F(bold=True); c.fill = PatternFill('solid', fgColor='D9E1F2'); vr += 1
    head(wv, vr, ['시가총액 구간', '적정가≥시총', '0.5~1', '0.5 미만', '(종목 수)', '', '', '']); vr += 1
    for nm_, sub in (('전체', df), ('대형', df[qv == 3]), ('중형', df[qv == 2]), ('소형 ⚠', df[qv == 1])):
        g1_ = sub[sub['fv_ratio'] >= 1]; g2_ = sub[(sub['fv_ratio'] >= 0.5) & (sub['fv_ratio'] < 1)]; g3_ = sub[sub['fv_ratio'] < 0.5]
        vals = [nm_] + [(g['cagr'].mean() if len(g) >= 5 else '표본 부족') for g in (g1_, g2_, g3_)] + [f'{len(g1_)} / {len(g2_)} / {len(g3_)}']
        for j, v in enumerate(vals):
            c = wv.cell(row=vr, column=1 + j, value=v); c.font = F()
            if j in (1, 2, 3) and not isinstance(v, str): c.number_format = '0.0%'
        vr += 1
    for kk, w in zip('ABCDEFGH', [36, 14, 22, 18, 22, 18, 22, 22]):
        wv.column_dimensions[kk].width = w
    wn = wb.create_sheet('정상화 비교')
    wn['A1'] = '정상화 비교: 기준 이익을 어떻게 잡을 때 "적정가÷시총"이 이후 수익률과 더 잘 맞았는가'
    wn['A1'].font = Font(name='맑은 고딕', size=12, bold=True)
    wn['A2'] = ('A=현재 방식(최근 3년 평균, 변동이 크면 5년과 비교해 낮은 값)  B=3년·5년 평균 중 항상 낮은 값  C=가능한 전체 연도 평균(이 백테스트에서는 6~9년). '
                '순위상관이 클수록 그 방식의 "싸 보임"이 실제 수익률과 더 잘 맞았다는 뜻입니다. ⚠ 소형은 생존 편향이 커서 참고만 하세요. 차이가 ±0.05 안이면 방식 간 우열이 없는 것입니다.')
    wn['A2'].font = F(italic=True, color='C00000'); wn.merge_cells('A2:I2'); wn['A2'].alignment = Alignment(wrap_text=True, vertical='top'); wn.row_dimensions[2].height = 48
    head(wn, 4, ['시가총액 구간', '방식', '종목 수', '순위상관(적정가÷시총↔수익률)', '"싸다(≥1)" 종목 수', '"싸다" 종목 평균 연수익률', '나머지 평균 연수익률', '판정'])
    nr = 5
    qn = pd_qcut(df['mcap0'], 3)
    for nm_, sub in (('전체', df), ('대형', df[qn == 3]), ('중형', df[qn == 2]), ('소형 ⚠', df[qn == 1])):
        for mode_, col in (('A 현재(3년)', 'fv_ratio'), ('B 3·5년 중 낮은 값', 'fv35'), ('C 전체 평균', 'fv_all')):
            sb = sub[sub[col].notna()]
            if len(sb) < 12:
                vals = [nm_, mode_, len(sb)] + ['-'] * 4 + ['표본 부족']
            else:
                rho = sb[col].rank().corr(sb['cagr'].rank()); th = 2 / len(sb) ** 0.5
                ch = sb[sb[col] >= 1]; rest = sb[sb[col] < 1]
                vals = [nm_, mode_, len(sb), rho, len(ch), ch['cagr'].mean() if len(ch) >= 5 else '표본 부족', rest['cagr'].mean() if len(rest) else '-',
                        '의미 있음(+)' if rho > th else ('의미 있음(−, 역방향)' if rho < -th else '구분 안 됨')]
            for j, (v, f) in enumerate(zip(vals, [None, None, None, '+0.00;-0.00', None, '0.0%', '0.0%', None])):
                c = wn.cell(row=nr, column=1 + j, value=v if (v is not None and v == v) else '-'); c.font = F()
                if f and not isinstance(v, str): c.number_format = f
            nr += 1
    for kk, w in zip('ABCDEFGH', [14, 22, 10, 26, 18, 22, 20, 20]):
        wn.column_dimensions[kk].width = w
    wm = wb.create_sheet('지표별 검증')
    NAMES = {'M1': 'ROIC>자본비용인 해의 비율', 'M2': '영업이익률 변동성(낮을수록 좋음)', 'M3': '영업이익률 최대 하락폭(낮을수록 좋음)', 'M4': '평균 ROIC',
             'Q1': '영업CF÷순이익', 'Q2': '발생액 비율(낮을수록 좋음)', 'Q3': '순이익 증가 해의 비율', 'C2': '주식 수 변화율(감소할수록 좋음)',
             'C3': '배당·자사주 지급 해의 비율', 'C4': '증분 투하자본수익', 'F1': '순차입금÷영업CF(낮을수록 좋음)', 'F2': '이자보상배율', 'F3': '부채÷순이익(낮을수록 좋음)'}
    wm['A1'] = '지표별 검증: 각 지표 점수가 높았던 종목이 이후 수익률도 높았는가 (점수는 이미 "좋을수록 높게" 변환된 값)'
    wm['A1'].font = Font(name='맑은 고딕', size=12, bold=True)
    wm['A2'] = (f'표본 {len(df)}개에서는 순위상관이 ±{2 / max(len(df), 1) ** 0.5:.2f} 안이면 우연과 구분되지 않습니다(표시 "의미 있음"은 그 바깥). '
                '지표 수가 많아 우연히 큰 값이 나올 수도 있으니, 한 번의 결과로 가중치를 바꾸지 마세요. 보유기간 2~5년 결과는 같은 최근 시장 국면을 공유하므로 서로 독립된 검증이 아닙니다.')
    wm['A2'].font = F(italic=True, color='C00000'); wm.merge_cells('A2:H2'); wm['A2'].alignment = Alignment(wrap_text=True, vertical='top'); wm.row_dimensions[2].height = 44
    head(wm, 4, ['구분', '설명', '종목 수', '순위상관(점수↔수익률)', '상위 1/3 평균 연수익률', '하위 1/3 평균 연수익률', '차이(%p)', '판정'])
    thr = 2 / max(len(df), 1) ** 0.5
    rr = 5
    items = [(f'영역: {a}', '영역 평균 점수', f'a_{a}') for a in AREAS] + [(k_, NAMES[k_], f's_{k_}') for k_ in NAMES]
    for lab, ds, col in items:
        if col not in df.columns:
            continue
        sub = df[df[col].notna()]
        if len(sub) < 10:
            continue
        rho = sub[col].rank().corr(sub['cagr'].rank())
        const = sub[col].nunique() < 3
        k3 = max(1, len(sub) // 3)
        srt = sub.sort_values(col, kind='mergesort')
        lo, hi = srt['cagr'].iloc[:k3].mean(), srt['cagr'].iloc[-k3:].mean()
        verdict = ('값 차이 거의 없음(판정 불가)' if const or rho != rho else ('의미 있음(+)' if rho > thr else ('의미 있음(−, 역방향)' if rho < -thr else '구분 안 됨')))
        for j, (v, f) in enumerate(zip([lab, ds, len(sub), None if const else rho, None if const else hi, None if const else lo, None if const else (hi - lo) * 100, verdict], [None, None, None, '+0.00;-0.00', '0.0%', '0.0%', '+0.0;-0.0', None])):
            c = wm.cell(row=rr, column=1 + j, value=v if (v is not None and v == v) else '-'); c.font = F()
            if f: c.number_format = f
        rr += 1
    for kk, w in zip('ABCDEFGH', [22, 38, 10, 16, 16, 16, 12, 20]):
        wm.column_dimensions[kk].width = w
    w3 = wb.create_sheet('제외')
    head(w3, 1, ['티커', '사유'])
    for i, (t, why) in enumerate(skipped, 2):
        w3.cell(row=i, column=1, value=t).font = F(); w3.cell(row=i, column=2, value=why).font = F()
    w3.column_dimensions['B'].width = 60
    for s in wb.worksheets:
        s.sheet_view.showGridLines = False
    wb.save(out)
    print(f'\n완료: 백테스트 {len(df)}종목 · 점수↔수익률 순위상관 {spear:+.2f}\n결과 파일: {out}')
    try:
        os.startfile(out)
    except (AttributeError, OSError):
        pass


def pd_qcut(series, q):
    import pandas as pd
    return pd.qcut(series.rank(method='first'), q, labels=range(1, q + 1)).astype(int)


def write_combined(here, done):
    ranked = sorted([r for _, (rk, _) in done for r in rk], key=lambda x: -x['composite'])
    okset = {r['ticker'] for r in ranked}
    exd = {}
    for _, (_, ex) in done:           # 나중 출처(한국·세계)의 사유가 앞선 'SEC 미등록'을 덮어쓴다
        for t, nm, why in ex:
            if t not in okset:
                exd[t] = (t, nm, why)
    excluded = list(exd.values())
    out = os.path.join(here, f'세계통합_모델순위_{date.today():%Y%m%d}.xlsx')
    parts = ' + '.join(f'{n} {len(rk)}종목' for n, (rk, _) in done)
    title = f'세계 통합 모델 순위 ({date.today().isoformat()} 산출: {parts})'
    note = ('※ 미국(SEC 10개년)·한국(DART)·그 밖의 나라(Yahoo 4~5개년 근사)를 한 표로 합쳤습니다. 데이터 신뢰도가 다르니 [종목별 주의]와 "데이터 연수"를 확인하고, '
            '가격은 모두 USD(시총 표 기준)입니다. 한국 종목의 원화 가격은 한국 결과 파일을 보세요.')
    limits = ['■ 합친 결과입니다: 미국=SEC 10-K 최대 10개년(가장 정확), 한국=DART 사업보고서(한국용 할인율 가정), 그 밖=Yahoo Finance 4~5개년(근사). 가정은 각 개별 파일의 [가정·한계] 시트를 보세요.',
              '■ 국가별 금리·세율·회계기준 차이가 완전히 반영되지 않아 국가 간 비교는 거칠게만 의미가 있습니다. 순위는 참고용이며 투자 권유가 아닙니다.']
    write_xlsx(out, ranked, excluded, date.today().isoformat(), title=title, scope_note=note, limits=limits)
    print(f'\n세계 통합 결과: {out}  ({parts})')
    try:
        os.startfile(out)
    except (AttributeError, OSError):
        pass


def run_diag(here, key, fx):
    """한국(DART)·세계(Yahoo) 연결을 대표 종목 몇 개로 점검하고 진단결과 txt 를 만든다(인증키는 기록하지 않음)."""
    log = []

    def P(t=''):
        print(t); log.append(t)
    P(f'=== 진단 ({date.today().isoformat()}, 버전 v47) ===')
    keys = ['revenue', 'operating_income', 'net_income', 'dep_amort', 'cfo', 'capex', 'total_assets', 'cash', 'total_debt', 'equity', 'interest_expense', 'shares_out', 'dividends_paid', 'buybacks']

    def summarize(name, d, notes):
        yrs = d['years']
        P(f'  [{name}] 연도 {yrs[0]}~{yrs[-1]}  ' + ' / '.join(notes)[:150])
        P('     항목별 채워진 연수: ' + ', '.join(f"{k} {sum(1 for x in d.get(k, []) if x is not None)}" for k in keys))
        dd, nn = check_and_fill(d, {}, min_years=4)
        if dd is None:
            P(f'     → 계산 불가: {nn}'); return
        try:
            r = evaluate(dd, 100000.0)
            P(f'     → 점수 {r["composite"]:.2f} ({r["tier"][1]}) · 적정 시총 {("%.0f" % r["fv_rep"]) if r["fv_rep"] else "산정불가"} 백만$ (시총 1000억$ 가정) · 지표 {len(r["scores"])}/{len(PARAMS)}')
        except Exception as e:  # noqa: BLE001
            P(f'     → 평가 오류 {type(e).__name__}: {str(e)[:100]}')
    P('\n[1] 세계(Yahoo Finance)')
    try:
        ensure_yfinance()
        import yfinance as yf
        P('  yfinance 불러오기: 성공')
        fxc = {}
        for sym, ctry in (('7203.T', 'Japan'), ('2330.TW', 'Taiwan'), ('NESN.SW', 'Switzerland')):
            try:
                d, notes = world_extract(yf, sym, ctry, fxc)
                if d is None:
                    P(f'  [{sym}] 실패: {notes}')
                else:
                    summarize(sym, d, notes)
            except Exception as e:  # noqa: BLE001
                P(f'  [{sym}] 오류 {type(e).__name__}: {str(e)[:150]}')
    except Exception as e:  # noqa: BLE001
        P(f'  yfinance 사용 불가: {type(e).__name__}: {str(e)[:150]}')
    P('\n[2] 한국(DART)')
    if not key:
        P('  인증키를 입력하지 않아 건너뜁니다.')
    else:
        cache = os.path.join(here, 'dart_cache'); os.makedirs(cache, exist_ok=True)
        try:
            codes = load_corp_codes(key, cache)
            P(f'  기업 코드표: 성공 ({len(codes):,}개 상장사)')
            for sc in ('005930', '000660', '005380'):
                if sc not in codes:
                    P(f'  [{sc}] 코드표에 없음'); continue
                try:
                    d, notes = build_company(key, codes[sc][0], cache, fx)
                    if d is None:
                        P(f'  [{sc} {codes[sc][1]}] 실패: {notes}')
                    else:
                        notes = notes + kr_fill_da(d, sc + '.KS', fx)
                        summarize(f'{sc} {codes[sc][1]}', d, notes)
                except Exception as e:  # noqa: BLE001
                    P(f'  [{sc}] 오류 {type(e).__name__}: {str(e)[:150]}')
            P('\n[3] 차입금 점검 (재무상태표의 차입·사채·부채 계정 vs 도구가 합산한 값, 단위 억원)')
            today_ = date.today(); L_ = today_.year - 1 if today_.month >= 4 else today_.year - 2
            for sc in ('096770', '034730', '028260', '012330', '207940', '010950', '005380'):
                if sc not in codes:
                    continue
                try:
                    cc_, nm_ = codes[sc]
                    rows_, fs_ = fetch_fs(key, cc_, L_, cache)
                    if rows_ is None:
                        rows_, fs_ = fetch_fs(key, cc_, L_ - 1, cache)
                    if rows_ is None:
                        P(f'  [{sc} {nm_}] 재무제표 없음'); continue
                    d_, nt_ = build_company(key, cc_, cache, fx)
                    mine = (d_['total_debt'][-1] * 1e6 * fx / 1e8) if d_ and d_['total_debt'][-1] is not None else None
                    P(f'  [{sc} {nm_}] 도구 합산 차입금: {mine:,.0f}억' if mine is not None else f'  [{sc} {nm_}] 도구 합산 차입금: 없음')
                    for r_ in rows_:
                        if r_.get('sj_div') == 'BS' and (r_.get('account_id') in DEBT_IDS or nm_norm(r_.get('account_nm')) in DEBT_NAMES) and vals(r_)[0] is not None:
                            P(f"   ✓합산 {nm_norm(r_.get('account_nm'))[:24]:24s} {vals(r_)[0] / 1e8:>12,.0f}억   id={str(r_.get('account_id'))[:42]}")
                    for r_ in rows_:
                        if r_.get('sj_div') != 'BS':
                            continue
                        n_ = nm_norm(r_.get('account_nm'))
                        v_ = vals(r_)[0]
                        if v_ is not None and any(k in n_ for k in ('차입', '사채', '부채', '리스')) and abs(v_) >= 1e10:
                            P(f"     {n_[:24]:24s} {v_ / 1e8:>12,.0f}억   id={str(r_.get('account_id'))[:42]}")
                except Exception as e:  # noqa: BLE001
                    P(f'  [{sc}] 오류 {type(e).__name__}: {str(e)[:120]}')
        except Exception as e:  # noqa: BLE001
            P(f'  DART 접속 실패: {type(e).__name__}: {str(e)[:150]}')
    out = os.path.join(here, f'진단결과_{date.today():%Y%m%d}.txt')
    open(out, 'w', encoding='utf-8-sig').write('\n'.join(log))
    print(f'\n진단결과 파일: {out}\n이 화면 내용 전체(또는 위 파일)를 복사해서 보내 주세요. (인증키는 기록되지 않습니다)')


def run_calib(here):
    """받아 둔 SEC 10개년 자료로, 같은 회사를 '최근 4개년만' 쓸 때 점수가 얼마나 달라지는지 잰다(Yahoo 4개년 결과의 편향 점검). 인터넷 불필요."""
    import glob
    files = glob.glob(os.path.join(here, 'sec_cache', 'ext', '*.json'))
    if not files:
        sys.exit('\n받아 둔 SEC 자료(sec_cache)가 없습니다. 메뉴 1(미국 순위)을 먼저 한 번 실행하세요.')
    rows, mdiff = [], {}
    for f in files:
        try:
            res, _ = json.load(open(f, encoding='utf-8'))
            if not res:
                continue
            d, used = res
            d, _n = check_and_fill(d, used)
            if d is None or len(d['years']) < 8:
                continue
            ni3 = [x for x in d['net_income'][-3:] if x is not None]
            m0 = max(1000.0, 15 * (sum(ni3) / len(ni3))) if ni3 else 1000.0
            d4 = {k: (v[-4:] if isinstance(v, list) and len(v) == len(d['years']) else v) for k, v in d.items()}
            a, b = evaluate(d, m0, A={'no_bias': True}), evaluate(d4, m0, A={'no_bias': True})
            bc = evaluate(d4, m0)
        except Exception:  # noqa: BLE001
            continue
        rows.append((a['composite'], b['composite'], a['tier'][1], b['tier'][1], bc['composite'], bc['tier'][1]))
        for k_, v_ in b['scores'].items():
            if k_ in a['scores']:
                mdiff.setdefault(k_, []).append(v_ - a['scores'][k_])
    if len(rows) < 30:
        sys.exit(f'\n비교할 종목이 {len(rows)}개뿐입니다(10개년 자료가 있는 종목 30개 이상 필요). 메뉴 1을 더 많은 종목으로 실행하세요.')
    full = [r[0] for r in rows]; four = [r[1] for r in rows]
    n = len(rows); mean_d = sum(b - a for a, b in zip(full, four)) / n
    rk = lambda xs: [sorted(xs).index(x) for x in xs]  # noqa: E731
    import statistics as _st
    try:
        rho = _st.correlation(rk(full), rk(four))
    except Exception:  # noqa: BLE001
        rho = float('nan')
    tiers = ['투자 제외', '보수적 검토', '양호', '우수']
    mig = {(x, y): 0 for x in tiers for y in tiers}
    for r in rows:
        mig[(r[2], r[3])] += 1
    lines = [f'=== 4개년 점수 편향 점검 ({date.today().isoformat()}, 10개년 자료가 있는 미국 {n}종목) ===',
             f'평균 점수: 10개년 {sum(full) / n:.2f} → 최근 4개년만 사용 {sum(four) / n:.2f} (평균 {mean_d:+.2f}점)',
             f'순위 상관(10개년 점수 ↔ 4개년 점수): {rho:+.2f}',
             f'4.5점 이상 비율: 10개년 {sum(1 for x in full if x >= 4.5) / n:.1%} → 4개년 {sum(1 for x in four if x >= 4.5) / n:.1%}',
             f'등급 이동(행=10개년 등급, 열=4개년 등급):']
    lines.append('            ' + ' '.join(f'{t:>8s}' for t in tiers))
    for x in tiers:
        lines.append(f'{x:>10s}  ' + ' '.join(f'{mig[(x, y)]:>8d}' for y in tiers))
    cor = [r[4] for r in rows]
    lines.append(f'보정 적용 후(v30): 4개년 평균 점수 {sum(cor) / n:.2f}(10개년 {sum(full) / n:.2f}), 4.5점 이상 {sum(1 for x in cor if x >= 4.5) / n:.1%}, '
                 f'우수 등급 {sum(1 for r in rows if r[5] == "우수")}개(10개년 {sum(1 for r in rows if r[2] == "우수")}개, 보정 전 4개년 {sum(1 for r in rows if r[3] == "우수")}개)')
    lines.append('지표별 평균 점수 변화(4개년 − 10개년, +이면 4개년이 후함):')
    for k_ in sorted(mdiff):
        lines.append(f'  {k_}: {sum(mdiff[k_]) / len(mdiff[k_]):+.2f}  (표본 {len(mdiff[k_])})')
    lines.append('해석: 평균이 +이고 상위 등급으로의 이동이 많으면 Yahoo(4개년) 점수가 후하게 나온다는 뜻입니다.')
    out = os.path.join(here, f'4개년편향점검_{date.today():%Y%m%d}.txt')
    open(out, 'w', encoding='utf-8-sig').write('\n'.join(lines))
    print('\n'.join(lines)); print(f'\n결과 파일: {out}')


# ===== 후보 사업보고서 점검 (미국 10-K, 메뉴 10) =====
FLAGS = [
    ('계속기업 의문', r'substantial doubt (about|regarding|as to)[^.]{0,60}(ability|going concern)|raise substantial doubt'),
    ('내부통제 중대한 미비', r'(identified|disclosed|reported|remediat\w+)[^.]{0,60}material weakness|material weakness[^.]{0,60}(was|were|has been|had been) (identified|remediated|reported)'),
    ('영업권·자산 손상(금액 기재)', r'(recorded|recognized|incurred|resulted in|included)[^.]{0,80}impairment[^.]{0,80}\$\s?\d|impairment (charges?|losses?)[^.]{0,60}(totaled|of|were)[^.]{0,20}\$\s?\d'),
    ('재무제표 재작성', r'restatement of (our|the|previously)|restated (our|the) (previously issued|consolidated|financial)|previously issued financial statements[^.]{0,60}(should no longer|restate)'),
    ('차입 약정(코버넌트) 문제', r'(credit|notes?|loan|facility|indenture|debt)[^.]{0,120}(not in compliance|waiver|violat\w+|breach\w*)[^.]{0,60}covenant|covenant[^.]{0,80}(waiver was|were waived|not in compliance|we violated|we breached)'),
    ('정부·규제 조사', r'(received|issued|served)[^.]{0,40}(subpoena|civil investigative demand|\bCID\b|wells notice)|cooperating with[^.]{0,80}(SEC|Securities and Exchange|Department of Justice|DOJ|Federal Trade Commission|FTC|attorney general)'),
    ('집단소송', r'(putative|purported|filed|captioned)[^.]{0,80}class action|class action[^.]{0,60}(was|were) (filed|brought|commenced)'),
    ('특정 고객 의존', r'(customer|client|distributor|wholesaler|retailer|payor)s?[^.]{0,60}accounted for (approximately )?\d+%|accounted for (approximately )?\d+%[^.]{0,60}(customer|client|distributor|wholesaler)'),
    ('구조조정(금액 기재)', r'(announced|approved|initiated|adopted|committed to)[^.]{0,60}restructuring|restructuring (charges?|costs?|expenses?)[^.]{0,60}(totaled|of|were|was)[^.]{0,20}\$\s?\d'),
    ('사이버 사고(실제 발생)', r'\b(we|the company)\b[^.]{0,40}(experienced|detected|identified|suffered|were subject to|was subject to)[^.]{0,60}(cybersecurity incident|data breach|ransomware|unauthorized access)'),
    ('감사인 변경', r'(dismiss|replac)\w* .{0,60}(auditor|accounting firm)'),
]
EXCL_RE = re.compile(r'accumulated (goodwill )?impairment|customary events of default|events of default|incorporated by reference|\bour audit|assessing the risk|amended and restated|Table of Contents.{0,40}Exhibit|have not materially|has not materially|to date,? .{0,60}not|have not experienced', re.I)
HYPO_RE = re.compile(r"\b(could|may|might|would|if|whether)\b|no substantial|did not identify|not (have|identify)", re.I)
MOAT_CATS = [
    ('브랜드·평판', r'brand (recognition|loyalty|awareness|equity|strength)|strong(est)? brands?|iconic|most trusted|trusted brand|well[- ]recognized'),
    ('전환비용·고객 고착', r'switching costs?|renewal rate|retention rate|recurring revenue|multi-year (contract|agreement)|long-term (contract|relationship)|mission[- ]critical|deeply embedded|sole[- ]source|repeat (and referral )?(business|customers)'),
    ('규모·원가 우위', r'economies of scale|scale advantage|cost advantage|low[- ]cost (producer|provider|manufactur|structure|leader)|purchasing power|vertical(ly)? integrat|(largest|leading) (\w+ ){0,3}(provider|retailer|company|manufacturer|producer|supplier|builder|operator|distributor|franchisor|brand|business)|market leader|leading market shares?'),
    ('네트워크·플랫폼', r'network effects?|marketplace|ecosystem|two-sided|installed base'),
    ('기술·지식재산', r'patented|proprietary (technology|platform|system|methodolog|software|data)|trade secrets?|know-how|differentiat|unique'),
    ('진입장벽·규제', r'barriers? to entry|licens(e|ing) requirements?|regulatory approval|(?<!gateway-)exclusiv(e|ity)|accredit\w+ (by|from)|sole[- ]source'),
    ('경쟁 환경', r'\bwe compete\b|our competitors|competitive (landscape|advantage|strength|position)'),
    ('성장 전략', r'(our|growth) strateg(y|ies)|we intend to|we plan to|we expect to (grow|expand)|expand(ing)? (our|into)'),
]
MONEY_RE = re.compile(r'\$\s?\d')


_TR = {'fail': 0, 'off': False, 'cache': None, 'path': None}


def ko(text):
    """영어 문장 -> 한국어(무료 번역 웹 엔드포인트, 키 불필요). 실패하면 '(번역 실패)'. 결과는 번역캐시.json에 저장."""
    if not text or text == '-':
        return text
    c = _TR['cache']
    if c is not None and text in c:
        return c[text]
    if _TR['off']:
        return '(번역 실패)'
    import urllib.parse
    out = None
    for attempt in range(2):
        try:
            url = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ko&dt=t&q=' + urllib.parse.quote(text[:1500])
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as r_:
                data = json.loads(r_.read().decode('utf-8'))
            out = ''.join(seg[0] for seg in data[0] if seg and seg[0])
            break
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
    if not out:
        _TR['fail'] += 1
        if _TR['fail'] >= 3:
            _TR['off'] = True
            print('   번역 서버에 연결되지 않아 이후 번역은 건너뜁니다(원문은 그대로 저장). 나중에 다시 실행하면 번역만 이어서 채웁니다.')
        return '(번역 실패)'
    _TR['fail'] = 0
    if c is not None:
        c[text] = out
    time.sleep(0.25)
    return out


def _tr_open(here):
    p = os.path.join(here, '번역캐시.json')
    try:
        _TR['cache'] = json.load(open(p, encoding='utf-8'))
    except (OSError, ValueError):
        _TR['cache'] = {}
    _TR['path'] = p; _TR['fail'] = 0; _TR['off'] = False


def _tr_save():
    if _TR['path'] and _TR['cache'] is not None:
        try:
            json.dump(_TR['cache'], open(_TR['path'], 'w', encoding='utf-8'), ensure_ascii=False)
        except OSError:
            pass


def get_10k_text(cik, acc, doc, ua, cache):
    import gzip
    p = os.path.join(cache, f'TEXT{cik}_{acc}.txt.gz')
    if os.path.exists(p):
        return gzip.open(p, 'rt', encoding='utf-8').read()
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
    t = html_to_text(http_json(url, ua, raw=True))
    gzip.open(p, 'wt', encoding='utf-8').write(t)
    time.sleep(0.35)
    return t


ABBR_END = re.compile(r'(\sv|\sInc|\sCorp|\sCo|\sLtd|\sNo|\sU\.S|\sL\.P|\sLLC|\sSt|\sVs)\.$')


def _sents(txt):
    raw = [x.strip() for x in SENT_SPLIT.split(re.sub(r'\s+', ' ', txt))]
    merged = []
    for x in raw:       # "... Fund v." 처럼 약어 뒤에서 잘린 문장을 다음 조각과 합친다
        if merged and ABBR_END.search(merged[-1]) and len(merged[-1]) < 600:
            merged[-1] = merged[-1] + ' ' + x
        else:
            merged.append(x)
    return [x for x in merged if 40 < len(x) < 900]


def _key(sent):
    return ' '.join(re.sub(r'[^a-z ]', ' ', sent.lower()).split()[:15])


def review_10k(cur, prev, filed_year=None):
    """현재·전년 10-K 본문 -> 점검 결과(dict)."""
    out = {}
    s1a = _section(cur, r'item\s*1a\.?\s*[\-–—:]?\s*risk\s*factors', r'item\s*1b')
    s1a_p = _section(prev, r'item\s*1a\.?\s*[\-–—:]?\s*risk\s*factors', r'item\s*1b') if prev else ''
    cs, ps = _sents(s1a), _sents(s1a_p)
    pk = {_key(x) for x in ps}
    new = [x for x in cs if _key(x) not in pk] if ps and cs else []
    out['risk_n'], out['risk_new_n'] = len(cs), len(new)
    neg = lambda x: sum(1 for w in WORD_RE.findall(x.lower()) if w in NEG_WORDS)
    out['risk_new'] = [x[:400] for x in sorted(new, key=lambda x: -neg(x))[:6]]
    allp = _sents(cur)
    fl = {}
    for nm, pat in FLAGS:
        hit = [x for x in allp if re.search(pat, x, re.I) and not EXCL_RE.search(x) and len(re.findall(r'\d[\d,\.]*', x)) <= 10]
        if not hit:
            continue
        real = [x for x in hit if not HYPO_RE.search(x)]
        if real:
            yrs = [int(y) for y in re.findall(r'\b(20\d\d)\b', real[0])]
            old_ = bool(yrs) and filed_year and max(yrs) < filed_year - 1
            fl[nm] = ('사실형(과거 사건: %d년)' % max(yrs) if old_ else '사실형 문장 있음', real[0][:400])
        else:
            fl[nm] = ('가정형 언급만', hit[0][:400])
    out['flags'] = fl
    s1 = _section(cur, r'item\s*1\.?\s*[\-–—:]?\s*business', r'item\s*1a', need_end=True, max_len=300000)
    biz = [x for x in _sents(s1 if len(s1) > 3000 else cur) if 80 < len(x) < 400 and not re.search(r'forward-looking|table of contents|item\s*\d', x, re.I)]
    out['biz'] = biz[:2]
    base_s = _sents(s1) if len(s1) > 3000 else []
    base_s = [x for x in base_s if 60 < len(x) < 520 and not re.search(r'forward-looking|table of contents|incorporated by reference|tax position|amortization|useful li|fair value|goodwill|impairment|we may|\bmay\b|\bcould\b|would|might|\bcan (result|be|lead|cause|have)|adverse|impair|risks?\b|unable|failure|loss of|substantial doubt|\bexpenses?\b|incremental costs|excluded as|forum|certificate of incorporation|\bface competition|\bpossible\b', x, re.I)]
    moat = {}
    for cat, pat in MOAT_CATS:
        seen_, hits = set(), []
        for x in base_s:
            if re.search(pat, x, re.I) and x not in seen_:
                seen_.add(x); hits.append(x)
        hits.sort(key=lambda x: -len(re.findall(pat, x, re.I)))
        moat[cat] = (len(hits), hits[:2])
    out['moat'] = moat
    out['moat_scope'] = f'Item 1 사업 설명 {len(base_s)}문장 중' if base_s else 'Item 1을 찾지 못해 분석 못함'
    out['bb'] = [x[:400] for x in allp if re.search(r'repurchase|buyback', x, re.I) and MONEY_RE.search(x) and not HYPO_RE.search(x)][:2]
    out['div'] = [x[:300] for x in allp if re.search(r'dividend', x, re.I) and MONEY_RE.search(x) and not HYPO_RE.search(x)][:1]
    s7 = _section(cur, r'item\s*7\.?\s*[\-–—:]?\s*management.s\s*discussion', r'item\s*7a') or cur
    out['liq'] = [x[:400] for x in _sents(s7) if re.search(r'senior notes|term loan|credit facility|revolving|total debt|outstanding borrowings', x, re.I) and MONEY_RE.search(x)][:3]
    return out


def make_questions(c, rv):
    q = ['이 회사의 이익이 10년 뒤에도 같은 방식으로 나올 근거(브랜드·전환비용·규모 등)가 무엇인지, 경쟁사가 따라 하기 어려운 이유는 무엇인가?']
    if c.get('gap_avg') is not None and c['gap'] - c['gap_avg'] > 0.3:
        q.append(f"최근 3년 이익 기준 괴리율({c['gap']:.0%})이 전 기간 평균 기준({c['gap_avg']:.0%})보다 크게 높다. 최근 이익이 일시적으로 좋았던 것인지, 사업이 한 단계 올라선 것인지?")
    else:
        q.append('최근 이익이 과거 평균과 비슷한 수준이다. 앞으로 5년 이익이 늘 이유(가격 인상·점유율·신사업)가 사업보고서에 구체적으로 적혀 있는가?')
    if c.get('mult') is not None and c['mult'] < 10:
        q.append(f"시총이 오너이익의 {c['mult']:.1f}배에 불과하다. 시장이 반영 중인 악재(소송·고객 이탈·규제·기술 변화)가 무엇이며, 사업보고서의 신규 위험문장에 그것이 있는가?")
    if rv['risk_new_n'] and rv['risk_n'] and rv['risk_new_n'] / rv['risk_n'] > 0.6:
        q.append(f"위험요인 문장의 {rv['risk_new_n'] / rv['risk_n']:.0%}가 '새 문장'으로 잡혔다. 문서 구조·문구가 크게 바뀌어 비교가 부정확할 수 있으니 신규위험문장 시트를 참고용으로만 보고 위험요인 항목 제목을 직접 비교할 것.")
    elif rv['risk_new_n'] and rv['risk_n'] and rv['risk_new_n'] / rv['risk_n'] > 0.3:
        q.append(f"위험요인 문장의 {rv['risk_new_n'] / rv['risk_n']:.0%}가 전년에 없던 문장이다. [신규위험문장] 시트의 내용 중 사업 모델을 직접 흔드는 것이 있는가?")
    else:
        q.append('[신규위험문장] 시트의 새 문장 중 사업 모델을 직접 흔드는 것이 있는가?')
    q.append('부채 만기와 이자 부담을 이익으로 감당할 수 있는가? (유동성 문장·차입 규모 확인)')
    q.append('경영진은 자사주·배당·인수를 어떤 원칙으로 쓰는가? 최근 5년 인수가 이익에 기여했는가, 아니면 손상(영업권)으로 이어졌는가?')
    return q[:6]


def _pick_list(here, groups):
    import glob
    files = sorted(glob.glob(os.path.join(here, '*_핵심후보.json')), key=os.path.getmtime, reverse=True)
    if not files:
        sys.exit('\n[점검] 핵심후보 목록이 없습니다. 먼저 메뉴 1·3·5 중 하나를 실행해 결과 엑셀을 만든 뒤 다시 실행하세요.')
    infos = []
    for f_ in files:
        try:
            lst_ = json.load(open(f_, encoding='utf-8'))
        except (OSError, ValueError):
            continue
        infos.append((f_, lst_, datetime.fromtimestamp(os.path.getmtime(f_))))
    if not infos:
        sys.exit('\n[점검] 핵심후보 목록 파일을 읽지 못했습니다. 메뉴 1·3·5를 다시 실행하세요.')
    best = max(range(len(infos)), key=lambda i: (sum(1 for c in infos[i][1] if c['grp'][0] in groups), infos[i][2]))
    if len(infos) > 1:
        print('\n점검에 쓸 결과 목록을 고르세요(구분 %s 종목 수가 많은 것이 기본):' % '·'.join(sorted(groups)))
        for i, (f_, lst_, t_) in enumerate(infos, 1):
            print(f"  {i}) {os.path.basename(f_).replace('_핵심후보.json', '')}  ({t_:%m-%d %H:%M})  해당 {sum(1 for c in lst_ if c['grp'][0] in groups)}종목 / 전체 {len(lst_)}")
        pick = ask(f'번호 [{best + 1}]: ', str(best + 1))
        best = int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(infos) else best
    src = infos[best][0]
    cands = [c for c in infos[best][1] if c['grp'][0] in groups]
    return src, cands



def run_review(here, email, ua, cache, groups):
    src, cands = _pick_list(here, groups)
    print(f'\n[점검] 목록 파일: {os.path.basename(src)}  (구분 {"·".join(sorted(groups))}: {len(cands)}종목)')
    try:
        tick = cached(os.path.join(cache, 'company_tickers.json'), 'https://www.sec.gov/files/company_tickers.json', ua)
    except (SecError, urllib.error.URLError, OSError) as e:
        sys.exit(f'\nSEC 접속 실패: {str(e)[:200]}\n10분~1시간 뒤 다시 시도하거나 다른 인터넷으로 바꿔 보세요.')
    t2cik = {v['ticker'].upper(): str(v['cik_str']).zfill(10) for v in tick.values()}
    res, skipped = [], []
    for k, c in enumerate(cands, 1):
        cik = t2cik.get(c['ticker'].upper()) or t2cik.get(c['ticker'].upper().replace('-', '.'))
        if not cik:
            skipped.append((c['ticker'], c['name'], '미국 10-K 제출 기업이 아님(한국은 DART 사업보고서, 그 밖은 회사 공시를 직접 열어 보세요)')); continue
        try:
            fl = list_10k(cik, ua, cache)
            if not fl:
                skipped.append((c['ticker'], c['name'], '10-K 없음')); continue
            cur = get_10k_text(cik, fl[0][1], fl[0][2], ua, cache)
            prev = get_10k_text(cik, fl[1][1], fl[1][2], ua, cache) if len(fl) > 1 else ''
        except SecError as e:
            if str(e) == 'RATE':
                print('   SEC가 요청 과다로 잠시 차단했습니다. 지금까지 분석한 종목까지만 저장합니다. (30분~1시간 뒤 재실행하면 이어집니다)'); break
            skipped.append((c['ticker'], c['name'], f'조회 실패: {str(e)[:100]}')); continue
        except (urllib.error.URLError, OSError, ValueError) as e:
            skipped.append((c['ticker'], c['name'], f'조회 실패: {str(e)[:100]}')); continue
        rv = review_10k(cur, prev, int(fl[0][3][:4]))
        rv['url'] = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{fl[0][1].replace('-', '')}/{fl[0][2]}"
        rv['filed'] = fl[0][3]
        res.append((c, rv))
        print(f"   [{k}/{len(cands)}] {c['ticker']:6s} 신규 위험문장 {rv['risk_new_n']}/{rv['risk_n']}, 주의신호 {len(rv['flags'])}개")
    if not res:
        print('\n분석된 종목이 없습니다.');
        for t in skipped:
            print('  ', t)
        return
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    FN = '맑은 고딕'
    hdr = PatternFill('solid', fgColor='1F3864')

    def head(ws, labels):
        for i, t in enumerate(labels):
            c = ws.cell(row=1, column=1 + i, value=t); c.font = Font(name=FN, size=10, bold=True, color='FFFFFF'); c.fill = hdr
            c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    def put(ws, row, vals, fmts=None):
        for j, v in enumerate(vals):
            c = ws.cell(row=row, column=1 + j, value=v); c.font = Font(name=FN, size=10)
            c.alignment = Alignment(wrap_text=True, vertical='top')
            if fmts and fmts[j]:
                c.number_format = fmts[j]
    wb = Workbook(); w1 = wb.active; w1.title = '점검요약'
    head(w1, ['구분', '티커', '종목', '종합점수', '괴리율', '전체평균 괴리율', '시총÷오너이익(배)', '신규 위험문장 비율', '주의신호', '사실형 주의신호', '10-K 제출일', '10-K 링크', '직접 확인할 질문', '사업 설명 속 경쟁우위 근거 (유형:문장 수)'])
    for r_, (c, rv) in enumerate(res, 2):
        real = [k_ + ('(시점확인)' if '시점 불명' in v[0] else '') for k_, v in rv['flags'].items() if v[0].startswith('사실형')]
        ratio = rv['risk_new_n'] / rv['risk_n'] if rv['risk_n'] else None
        put(w1, r_, [c['grp'], c['ticker'], c['name'], c['score'], c['gap'], c['gap_avg'], c['mult'], ratio, ', '.join(real) or '-', ', '.join(k_ for k_, v in rv['flags'].items() if v[0].startswith('가정')) or '-',
                     rv['filed'], rv['url'], '\n'.join(f'{i + 1}. {x}' for i, x in enumerate(make_questions(c, rv))),
                     ', '.join(f'{k_}:{v_[0]}' for k_, v_ in rv['moat'].items() if v_[0] and k_ not in ('경쟁 환경', '성장 전략')) or '근거 문장 거의 없음'],
            [None, None, None, '0.00', '0%', '0%', '0.0', '0%', None, None, None, None, None, None])
    for kk, w in zip('ABCDEFGHIJKLMN', [20, 9, 28, 9, 9, 11, 11, 11, 34, 34, 12, 40, 110, 50]):
        w1.column_dimensions[kk].width = w
    w1.freeze_panes = 'D2'
    _tr_open(here)
    print('\n[번역] 근거 문장을 한국어로 옮기는 중입니다(무료 번역 서버, 문장당 약 0.3초; 한 번 번역한 문장은 저장돼 다음부터 빠릅니다)...')
    w2 = wb.create_sheet('신규위험문장'); head(w2, ['티커', '종목', '전년 10-K에 없던 위험요인 문장 (부정 단어가 많은 순, 원문 발췌)', '한글 번역'])
    w3 = wb.create_sheet('주의신호근거'); head(w3, ['티커', '종목', '신호', '성격', '원문 발췌', '한글 번역'])
    w4 = wb.create_sheet('사업·자본배분'); head(w4, ['티커', '종목', '사업 설명(원문)', '사업 설명(한글)', '자사주 매입(원문)', '자사주 매입(한글)', '배당(원문)', '배당(한글)', '차입·유동성(원문)', '차입·유동성(한글)'])
    a2 = a3 = 2
    for r_, (c, rv) in enumerate(res, 2):
        for x in rv['risk_new']:
            put(w2, a2, [c['ticker'], c['name'], x, ko(x)]); a2 += 1
        for nm, (kind, ev) in rv['flags'].items():
            put(w3, a3, [c['ticker'], c['name'], nm, kind, ev, ko(ev)]); a3 += 1
        cols_ = [('\n'.join(rv['biz']), rv['biz']), ('\n'.join(rv['bb']), rv['bb']), ('\n'.join(rv['div']), rv['div']), ('\n'.join(rv['liq']), rv['liq'])]
        vals_ = [c['ticker'], c['name']]
        for txt_, lst_ in cols_:
            vals_ += [txt_ or '-', '\n'.join(ko(x) for x in lst_) or '-']
        put(w4, r_, vals_)
        _tr_save()
    for ws_, ws_w in ((w2, [9, 28, 90, 90]), (w3, [9, 28, 24, 16, 80, 80]), (w4, [9, 28, 50, 50, 50, 50, 35, 35, 55, 55])):
        for kk, w in zip('ABCDEFGHIJ', ws_w):
            ws_.column_dimensions[kk].width = w
        ws_.freeze_panes = 'A2'
    w6 = wb.create_sheet('사업질요약', 1); head(w6, ['티커', '종목', '구분', '문장 수', '대표 문장 (사업 설명 원문)', '한글 번역'])
    a6 = 2
    for c, rv in res:
        for cat, (n_, sents_) in rv['moat'].items():
            if not n_:
                continue
            for x in sents_:
                put(w6, a6, [c['ticker'], c['name'], cat, n_, x, ko(x)]); a6 += 1
        _tr_save()
    for kk, w in zip('ABCDEF', [9, 26, 18, 8, 90, 90]):
        w6.column_dimensions[kk].width = w
    w6.freeze_panes = 'A2'
    w5 = wb.create_sheet('제외·안내'); head(w5, ['티커', '종목', '사유'])
    for i, t in enumerate(skipped, 2):
        put(w5, i, list(t))
    put(w5, len(skipped) + 5, ['※', '', '[사업질요약]은 사업 설명(Item 1)에서 경쟁우위를 서술한 문장을 키워드로 모은 것입니다. 회사가 스스로 쓴 주장이므로 사실 여부는 판단하지 않으며, 문장이 많다고 해자가 강한 것은 아닙니다. 근거가 구체적(점유율 수치·계약 기간·재계약률)인지 직접 읽어 보세요.'])
    put(w5, len(skipped) + 4, ['※', '', '이 점검은 문장 비교·키워드 검색으로 "어디를 읽어야 하는지"를 찍어 주는 보조 도구입니다. 사업의 좋고 나쁨을 판정하지 않습니다. '
                              '"가정형 언급만"은 위험요인 상투 문구일 가능성이 높고, "사실형 문장 있음"은 실제 발생을 서술했을 가능성이 있어 원문 확인이 필요합니다.'])
    w5.column_dimensions['A'].width = 9; w5.column_dimensions['B'].width = 28; w5.column_dimensions['C'].width = 120
    for s_ in wb.worksheets:
        s_.sheet_view.showGridLines = False
    out = os.path.join(here, f'사업보고서점검_{date.today():%Y%m%d}.xlsx')
    wb.save(out)
    print(f'\n점검 결과: {out}  ({len(res)}종목, 제외 {len(skipped)}종목)')


# ===== 한국 종목 사업보고서 점검 (DART, 메뉴 11) =====
KR_FLAGS = [
    ('감사의견 비적정', r'(한정|부적정)\s*의견|의견\s*거절'),
    ('계속기업 불확실성', r'계속기업[^.]{0,40}(불확실성이 존재|의문이 (제기|존재)|중요한 불확실성이 있)'),
    ('내부회계관리제도 비적정', r'내부회계관리제도[^.]{0,60}(비적정|중요한 취약점이 (발견|존재합니다))|중요한 취약점[^.]{0,20}발견'),
    ('소송 계류', r'(소송|피소|제소)[^.]{0,80}(원고|피고|청구금액|소송가액|계류)'),
    ('공정위·수사·제재', r'과징금|시정명령|압수수색|기소|공정거래위원회[^.]{0,50}(제재|조치|처분|부과|시정|조사)|(검찰|경찰)[^.]{0,20}(수사|조사|고발)'),
    ('횡령·배임', r'(횡령|배임)[^.]{0,30}(발생|혐의|고소|고발|기소|판결|사건|사고)'),
    ('손상차손 인식(금액)', r'손상차손[^.]{0,50}(인식|계상)[^.]{0,40}\d{2,}|\d{2,}[^.]{0,20}손상차손'),
    ('유상증자·전환사채·BW 발행', r'(유상증자|전환사채|신주인수권부사채|교환사채)[^.]{0,40}(결정|발행하|발행 |납입|청약)'),
    ('최대주주 변경', r'최대주주[^.]{0,30}변경'),
    ('지급보증', r'(지급보증|채무보증)[^.]{0,50}(제공하|제공한|제공 내역|\d)'),
]
# 감사보고서 상투문구·이사 약력·첨부파일·회계정책 설명 등은 신호가 아님
KR_EXCL = re.compile(r'감사증거|합리적 확신|결론을 내립|의견을 표명|감사를 계획|감사인의 책임|경영진의 책임|감사의견을|역임|경력|교수|변호사|위원회 위원|위원장|\.jpg|\.png|붙임|제도 도입|지주회사의 기준|규정에 따라 판단|검토하고 있|판단하고 있|결정됩니다|공정가치|신용도|제공받|담보로 제공된|투자|취득|참여|대여금|정정공시|철회|사외이사|후보|주식매수선택권|제\s?\d+\s?조|배당기산일|정관')
KR_HYPO = re.compile(r'수 있|우려|가능성|할 경우|하는 경우|없음|해당사항')
KR_MOAT = [
    ('브랜드·평판', r'브랜드[^.]{0,20}(인지도|충성|가치|파워)|대표 브랜드|인지도'),
    ('시장지위·점유율', r'시장점유율|점유율|1위|업계 (선두|최대)|국내 최대|독점적'),
    ('전환비용·장기관계', r'장기\s*(공급|계약)|재계약|독점\s*(공급|계약)|전속|납품 계약|거래 관계'),
    ('규모·원가 우위', r'규모의 경제|원가 (경쟁력|절감)|대량 (구매|생산)|수직계열|가격 경쟁력'),
    ('기술·지식재산', r'특허|지식재산|독자적?\s*기술|연구개발'),
    ('진입장벽·규제', r'진입장벽|인허가|면허|라이선스|규제 산업'),
    ('경쟁 환경', r'경쟁(사|업체)|경쟁 (구도|환경)'),
    ('성장 전략', r'성장 전략|신규 (사업|시장)|해외 (진출|확대)|사업 확장'),
]


def _kr_text(raw_zip):
    z = zipfile.ZipFile(io.BytesIO(raw_zip))
    parts = []
    for nm in z.namelist():
        b = z.read(nm)
        try:
            t = b.decode('utf-8')
        except UnicodeDecodeError:
            t = b.decode('cp949', 'ignore')
        t = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', t)
        t = re.sub(r'(?i)</(p|tr|title|li|div|section-\d|table)>|<br\s*/?>', '\n', t)
        t = re.sub(r'<[^>]+>', ' ', t)
        import html as _h
        parts.append(_h.unescape(t))
    return re.sub(r'[ \t\r\f\v\xa0]+', ' ', '\n'.join(parts))


def kr_report_text(key, corp, rcept, cache):
    import gzip
    p = os.path.join(cache, f'KRTXT_{rcept}.txt.gz')
    if os.path.exists(p):
        return gzip.open(p, 'rt', encoding='utf-8').read()
    raw = dart_get('document.xml', {'crtfc_key': key, 'rcept_no': rcept}, binary=True)
    if raw[:2] != b'PK':
        raise DartError('문서 조회 실패: ' + re.sub(r'<[^>]+>', ' ', raw.decode('utf-8', 'ignore')).strip()[:120])
    t = _kr_text(raw)
    gzip.open(p, 'wt', encoding='utf-8').write(t)
    time.sleep(0.2)
    return t


def kr_list_reports(key, corp, cache):
    y = date.today().year
    p = os.path.join(cache, f'KRLIST_{corp}.json')
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 7 * 86400:
        js = json.load(open(p, encoding='utf-8'))
    else:
        js = dart_get('list.json', {'crtfc_key': key, 'corp_code': corp, 'bgn_de': f'{y - 3}0101', 'end_de': f'{y}1231',
                                    'pblntf_ty': 'A', 'page_count': 50})
        if js.get('status') == '000':
            json.dump(js, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
        time.sleep(0.15)
    rows = [r for r in js.get('list', []) if str(r.get('report_nm', '')).startswith('사업보고서')]
    rows.sort(key=lambda r: (r.get('rcept_dt', ''), r.get('rcept_no', '')), reverse=True)
    out, seen = [], set()
    for r in rows:       # 같은 사업연도(보고서명 괄호)는 가장 최근 것(정정 포함)만
        k = r.get('report_nm', '')
        k = re.sub(r'\s', '', k)
        k = re.search(r'\((\d{4}\.\d{2})\)', k)
        k = k.group(1) if k else r.get('rcept_no')
        if k in seen:
            continue
        seen.add(k); out.append(r)
    return out[:2]


def _kr_sents(txt):
    out = []
    for ln in txt.split('\n'):
        ln = ln.strip()
        if len(ln) < 25:
            continue
        for x in re.split(r'(?<=[다요음임함됨])\.\s*', ln):
            x = x.strip()
            if 25 <= len(x) <= 600:
                out.append(x)
    return out


def review_kr(cur, prev, fy=None):
    out = {}
    sec = _section(cur, r'II\.\s*사업의\s*내용', r'III\.\s*재무에\s*관한\s*사항', need_end=True, max_len=600000)
    base = _kr_sents(sec) if len(sec) > 3000 else []
    out['scope'] = f'II. 사업의 내용 {len(base)}문장' if base else 'II. 사업의 내용을 찾지 못함(문서 전체에서 검색)'
    pool = base or _kr_sents(cur)
    allp = _kr_sents(cur)
    fl = {}
    for nm, pat in KR_FLAGS:
        hit = [x for x in allp if re.search(pat, x) and not KR_EXCL.search(x) and len(re.findall(r'\d[\d,\.]*', x)) <= 25]
        if fy:   # 3년 넘은 과거 사건만 적힌 문장 제외
            hit = [x for x in hit if not (re.findall(r'(20\d\d)년', x) and max(int(y) for y in re.findall(r'(20\d\d)년', x)) <= fy - 3)]
        if not hit:
            continue
        real = [x for x in hit if not KR_HYPO.search(x)]
        if real:
            kind = '사실형 문장 있음'
            if nm in ('최대주주 변경', '유상증자·전환사채·BW 발행') and not re.search(r'20\d\d', real[0]):
                kind = '사실형(시점 불명: 과거 사건일 수 있음)'
            fl[nm] = (kind, real[0][:400])
        else:
            fl[nm] = ('가정·일반 언급만', hit[0][:400])
    out['flags'] = fl
    moat = {}
    for cat, pat in KR_MOAT:
        seen_, hits = set(), []
        for x in pool:
            if re.search(pat, x) and x not in seen_ and not re.search(r'수 있|우려|가능성|해당사항', x):
                seen_.add(x); hits.append(x)
        hits.sort(key=lambda x: -len(re.findall(pat, x)))
        moat[cat] = (len(hits), [h[:400] for h in hits[:2]])
    out['moat'] = moat
    ps = {x[:30] for x in _kr_sents(_section(prev, r'II\.\s*사업의\s*내용', r'III\.\s*재무에\s*관한\s*사항', need_end=True, max_len=600000))} if prev else set()
    new = [x for x in base if x[:30] not in ps] if ps and base else []
    out['new_n'], out['base_n'] = len(new), len(base)
    out['new'] = [x[:400] for x in new if re.search(r'신규|진출|인수|중단|철수|감소|하락|증가|변경', x)][:5]
    out['biz'] = [x[:400] for x in base if 40 <= len(x) <= 400 and re.search(r'영위|주요 (제품|사업)|사업부문|매출', x)][:2]
    return out


def run_review_kr(here, key, groups):
    src, cands = _pick_list(here, groups)
    cands = [c for c in cands if re.fullmatch(r'\d{6}\.(KS|KQ)', c['ticker'])]
    print(f'\n[한국 점검] 목록 파일: {os.path.basename(src)}  (구분 {"·".join(sorted(groups))} 중 한국 종목 {len(cands)}개)')
    if not cands:
        print('한국 종목이 없습니다.'); return
    cache = os.path.join(here, 'dart_cache'); os.makedirs(cache, exist_ok=True)
    try:
        corp = load_corp_codes(key, cache)
    except DartError as e:
        sys.exit(f'\n{e}')
    res, skipped = [], []
    for k, c in enumerate(cands, 1):
        code = c['ticker'][:6]
        if code not in corp:
            skipped.append((c['ticker'], c['name'], 'DART 기업 목록에 없음')); continue
        cc = corp[code][0]
        try:
            rl = kr_list_reports(key, cc, cache)
            if not rl:
                skipped.append((c['ticker'], c['name'], '사업보고서를 찾지 못함')); continue
            cur = kr_report_text(key, cc, rl[0]['rcept_no'], cache)
            prev = kr_report_text(key, cc, rl[1]['rcept_no'], cache) if len(rl) > 1 else ''
        except DartError as e:
            skipped.append((c['ticker'], c['name'], str(e)[:120])); continue
        rv = review_kr(cur, prev, int(rl[0].get('rcept_dt', '0')[:4] or 0) or None)
        rv['filed'] = rl[0].get('rcept_dt', ''); rv['name_r'] = rl[0].get('report_nm', '')
        rv['url'] = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rl[0]['rcept_no']}"
        res.append((c, rv))
        print(f"   [{k}/{len(cands)}] {c['ticker']:10s} {c['name'][:12]:12s} 주의신호 {len(rv['flags'])}개, 사업 설명 {rv['base_n']}문장")
    if not res:
        print('\n분석된 종목이 없습니다.')
        for t in skipped:
            print('  ', t)
        return
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    FN = '맑은 고딕'; hdr = PatternFill('solid', fgColor='1F3864')

    def head(ws, labels):
        for i, t in enumerate(labels):
            c_ = ws.cell(row=1, column=1 + i, value=t); c_.font = Font(name=FN, size=10, bold=True, color='FFFFFF'); c_.fill = hdr
            c_.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    def put(ws, row, vals, fmts=None):
        for j, v in enumerate(vals):
            c_ = ws.cell(row=row, column=1 + j, value=v); c_.font = Font(name=FN, size=10); c_.alignment = Alignment(wrap_text=True, vertical='top')
            if fmts and fmts[j]:
                c_.number_format = fmts[j]
    wb = Workbook(); w1 = wb.active; w1.title = '점검요약'
    head(w1, ['구분', '티커', '종목', '종합점수', '괴리율', '전체평균 괴리율', '시총÷오너이익(배)', '사업보고서', '제출일', '사실형 주의신호', '가정·일반 언급만 걸린 항목', '사업 설명 속 경쟁우위 근거 (유형:문장 수)', 'DART 링크', '직접 확인할 질문'])
    for r_, (c, rv) in enumerate(res, 2):
        real = [k_ + ('(시점확인)' if '시점 불명' in v[0] else '') for k_, v in rv['flags'].items() if v[0].startswith('사실형')]
        qs = ['이 회사의 이익이 10년 뒤에도 같은 방식으로 나올 근거는 무엇인가? (시장점유율·브랜드·장기계약 문장의 수치를 확인)']
        if c.get('gap_avg') is not None and c['gap'] - c['gap_avg'] > 0.3:
            qs.append(f"최근 3년 이익 기준 괴리율({c['gap']:.0%})이 전체평균 기준({c['gap_avg']:.0%})보다 크게 높다. 최근 이익이 정점인지 확인하세요.")
        if c.get('mult') is not None and c['mult'] < 10:
            qs.append(f"시총이 오너이익의 {c['mult']:.1f}배에 불과하다. 시장이 반영 중인 악재(소송·규제·지배구조)가 [주의신호근거]에 있는가?")
        qs.append('지배구조: 최대주주·특수관계인 지분율, 배당 성향, 자사주 정책, 계열사 거래 비중을 사업보고서 [VII. 주주에 관한 사항], [X. 대주주 등과의 거래내용]에서 확인하세요.')
        qs.append('차입금 만기와 이자 부담을 이익으로 감당할 수 있는가?')
        put(w1, r_, [c['grp'], c['ticker'], c['name'], c['score'], c['gap'], c['gap_avg'], c['mult'], rv['name_r'], rv['filed'], ', '.join(real) or '-', ', '.join(k_ for k_, v in rv['flags'].items() if v[0].startswith('가정')) or '-',
                     ', '.join(f'{k_}:{v_[0]}' for k_, v_ in rv['moat'].items() if v_[0] and k_ not in ('경쟁 환경', '성장 전략')) or '근거 문장 거의 없음', rv['url'],
                     '\n'.join(f'{i + 1}. {x}' for i, x in enumerate(qs))],
            [None, None, None, '0.00', '0%', '0%', '0.0', None, None, None, None, None, None, None])
    for kk, w in zip('ABCDEFGHIJKLMN', [20, 11, 20, 9, 9, 11, 11, 22, 11, 34, 30, 50, 44, 100]):
        w1.column_dimensions[kk].width = w
    w1.freeze_panes = 'D2'
    w2 = wb.create_sheet('주의신호근거'); head(w2, ['티커', '종목', '신호', '성격', '원문 발췌'])
    w3 = wb.create_sheet('사업질요약'); head(w3, ['티커', '종목', '구분', '문장 수', '대표 문장 (사업의 내용 원문)'])
    w4 = wb.create_sheet('사업·변화'); head(w4, ['티커', '종목', '사업 설명', '전년 보고서에 없던 문장(신규·진출·변경 관련)', '신규 문장 비율'])
    a2 = a3 = 2
    for r_, (c, rv) in enumerate(res, 2):
        for nm, (kind, ev) in rv['flags'].items():
            put(w2, a2, [c['ticker'], c['name'], nm, kind, ev]); a2 += 1
        for cat, (n_, ss) in rv['moat'].items():
            for x in ss:
                put(w3, a3, [c['ticker'], c['name'], cat, n_, x]); a3 += 1
        put(w4, r_, [c['ticker'], c['name'], '\n'.join(rv['biz']) or '-', '\n'.join(rv['new']) or '-', (rv['new_n'] / rv['base_n']) if rv['base_n'] else None], [None, None, None, None, '0%'])
    for ws_, ws_w in ((w2, [11, 20, 24, 16, 150]), (w3, [11, 20, 18, 8, 150]), (w4, [11, 20, 70, 90, 12])):
        for kk, w in zip('ABCDE', ws_w):
            ws_.column_dimensions[kk].width = w
        ws_.freeze_panes = 'A2'
    w5 = wb.create_sheet('제외·안내'); head(w5, ['티커', '종목', '사유'])
    for i, t in enumerate(skipped, 2):
        put(w5, i, list(t))
    put(w5, len(skipped) + 3, ['※', '', '문장 검색 방식의 보조 도구입니다. 한국 사업보고서에는 미국 10-K 같은 위험요인 항목이 없어 "위험"은 소송·제재·증자·감사의견 문구로만 찾습니다. '
                              '"가정·일반 언급만"은 일반 설명일 가능성이 높습니다. 지배구조(최대주주·특수관계인 거래)는 자동 판단하지 않으니 직접 확인하세요.'])
    w5.column_dimensions['A'].width = 11; w5.column_dimensions['B'].width = 20; w5.column_dimensions['C'].width = 130
    for s_ in wb.worksheets:
        s_.sheet_view.showGridLines = False
    out = os.path.join(here, f'한국사업보고서점검_{date.today():%Y%m%d}.xlsx')
    wb.save(out)
    print(f'\n점검 결과: {out}  ({len(res)}종목, 제외 {len(skipped)}종목)')


# ===== 5년 실적·경쟁사 비교 (메뉴 12, 미국 SEC) =====
def _last_n(d, k, n=5):
    return [x for x in d[k]][-n:]


def peer_metrics(d, n=5):
    yrs = d['years'][-n:]
    rev, oi, ni = _last_n(d, 'revenue', n), _last_n(d, 'operating_income', n), _last_n(d, 'net_income', n)
    cfo, cap = _last_n(d, 'cfo', n), _last_n(d, 'capex', n)
    sh, dv, bb, debt = _last_n(d, 'shares_out', n), _last_n(d, 'dividends_paid', n), _last_n(d, 'buybacks', n), _last_n(d, 'total_debt', n)
    fcf = [None if (a is None) else a - (b or 0.0) for a, b in zip(cfo, cap)]
    om = [None if (a is None or not b) else a / b for a, b in zip(oi, rev)]

    def cagr(v):
        a = [x for x in v if x is not None]
        if len(a) < 2 or a[0] <= 0 or a[-1] <= 0:
            return None
        return (a[-1] / a[0]) ** (1 / (len(a) - 1)) - 1

    def ssum(v):
        a = [x for x in v if x is not None]
        return sum(a) if a else None
    f_s, n_s = ssum(fcf), ssum(ni)
    ret = ssum([(x or 0) + (y or 0) for x, y in zip(dv, bb)]) if any(x is not None for x in dv + bb) else None
    m = {'years': yrs, 'rev': rev, 'oi': oi, 'ni': ni, 'fcf': fcf, 'om': om, 'sh': sh, 'debt': debt,
         'rev_cagr': cagr(rev), 'ni_cagr': cagr(ni), 'fcf_cagr': cagr(fcf),
         'om_first': next((x for x in om if x is not None), None), 'om_last': next((x for x in reversed(om) if x is not None), None),
         'om_max': max((x for x in om if x is not None), default=None),
         'fcf_ni': (f_s / n_s) if (f_s is not None and n_s and n_s > 0) else None,
         'payout': (ret / f_s) if (ret is not None and f_s and f_s > 0) else None,
         'sh_chg': (sh[-1] / sh[0] - 1) if (sh[0] and sh[-1]) else None,
         'last_rev_g': (rev[-1] / rev[-2] - 1) if (rev[-1] and rev[-2]) else None,
         'last_oi_g': (oi[-1] / oi[-2] - 1) if (oi[-1] and oi[-2] and oi[-2] > 0) else None,
         'rev_last': rev[-1]}
    fl = []
    if m['om_last'] is not None and m['om_max'] is not None and m['om_max'] - m['om_last'] > 0.05:
        fl.append(f"영업이익률이 5년 최고({m['om_max']:.0%})보다 {m['om_max'] - m['om_last']:.0%}p 낮음")
    if m['last_oi_g'] is not None and m['last_oi_g'] < -0.05:
        fl.append(f"최근 연도 영업이익 {m['last_oi_g']:.0%}")
    if m['fcf_ni'] is not None and m['fcf_ni'] < 0.6:
        fl.append(f"FCF가 순이익의 {m['fcf_ni']:.0%}에 그침(이익의 현금 전환 약함)")
    if m['payout'] is not None and m['payout'] > 1.0:
        fl.append(f"배당·자사주가 FCF의 {m['payout']:.0%}(FCF 초과 환원)")
    if m['sh_chg'] is not None and m['sh_chg'] > 0.05:
        fl.append(f"주식수 {m['sh_chg']:.0%} 증가(희석)")
    m['flags'] = fl
    return m


def run_compare(here, email, ua, cache, groups):
    from statistics import median
    try:
        tick = cached(os.path.join(cache, 'company_tickers.json'), 'https://www.sec.gov/files/company_tickers.json', ua)
    except (SecError, urllib.error.URLError, OSError) as e:
        sys.exit(f'\nSEC 접속 실패: {str(e)[:200]}')
    t2 = {v['ticker'].upper(): (str(v['cik_str']).zfill(10), v['title']) for v in tick.values()}
    out_rows, skipped = [], []
    for grp in groups:
        for i, tk in enumerate(grp):
            cik_t = t2.get(tk.upper()) or t2.get(tk.upper().replace('-', '.'))
            if not cik_t:
                skipped.append((tk, '미국 SEC 등록 티커가 아님')); continue
            cik, nm = cik_t
            try:
                res, why = load_extracted(cik, cache, ua)
            except (SecError, urllib.error.URLError, OSError) as e:
                skipped.append((tk, '조회 실패: ' + str(e)[:80])); continue
            if not res:
                skipped.append((tk, why or '자료 없음')); continue
            d = res[0]
            if sum(1 for x in d['revenue'][-5:] if x is not None) < 3:
                skipped.append((tk, '최근 5년 중 매출 자료 3년 미만')); continue
            out_rows.append({'grp': grp[0], 'role': '기준' if i == 0 else '경쟁사', 'tk': tk.upper(), 'name': nm, 'm': peer_metrics(d)})
            print(f"   {tk.upper():8s} {nm[:28]:28s} 매출 {out_rows[-1]['m']['rev_last'] or 0:,.0f}백만$")
    if not out_rows:
        print('\n비교할 종목이 없습니다.'); return
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    FN = '맑은 고딕'; hdr = PatternFill('solid', fgColor='1F3864'); base = PatternFill('solid', fgColor='DDEBF7')

    def head(ws, labels):
        for i, t in enumerate(labels):
            c_ = ws.cell(row=1, column=1 + i, value=t); c_.font = Font(name=FN, size=10, bold=True, color='FFFFFF'); c_.fill = hdr
            c_.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    def put(ws, row, vals, fmts=None, fill=None):
        for j, v in enumerate(vals):
            c_ = ws.cell(row=row, column=1 + j, value=v); c_.font = Font(name=FN, size=10, bold=(fill is not None)); c_.alignment = Alignment(wrap_text=True, vertical='top')
            if fill:
                c_.fill = fill
            if fmts and fmts[j]:
                c_.number_format = fmts[j]
    wb = Workbook(); w1 = wb.active; w1.title = '비교요약'
    cols = ['묶음(기준)', '역할', '티커', '회사', '최근 매출(백만$)', '매출 연평균', '최근연도 매출 증가', '영업이익률 시작', '영업이익률 최근', '영업이익률 5년 최고', '순이익 연평균', 'FCF 연평균',
            'FCF÷순이익(5년 합)', '배당+자사주÷FCF', '주식수 변화', '주의 사항(자동)']
    head(w1, cols)
    fm = [None, None, None, None, '#,##0', '0.0%', '0.0%', '0.0%', '0.0%', '0.0%', '0.0%', '0.0%', '0%', '0%', '0.0%', None]
    r_ = 2
    for grp in groups:
        rows = [x for x in out_rows if x['grp'] == grp[0]]
        for x in rows:
            m = x['m']
            put(w1, r_, [x['grp'], x['role'], x['tk'], x['name'], m['rev_last'], m['rev_cagr'], m['last_rev_g'], m['om_first'], m['om_last'], m['om_max'], m['ni_cagr'], m['fcf_cagr'],
                         m['fcf_ni'], m['payout'], m['sh_chg'], '; '.join(m['flags']) or '-'], fm, base if x['role'] == '기준' else None)
            r_ += 1
        peers = [x['m'] for x in rows if x['role'] == '경쟁사']
        if peers:
            def med(k):
                v = [p[k] for p in peers if p[k] is not None]
                return median(v) if v else None
            put(w1, r_, [grp[0], '경쟁사 중앙값', '', f'{len(peers)}개사', None, med('rev_cagr'), med('last_rev_g'), med('om_first'), med('om_last'), med('om_max'), med('ni_cagr'), med('fcf_cagr'),
                         med('fcf_ni'), med('payout'), med('sh_chg'), ''], fm)
            for c_ in w1[r_]:
                c_.font = Font(name=FN, size=10, italic=True)
            r_ += 1
        r_ += 1
    for kk, w in zip('ABCDEFGHIJKLMNOP', [10, 12, 8, 26, 12, 9, 10, 10, 10, 10, 9, 9, 11, 11, 9, 70]):
        w1.column_dimensions[kk].width = w
    w1.freeze_panes = 'E2'
    w2 = wb.create_sheet('연도별'); head(w2, ['티커', '연도', '매출', '영업이익', '영업이익률', '순이익', 'FCF(영업CF-설비투자)', '주식수(백만)', '차입금'])
    r2 = 2
    for x in out_rows:
        m = x['m']
        for j, y in enumerate(m['years']):
            put(w2, r2, [x['tk'], y, m['rev'][j], m['oi'][j], m['om'][j], m['ni'][j], m['fcf'][j], m['sh'][j], m['debt'][j]], [None, '0', '#,##0', '#,##0', '0.0%', '#,##0', '#,##0', '#,##0', '#,##0']); r2 += 1
    for kk, w in zip('ABCDEFGHI', [9, 7, 12, 12, 10, 12, 16, 12, 12]):
        w2.column_dimensions[kk].width = w
    w2.freeze_panes = 'A2'
    w3 = wb.create_sheet('안내'); head(w3, ['항목', '설명'])
    notes = [('자료', 'SEC 10-K 공시(XBRL) 최근 5개 사업연도. 금액은 백만 달러. 회사마다 쓰는 항목 이름이 달라 일부 칸이 비거나 어긋날 수 있습니다.'),
             ('FCF', '영업현금흐름 - 유형자산 투자. 리스·인수 대금은 반영하지 않습니다.'),
             ('읽는 법', '기준 회사가 경쟁사 중앙값보다 이익률·FCF 전환이 꾸준히 좋은지 보세요. 같이 나빠졌다면 업종 문제, 혼자 나빠졌다면 회사 문제일 가능성이 큽니다.'),
             ('한계', '주식수는 발행 주식 또는 가중평균 중 공시된 항목이라 해마다 정의가 다를 수 있습니다. 일회성 이익(자산 매각 등)은 걸러내지 못합니다. 경쟁사는 직접 고른 종목이며 사업이 완전히 같지 않을 수 있습니다.')]
    for i, t in enumerate(notes, 2):
        put(w3, i, list(t))
    for i, t in enumerate(skipped, len(notes) + 3):
        put(w3, i, ['제외: ' + t[0], t[1]])
    w3.column_dimensions['A'].width = 16; w3.column_dimensions['B'].width = 130
    for s_ in wb.worksheets:
        s_.sheet_view.showGridLines = False
    out = os.path.join(here, f'5년실적_경쟁사비교_{date.today():%Y%m%d}.xlsx')
    wb.save(out)
    print(f'\n비교 결과: {out}  ({len(out_rows)}종목, 제외 {len(skipped)})')


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        sys.exit('openpyxl이 없습니다. 명령 창에서  pip install openpyxl  을 실행한 뒤 다시 시작하세요. (실행하기.bat을 쓰면 자동 설치됩니다)')
    sp = os.path.join(here, '설정.json')
    try:
        saved = json.load(open(sp, encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}
    print('=== 적정주가 순위 도구 (버핏·멍거 방식)  버전 v47 ===\n(질문에 답하면 됩니다. 그냥 Enter를 누르면 [ ] 안의 값을 씁니다)\n')
    print('무엇을 볼까요?\n  1) 미국 순위   2) 한국 순위   3) 미국+한국   4) 내가 고른 종목\n'
          '  5) 세계 전체 (미국 SEC + 한국 DART + 그 밖의 나라 Yahoo 근사. 시총 표의 모든 종목, 수 시간 걸릴 수 있음)\n'
          '  6) 미국·한국 이외 나라만 (Yahoo 근사: 일본·중국·유럽·대만·인도·사우디 등)\n'
          '  7) 백테스트 (미국: 5년 전 공시로 매긴 점수가 그 뒤 수익률과 맞았는지 검증)\n'
          '  8) 진단 (한국·세계 연결이 잘 되는지 대표 종목 몇 개로 1~2분 점검)\n'
          '  9) 4개년 편향 점검 (받아 둔 미국 10개년 자료로, 4개년만 쓸 때 점수가 얼마나 후해지는지 측정. 인터넷 불필요)\n'
          '  10) 후보 사업보고서 점검 (직전 결과의 [핵심후보]에 대해 미국 10-K 최근 2건을 비교해 읽을 곳을 찍어 줌)\n'
          '  11) 한국 후보 사업보고서 점검 (직전 결과의 [핵심후보] 중 한국 종목을 DART 사업보고서로 점검. DART 키 필요)\n'
          '  12) 5년 실적·경쟁사 비교 (미국 종목: 기준 회사 1개 + 경쟁사들의 매출·이익률·FCF·환원·주식수를 한 표로)')
    m = ask(f"번호 [{saved.get('market', '1')}]: ", saved.get('market', '1'))
    m = m if m in ('1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12') else '1'
    saved['market'] = m
    ALL = 10 ** 6

    def to_top(txt, default):
        t = (txt or '').strip().replace(',', '')
        if t in ('전체', 'all', 'ALL'):
            return ALL
        return int(t) if t.isdigit() else default
    email = key = None
    watch_us, watch_kr = None, None
    if m == '9':
        run_calib(here)
        return
    if m == '12':
        while not email or '@' not in email or email.endswith('example.com'):
            email = ask(f"\n[미국] 본인 이메일 (SEC가 요구하는 연락처, 실제 주소){' [' + saved['email'] + ']' if saved.get('email') else ''}: ", saved.get('email'))
            if not email or '@' not in email:
                print('   이메일 형식이 아닙니다. 다시 입력하세요.'); email = None
        saved['email'] = email
        print('\n한 줄에 한 묶음씩 입력: 첫 티커가 기준 회사, 나머지가 경쟁사 (예: DPZ PZZA YUM). 빈 줄이면 시작합니다.')
        grps = []
        while True:
            ln = input(f'  묶음 {len(grps) + 1}: ').strip()
            if not ln:
                break
            tk_ = [t.upper() for t in re.split(r'[\s,]+', ln) if t]
            if tk_:
                grps.append(tk_)
        if not grps:
            sys.exit('입력된 종목이 없습니다.')
        json.dump(saved, open(sp, 'w', encoding='utf-8'), ensure_ascii=False)
        local = re.sub(r'[^A-Za-z0-9]', '', email.split('@')[0]) or 'User'
        run_compare(here, email, f'{local} PersonalResearch {email}', os.path.join(here, 'sec_cache'), grps)
        return
    if m == '11':
        key = (ask(f"\n[한국] DART 인증키 40자리{' [저장된 키 사용]' if saved.get('key') else ''}: ", saved.get('key')) or '').strip()
        if len(key) != 40:
            sys.exit('인증키는 영문·숫자 40자리입니다. 발급 방법은 사용설명서 3번을 보세요.')
        saved['key'] = key
        gsel = (ask(f"점검할 구분 (A·B가 기본, 예: AB 또는 ABCD) [{saved.get('rv_groups', 'AB')}]: ", saved.get('rv_groups', 'AB')) or 'AB').upper()
        gset = {g for g in gsel if g in 'ABCD'} or {'A', 'B'}
        saved['rv_groups'] = ''.join(sorted(gset))
        json.dump(saved, open(sp, 'w', encoding='utf-8'), ensure_ascii=False)
        run_review_kr(here, key, gset)
        return
    if m == '10':
        while not email or '@' not in email or email.endswith('example.com'):
            email = ask(f"\n[미국] 본인 이메일 (SEC가 요구하는 연락처, 실제 주소){' [' + saved['email'] + ']' if saved.get('email') else ''}: ", saved.get('email'))
            if not email or '@' not in email:
                print('   이메일 형식이 아닙니다. 다시 입력하세요.'); email = None
        saved['email'] = email
        gsel = (ask(f"점검할 구분 (A·B가 기본, 예: AB 또는 ABCD) [{saved.get('rv_groups', 'AB')}]: ", saved.get('rv_groups', 'AB')) or 'AB').upper()
        gset = {g for g in gsel if g in 'ABCD'} or {'A', 'B'}
        saved['rv_groups'] = ''.join(sorted(gset))
        json.dump(saved, open(sp, 'w', encoding='utf-8'), ensure_ascii=False)
        local = re.sub(r'[^A-Za-z0-9]', '', email.split('@')[0]) or 'User'
        run_review(here, email, f'{local} PersonalResearch {email}', os.path.join(here, 'sec_cache'), gset)
        return
    if m == '8':
        key = (ask(f"\n[한국] DART 인증키 40자리 (없으면 Enter로 건너뜀){' [저장된 키 사용]' if saved.get('key') else ''}: ", saved.get('key')) or '').strip()
        key = key if len(key) == 40 else None
        if key:
            saved['key'] = key
        json.dump(saved, open(sp, 'w', encoding='utf-8'), ensure_ascii=False)
        run_diag(here, key, float(saved.get('fx', 1400)))
        return
    if m == '7':
        while not email or '@' not in email or email.endswith('example.com'):
            email = ask(f"\n[미국] 본인 이메일 (SEC가 요구하는 연락처, 실제 주소){' [' + saved['email'] + ']' if saved.get('email') else ''}: ", saved.get('email'))
            if not email or '@' not in email:
                print('   이메일 형식이 아닙니다. 다시 입력하세요.'); email = None
        saved['email'] = email
        tb = saved.get('top_bt', 200)
        saved['top_bt'] = to_top(ask(f"[백테스트] 시총 상위 몇 개 종목? ('전체' 가능, 많을수록 신뢰도↑·시간↑) [{'전체' if tb >= ALL else tb}]: ", ''), tb)
        hb = saved.get('hor_bt', 5)
        saved['hor_bt'] = max(2, min(5, to_top(ask(f"[백테스트] 몇 년 전 시점으로 볼까요? (2~5, 받아 둔 자료가 10개년이라 6년 이상은 과거 점수를 못 만듭니다) [{min(hb, 5)}]: ", ''), hb)))
        json.dump(saved, open(sp, 'w', encoding='utf-8'), ensure_ascii=False)
        csv_path = os.path.join(here, 'companies.csv')
        ensure_csv(here, csv_path)
        local = re.sub(r'[^A-Za-z0-9]', '', email.split('@')[0]) or 'User'
        run_backtest(here, email, saved['top_bt'], saved['hor_bt'], csv_path, f'{local} PersonalResearch {email}', os.path.join(here, 'sec_cache'))
        return
    if m == '4':
        raw = ask(f"\n종목 코드를 쉼표로 구분해 입력 (미국 예: AAPL, MSFT  한국 예: 005930, 000660)\n{' [' + saved['watch'] + ']' if saved.get('watch') else ''}: ", saved.get('watch')) or ''
        saved['watch'] = raw
        toks = [t.strip().upper() for t in re.split(r'[,\s]+', raw) if t.strip()]
        watch_kr = {t[:6] for t in toks if re.fullmatch(r'\d{6}(\.(KS|KQ))?', t)}
        watch_us = {t for t in toks if t not in watch_kr and not re.fullmatch(r'\d{6}(\.(KS|KQ))?', t)}
        if not toks:
            sys.exit('종목을 입력하지 않았습니다.')
    need_us = m in ('1', '3', '5') or bool(watch_us)
    need_kr = m in ('2', '3', '5') or bool(watch_kr)
    need_world = m in ('5', '6')
    if m in ('5', '6'):
        print('\n※ 전체 시장 모드: 시총 표(약 5,000종목)의 모든 종목을 처리합니다(미국 SEC, 한국 DART, 그 밖은 Yahoo). 처음에는 수 시간 걸리지만,\n   중간에 창을 닫거나 오류가 나도 지금까지의 자료는 저장되어 다음 실행 때 이어집니다. (Ctrl+C로 중단하면 그때까지의 결과를 저장합니다)')
    if need_us:
        while not email or '@' not in email or email.endswith('example.com'):
            email = ask(f"\n[미국] 본인 이메일 (SEC가 요구하는 연락처, 실제 주소){' [' + saved['email'] + ']' if saved.get('email') else ''}: ", saved.get('email'))
            if not email or '@' not in email:
                print('   이메일 형식이 아닙니다. 다시 입력하세요.'); email = None
        saved['email'] = email
        if m == '5':
            saved['top_us'] = ALL
        elif m != '4':
            tu = saved.get('top_us', 150)
            saved['top_us'] = to_top(ask(f"[미국] 시총 상위 몇 개? ('전체' 입력 가능) [{'전체' if tu >= ALL else tu}]: ", ''), tu)
        prev = int(saved.get('top_text', 0) or 0)
        yn = ask(f"[미국] 사업보고서(10-K) 문장 분석도 할까요?  1) 안 함  2) 함 (종목당 수 초~수십 초 더 걸림) [{2 if prev else 1}]: ", '2' if prev else '1')
        if yn == '2':
            saved['top_text'] = to_top(ask(f"      몇 개 종목을 분석할까요? (순위 상위부터, 예: 30) [{prev or 30}]: ", ''), prev or 30)
        else:
            saved['top_text'] = 0
    if need_kr:
        while not key or len(key) != 40:
            key = (ask(f"\n[한국] DART 인증키 40자리{' [저장된 키 사용]' if saved.get('key') else ''}: ", saved.get('key')) or '').strip()
            if len(key) != 40:
                print('   인증키는 영문·숫자 40자리입니다. 발급 방법은 사용설명서 3번을 보세요.'); key = None
        saved['key'] = key
        saved['fx'] = float(ask(f"[한국] 원/달러 환율 [{saved.get('fx', 1400)}]: ", str(saved.get('fx', 1400))).replace(',', ''))
        if m == '5':
            saved['top_kr'] = ALL
        elif m != '4':
            tk = saved.get('top_kr', 50)
            saved['top_kr'] = to_top(ask(f"[한국] 시총 상위 한국 종목 몇 개? ('전체' 입력 가능) [{'전체' if tk >= ALL else tk}]: ", ''), tk)
    if need_world:
        if m == '5':
            saved['top_world'] = ALL
        else:
            tw = saved.get('top_world', 100)
            saved['top_world'] = to_top(ask(f"[세계] 시총 상위 몇 개? (미국·한국 제외, '전체' 입력 가능) [{'전체' if tw >= ALL else tw}]: ", ''), tw)
    if True:  # 모든 모드 공통 질문
        ub = ask(f"\n[공통] 종목별 베타(5년 월간 주가, Yahoo)로 할인율을 조정할까요?  1) 안 함(모두 1.0)  2) 함 (처음에는 수 분 더 걸림) [{2 if saved.get('use_beta') else 1}]: ", '2' if saved.get('use_beta') else '1')
        saved['use_beta'] = (ub == '2')
    json.dump(saved, open(sp, 'w', encoding='utf-8'), ensure_ascii=False)
    csv_path = os.path.join(here, 'companies.csv')
    ensure_csv(here, csv_path)
    results = []
    if need_us:
        local = re.sub(r'[^A-Za-z0-9]', '', email.split('@')[0]) or 'User'
        ua = f'{local} PersonalResearch {email}'
        results.append(('미국', run_us(here, email, saved.get('top_us', 150), csv_path, ua, os.path.join(here, 'sec_cache'), only=watch_us, text_top=saved.get('top_text', 0), use_beta=saved['use_beta'])))
    if need_kr:
        results.append(('한국', run_kr(here, key, saved['fx'], saved.get('top_kr', 50), csv_path, only=watch_kr, use_beta=saved['use_beta'])))
    if need_world:
        results.append(('세계기타', run_world(here, csv_path, saved.get('top_world', 100), use_beta=saved['use_beta'])))
    done = [(n, r) for n, r in results if r and r[0]]
    if len(done) >= 2:
        write_combined(here, done)
    if m == '4':
        known = {c[0] for c in read_csv(csv_path)}
        known |= {k[:6] for k in known}
        nf = sorted(t for t in (watch_us | watch_kr) if t not in known)
        if nf:
            print(f"\n※ 시가총액 표(companies.csv)에 없어 계산하지 못한 종목: {', '.join(nf)}")
            print('   companies.csv 맨 아래에  0,회사이름,심볼,시가총액(달러),주가(달러),USA  한 줄을 추가하면 다음 실행부터 계산됩니다.')


if __name__ == '__main__':
    main()
