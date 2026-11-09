# -*- coding: utf-8 -*-
"""적정주가 폰 앱 서버. v47 계산 엔진(engine.py)을 그대로 쓰고, 폰 화면(static/)에 JSON으로 결과를 준다.
SEC·DART·Yahoo는 브라우저가 직접 부를 수 없어 이 서버가 대신 받아 계산한다."""
import hmac, json, os, re, threading, time, traceback, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from flask import Flask, jsonify, request, send_from_directory

import engine as E

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('DATA_DIR', '/tmp/jj_data')
os.makedirs(DATA, exist_ok=True)
SEC_CACHE = os.path.join(DATA, 'sec_cache'); os.makedirs(SEC_CACHE, exist_ok=True)
DART_CACHE = os.path.join(DATA, 'dart_cache'); os.makedirs(DART_CACHE, exist_ok=True)
WORLD_CACHE = os.path.join(DATA, 'world_cache'); os.makedirs(WORLD_CACHE, exist_ok=True)
SETTINGS = os.path.join(DATA, 'settings.json')

app = Flask(__name__, static_folder=os.path.join(BASE, 'static'), static_url_path='')
POOL = ThreadPoolExecutor(max_workers=1)      # SEC·Yahoo 요청 한도와 메모리 때문에 한 번에 하나씩
JOBS, JLOCK = {}, threading.Lock()
from collections import OrderedDict
CTX = OrderedDict()      # 최근 분석한 종목의 계산 재료(가정 바꿔 보기용, 메모리 보관)


# ---------- 설정·인증 ----------
def settings():
    s = {}
    try:
        s = json.load(open(SETTINGS, encoding='utf-8'))
    except (OSError, ValueError):
        pass
    return {
        'contact': s.get('contact') or os.environ.get('SEC_CONTACT', ''),
        'dart_key': s.get('dart_key') or os.environ.get('DART_KEY', ''),
        'fx': float(s.get('fx') or os.environ.get('FX_KRW', 1400)),
    }


def ua_string():
    c = settings()['contact']
    local = re.sub(r'[^A-Za-z0-9]', '', c.split('@')[0]) if '@' in c else ''
    return f'{local or "User"} PersonalResearch {c}' if c else ''


def authed():
    pw = os.environ.get('APP_PASSWORD', '')
    if not pw:
        return False
    return hmac.compare_digest(request.headers.get('X-App-Key', ''), pw)


@app.before_request
def guard():
    if request.path.startswith('/api/') and request.path != '/api/health':
        if not os.environ.get('APP_PASSWORD'):
            return jsonify(error='서버에 APP_PASSWORD(앱 비밀번호)가 설정되지 않았습니다. 배포 안내 3단계를 확인하세요.'), 503
        if not authed():
            return jsonify(error='비밀번호가 맞지 않습니다.'), 401


# ---------- 시장 구분·시세 ----------
def classify(tk):
    t = tk.strip().upper().replace(' ', '')
    if re.fullmatch(r'\d{6}(\.(KS|KQ))?', t):
        return 'KR', t[:6]
    return 'OTHER', t


def sec_map():
    ua = ua_string()
    if not ua:
        raise ValueError('SEC 접속에 쓸 연락처 이메일이 필요합니다. [설정]에서 입력하세요.')
    tick = E.cached(os.path.join(SEC_CACHE, 'company_tickers.json'), 'https://www.sec.gov/files/company_tickers.json', ua)
    return {v['ticker'].upper(): (str(v['cik_str']).zfill(10), v['title']) for v in tick.values()}


def yf_mod():
    E.ensure_yfinance()
    import yfinance as yf
    return yf


def get_quote(sym, price_override=None):
    """-> dict(price, mcap, currency, name) 현지 통화. 실패하면 None 값."""
    q = {'price': None, 'mcap': None, 'currency': None, 'name': None}
    try:
        yf = yf_mod()
        t = yf.Ticker(sym)
        try:
            fi = t.fast_info
            q['price'] = float(fi['last_price']) if fi['last_price'] else None
            q['mcap'] = float(fi['market_cap']) if fi['market_cap'] else None
            q['currency'] = fi['currency']
        except Exception:  # noqa: BLE001
            pass
        if q['price'] is None or q['mcap'] is None:
            try:
                info = t.info or {}
                q['price'] = q['price'] or info.get('currentPrice') or info.get('regularMarketPrice')
                q['mcap'] = q['mcap'] or info.get('marketCap')
                q['currency'] = q['currency'] or info.get('currency')
                q['name'] = info.get('longName') or info.get('shortName')
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    if price_override:
        q['price'] = float(price_override)
        q['mcap'] = None          # 직접 입력한 가격이면 주식 수(공시)로 시총을 다시 만든다
    return q


def usd_rate(cur, fxc):
    """현지통화 1단위 = 몇 USD"""
    if cur in (None, '', 'USD'):
        return 1.0
    return E.fx_to_usd(yf_mod(), cur, fxc)


# ---------- 재무 수집(시장별) ----------
def get_financials(tk, log):
    """-> (d, notes, meta). meta: market, name, sym, cik/corp, A(가정), lc(현지통화/USD), cur"""
    mk, code = classify(tk)
    st = settings()
    if mk == 'KR':
        if len(st['dart_key']) != 40:
            raise ValueError('한국 종목은 DART 인증키(40자리)가 필요합니다. [설정]에서 입력하세요.')
        log('DART 기업 목록 확인')
        corp = E.load_corp_codes(st['dart_key'], DART_CACHE)
        if code not in corp:
            raise ValueError(f'{code}: DART 기업 목록에 없습니다(우선주이거나 비상장).')
        cc, kname = corp[code]
        if any(w in kname for w in E.FIN_WORDS):
            raise ValueError('금융업으로 추정되어 제외합니다(이 모델은 비금융 기업용).')
        fxc = {}
        rate = usd_rate('KRW', fxc)
        fx = (1 / rate) if rate else st['fx']
        log('DART 재무제표 받는 중(처음엔 1~2분)')
        d, notes = E.build_company(st['dart_key'], cc, DART_CACHE, fx)
        if d is None:
            raise ValueError('; '.join(notes))
        sym = None
        for suf in ('.KS', '.KQ'):
            if get_quote(code + suf)['price']:
                sym = code + suf; break
        notes = notes + E.kr_fill_da(d, sym or code + '.KS', fx)
        d, n2 = E.check_and_fill(d, {})
        if d is None:
            raise ValueError('; '.join(n2))
        return d, notes + n2, {'market': 'KR', 'name': kname, 'sym': sym or code + '.KS', 'A': E.KR_ASSUME, 'lc': fx, 'cur': '원', 'code': code, 'corp': cc}
    m = sec_map()
    t = code
    if t in m or t.replace('-', '.') in m:
        cik, title = m.get(t) or m[t.replace('-', '.')]
        log('SEC 공시(10개년) 받는 중(처음엔 10~30초)')
        res, why = E.load_extracted(cik, SEC_CACHE, ua_string())
        if res is None:
            raise ValueError(why)
        d, used = res
        d, notes = E.check_and_fill(d, used)
        if d is None:
            raise ValueError('; '.join(notes))
        return d, notes, {'market': 'US', 'name': title, 'sym': t, 'A': None, 'lc': 1.0, 'cur': '$', 'cik': cik}
    log('Yahoo Finance 재무제표 받는 중(근사치)')
    yf = yf_mod()
    fxc = {}
    d, notes = E.world_extract(yf, t, None, fxc)
    if d is None:
        raise ValueError('; '.join(notes) + ' (미국 티커는 AAPL, 한국은 005930, 그 밖은 7203.T 처럼 야후 형식)')
    d, n2 = E.check_and_fill(d, {}, min_years=4)
    if d is None:
        raise ValueError('; '.join(n2))
    return d, notes + n2, {'market': 'WORLD', 'name': t, 'sym': t, 'A': None, 'lc': None, 'cur': None}


def fill_d(d):
    for k in ('revenue', 'operating_income', 'net_income', 'cfo', 'capex', 'shares_out', 'dividends_paid', 'buybacks', 'total_debt'):
        if not d.get(k):
            d[k] = [None] * len(d['years'])
    return d


def num(x, nd=None):
    if x is None:
        return None
    try:
        if x != x:
            return None
        return round(float(x), nd) if nd is not None else float(x)
    except (TypeError, ValueError):
        return None


# ---------- 분석 ----------
def analyze(tk, price=None, log=lambda s: None):
    d, notes, meta = get_financials(tk, log)
    log('시세 확인')
    q = get_quote(meta['sym'], price)
    fxc = {}
    if meta['market'] == 'WORLD':
        cur = q['currency']
        rate = usd_rate(cur, fxc) if cur else None
        if not rate:
            raise ValueError('시세 통화·환율을 확인하지 못했습니다. 잠시 후 다시 시도하거나 현재가를 직접 입력하세요.')
        meta['lc'] = 1 / rate; meta['cur'] = cur
        if q.get('name'):
            meta['name'] = q['name']
    elif meta['market'] == 'US' and q.get('name') is None:
        pass
    px, lc = q['price'], meta['lc']
    sv = [x for x in d['shares_out'] if x]
    shares_m = None
    if q['mcap'] and px:
        shares_m = q['mcap'] / px / 1e6
    elif sv:
        shares_m = sv[-1]
        notes.append('시세에서 주식 수를 얻지 못해 최근 공시의 주식 수를 사용')
    if not px:
        raise ValueError('현재가를 가져오지 못했습니다(Yahoo 일시 차단 가능). 현재가를 직접 입력해 다시 시도하세요.')
    mcap_usd_m = (px * shares_m) / lc if shares_m else None
    if mcap_usd_m is None:
        raise ValueError('시가총액을 계산하지 못했습니다.')
    excl = ('C2',) if E.split_jump(d['shares_out']) else ()
    if excl:
        notes.append('주식 수 급변(분할 의심) → C2 제외')
    A = meta['A']
    r = E.evaluate(d, mcap_usd_m, A=A, exclude=excl)
    E.add_scenarios(r, d, mcap_usd_m, notes, A=A, exclude=excl)
    tier = r['tier']
    margin = tier[2]
    fv = r['fv_rep']
    ps = lambda v: None if (v is None or not shares_m) else v * lc / shares_m
    fair = ps(fv)
    buy = fair * (1 - margin) if (fair is not None and margin is not None) else None
    if r['composite'] < E.TIERS[1][0]:
        verdict = '투자 제외'
    elif fv is None:
        verdict = '산정 불가'
    elif buy is not None and px <= buy:
        verdict = '매수 기준 이하'
    else:
        verdict = '매수 기준 초과'
    CTX[meta['sym'].upper()] = {'d': d, 'A': A, 'mcap': mcap_usd_m, 'excl': excl, 'shares_m': shares_m, 'lc': lc, 'px': px, 'tier': tier, 'score': r['composite'], 'g1': r['g1']}
    while len(CTX) > 30:
        CTX.popitem(last=False)
    peers = E.peer_metrics(fill_d(d))
    flags = list(peers['flags'])
    out = {
        'ticker': meta['sym'], 'name': meta['name'], 'market': meta['market'], 'cur': meta['cur'],
        'asof': date.today().isoformat(),
        'price': num(px, 4), 'fair': num(fair, 4), 'buy': num(buy, 4), 'opt': num(ps(r.get('fv_opt')), 4),
        'gap': (fair / px - 1) if (fair and px) else None,
        'score': num(r['composite'], 2), 'tier': tier[1], 'margin': margin, 'verdict': verdict,
        'areas': {k: num(v, 2) for k, v in r['area'].items()},
        'irr': num(r['irr'], 4), 'wacc': num(r['wacc'], 4), 'g1': num(r['g1'], 4), 'r': num(r['r'], 4),
        'cagr': num(r['cagr'], 4), 'n': r['n'], 'coverage': num(r['coverage'], 2), 'gates': r['gates'],
        'mult': (mcap_usd_m / r['base_oe']) if (r.get('base_oe') and r['base_oe'] > 0) else None,
        'gap_avg': ((ps(r.get('fv_all')) / px - 1) if (r.get('fv_all') and px) else None),
        'trend_down': bool(r.get('trend_down')),
        'mcap_usd_m': num(mcap_usd_m, 1), 'notes': notes,
        'metrics': {k: num(v, 4) for k, v in r['metrics'].items()},
        'scores': {k: num(v, 2) for k, v in r['scores'].items()},
        'five': pack_peer(peers), 'flags': flags,
    }
    return out, {'gap': out['gap'], 'gap_avg': out['gap_avg'], 'mult': out['mult']}, meta


def pack_peer(m):
    return {k: ([num(x, 4) for x in m[k]] if isinstance(m[k], list) else m[k]) for k in
            ('years', 'rev', 'oi', 'ni', 'fcf', 'om', 'sh', 'debt')} | {
        k: num(m[k], 4) for k in ('rev_cagr', 'ni_cagr', 'fcf_cagr', 'om_first', 'om_last', 'om_max', 'fcf_ni', 'payout', 'sh_chg', 'last_rev_g', 'last_oi_g')} | {'flags': m['flags']}


# ---------- 비교 ----------
def compare(groups, log):
    from statistics import median
    rows = []
    for grp in groups:
        for i, tk in enumerate(grp):
            tk = tk.strip().upper()
            if not tk:
                continue
            log(f'{tk} 자료 받는 중')
            try:
                d, notes, meta = get_financials(tk, lambda s: None)
                m = E.peer_metrics(fill_d(d))
                rows.append({'group': grp[0].upper(), 'role': '기준' if i == 0 else '경쟁사', 'ticker': tk, 'name': meta['name'],
                             'market': meta['market'], 'm': pack_peer(m)})
            except Exception as e:  # noqa: BLE001
                rows.append({'group': grp[0].upper(), 'role': '기준' if i == 0 else '경쟁사', 'ticker': tk, 'error': str(e)[:200]})
    meds = {}
    for grp in groups:
        g = grp[0].upper()
        peers = [r['m'] for r in rows if r['group'] == g and r['role'] == '경쟁사' and 'm' in r]
        if peers:
            meds[g] = {k: (median([p[k] for p in peers if p[k] is not None]) if any(p[k] is not None for p in peers) else None)
                       for k in ('rev_cagr', 'ni_cagr', 'fcf_cagr', 'om_first', 'om_last', 'om_max', 'fcf_ni', 'payout', 'sh_chg', 'last_rev_g')}
    return {'rows': rows, 'medians': meds}


# ---------- 사업보고서 점검 ----------
def review(tk, translate, log):
    mk, code = classify(tk)
    st = settings()
    ana = c = None
    try:
        log('수치 분석')
        ana, c, _ = analyze(tk, None, lambda s: None)
    except Exception as e:  # noqa: BLE001
        log(f'수치 분석은 건너뜀: {str(e)[:80]}')
    if mk == 'KR':
        key = st['dart_key']
        if len(key) != 40:
            raise ValueError('DART 인증키가 필요합니다. [설정]에서 입력하세요.')
        corp = E.load_corp_codes(key, DART_CACHE)
        if code not in corp:
            raise ValueError('DART 기업 목록에 없습니다.')
        cc = corp[code][0]
        log('사업보고서 목록 확인')
        rl = E.kr_list_reports(key, cc, DART_CACHE)
        if not rl:
            raise ValueError('사업보고서를 찾지 못했습니다.')
        log('사업보고서 본문 받는 중(1~3분)')
        cur = E.kr_report_text(key, cc, rl[0]['rcept_no'], DART_CACHE)
        prev = E.kr_report_text(key, cc, rl[1]['rcept_no'], DART_CACHE) if len(rl) > 1 else ''
        rv = E.review_kr(cur, prev, int(rl[0].get('rcept_dt', '0')[:4] or 0) or None)
        qs = ['이 회사의 이익이 10년 뒤에도 같은 방식으로 나올 근거는 무엇인가?(점유율·브랜드·장기계약 문장의 수치를 확인)']
        if c and c.get('gap') is not None and c.get('gap_avg') is not None and c['gap'] - c['gap_avg'] > 0.3:
            qs.append(f"최근 3년 이익 기준 괴리율({c['gap']:.0%})이 전체평균 기준({c['gap_avg']:.0%})보다 크게 높다. 최근 이익이 정점인지 확인하세요.")
        qs += ['지배구조: 최대주주·특수관계인 지분율, 배당 성향, 자사주 정책을 [VII. 주주에 관한 사항]에서 확인하세요.', '차입금 만기와 이자 부담을 이익으로 감당할 수 있는가?']
        return {'market': 'KR', 'name': corp[code][1], 'report': rl[0].get('report_nm'), 'filed': rl[0].get('rcept_dt'),
                'url': f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rl[0]['rcept_no']}", 'rv': kr_pack(rv), 'questions': qs, 'analysis': ana}
    m = sec_map()
    if code not in m and code.replace('-', '.') not in m:
        raise ValueError('미국 SEC 10-K 제출 기업이 아닙니다(미국 티커 또는 한국 6자리 코드만 점검합니다).')
    cik, title = m.get(code) or m[code.replace('-', '.')]
    ua = ua_string()
    log('10-K 목록 확인')
    fl = E.list_10k(cik, ua, SEC_CACHE)
    if not fl:
        raise ValueError('10-K를 찾지 못했습니다.')
    log('10-K 본문 받는 중(10~40초)')
    cur = E.get_10k_text(cik, fl[0][1], fl[0][2], ua, SEC_CACHE)
    prev = E.get_10k_text(cik, fl[1][1], fl[1][2], ua, SEC_CACHE) if len(fl) > 1 else ''
    rv = E.review_10k(cur, prev, int(fl[0][3][:4]))
    if translate:
        log('한글 번역 중')
        E._tr_open(DATA)
        for nm, (kind, ev) in list(rv['flags'].items()):
            rv['flags'][nm] = (kind, ev, E.ko(ev))
        for cat, (n, ss) in list(rv['moat'].items()):
            rv['moat'][cat] = (n, ss, [E.ko(x) for x in ss])
        rv['risk_new_ko'] = [E.ko(x) for x in rv['risk_new'][:4]]
        E._tr_save()
    cc = dict(c or {})
    cc.setdefault('gap', 0); cc.setdefault('gap_avg', None); cc.setdefault('mult', None)
    qs = E.make_questions(cc, rv)
    return {'market': 'US', 'name': title, 'report': '10-K', 'filed': fl[0][3],
            'url': f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{fl[0][1].replace('-', '')}/{fl[0][2]}",
            'rv': us_pack(rv, translate), 'questions': qs, 'analysis': ana}


def us_pack(rv, tr):
    flags = []
    for nm, v in rv['flags'].items():
        flags.append({'name': nm, 'kind': v[0], 'text': v[1], 'ko': v[2] if len(v) > 2 else None})
    moat = []
    for cat, v in rv['moat'].items():
        if v[0]:
            moat.append({'cat': cat, 'n': v[0], 'text': v[1], 'ko': v[2] if len(v) > 2 else None})
    return {'flags': flags, 'moat': moat, 'risk_n': rv['risk_n'], 'risk_new_n': rv['risk_new_n'], 'risk_new': rv['risk_new'][:4],
            'risk_new_ko': rv.get('risk_new_ko'), 'biz': rv['biz'], 'bb': rv['bb'], 'div': rv['div'], 'liq': rv['liq'], 'scope': rv.get('moat_scope')}


def kr_pack(rv):
    flags = [{'name': nm, 'kind': v[0], 'text': v[1], 'ko': None} for nm, v in rv['flags'].items()]
    moat = [{'cat': cat, 'n': v[0], 'text': v[1], 'ko': None} for cat, v in rv['moat'].items() if v[0] and cat not in ('경쟁 환경', '성장 전략')]
    return {'flags': flags, 'moat': moat, 'risk_n': rv['base_n'], 'risk_new_n': rv['new_n'], 'risk_new': rv['new'], 'biz': rv['biz'], 'bb': [], 'div': [], 'liq': [], 'scope': rv['scope']}


# ---------- 가정 바꿔 보기 ----------
def whatif(sym, g1, shift):
    ctx = CTX.get(sym.upper())
    if not ctx:
        return None
    g1 = max(0.0, min(0.20, float(g1)))
    shift = max(-0.03, min(0.05, float(shift)))
    base = ctx['A'] or {}
    A = {**base, 'g1_override': g1, 'r_shift': shift, 'g_cap': max(g1, base.get('g_cap', E.ASSUME['g_cap']))}
    r = E.evaluate(ctx['d'], ctx['mcap'], A=A, exclude=ctx['excl'])
    sh, lc, px, tier = ctx['shares_m'], ctx['lc'], ctx['px'], ctx['tier']
    ps = lambda v: None if (v is None or not sh) else v * lc / sh
    fair = ps(r['fv_rep'])
    margin = tier[2]
    buy = fair * (1 - margin) if (fair is not None and margin is not None) else None
    return {'fair': num(fair, 4), 'buy': num(buy, 4), 'gap': (fair / px - 1) if (fair and px) else None,
            'irr': num(r['irr'], 4), 'r': num(r['r'], 4), 'g1': g1, 'shift': shift,
            'verdict': ('투자 제외' if ctx['score'] < E.TIERS[1][0] else '산정 불가' if r['fv_rep'] is None else ('매수 기준 이하' if (buy is not None and px <= buy) else '매수 기준 초과'))}


def watch(tickers, log):
    out = []
    for i, tk in enumerate(tickers, 1):
        log(f'{i}/{len(tickers)} {tk}')
        try:
            a, _, _ = analyze(tk, None, lambda s: None)
            out.append({'t': tk, 'ok': True, 'name': a['name'], 'sym': a['ticker'], 'cur': a['cur'], 'price': a['price'], 'fair': a['fair'], 'buy': a['buy'],
                        'gap': a['gap'], 'score': a['score'], 'tier': a['tier'], 'verdict': a['verdict'], 'irr': a['irr'], 'gates': a['gates'],
                        'trend_down': a['trend_down'], 'asof': a['asof']})
        except Exception as e:  # noqa: BLE001
            out.append({'t': tk, 'ok': False, 'error': str(e)[:160]})
    return out


# ---------- 작업 큐 ----------
def run_job(jid, kind, p):
    job = JOBS[jid]

    def log(msg):
        job['msg'] = msg

    try:
        job['status'] = 'running'
        if kind == 'analyze':
            out, _, _ = analyze(p['ticker'], p.get('price'), log)
        elif kind == 'compare':
            out = compare(p['groups'], log)
        elif kind == 'review':
            out = review(p['ticker'], bool(p.get('translate', True)), log)
        elif kind == 'watch':
            out = watch(p['tickers'], log)
        else:
            raise ValueError('알 수 없는 작업')
        job.update(status='done', result=out, msg='완료')
    except E.SecError as e:
        s = str(e)
        job.update(status='error', error=('SEC가 요청 과다로 이 서버를 잠시 차단했습니다. 30분~1시간 뒤 다시 시도하세요.' if s == 'RATE' else 'SEC 접속 오류: ' + s[:300]))
    except E.DartError as e:
        s = str(e)
        job.update(status='error', error=('DART 일일 사용 한도에 도달했습니다. 내일 다시 시도하세요.' if s == 'QUOTA' else 'DART 오류: ' + s[:300]))
    except (ValueError, SystemExit) as e:
        job.update(status='error', error=str(e)[:400])
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        job.update(status='error', error=f'처리 중 오류: {type(e).__name__} {str(e)[:250]}')
    finally:
        job['t1'] = time.time()


def prune():
    now = time.time()
    with JLOCK:
        for k in [k for k, v in JOBS.items() if now - v['t0'] > 3600]:
            JOBS.pop(k, None)


@app.post('/api/jobs')
def api_job():
    body = request.get_json(silent=True) or {}
    kind = body.get('kind')
    if kind not in ('analyze', 'compare', 'review', 'watch'):
        return jsonify(error='잘못된 요청'), 400
    p = {}
    if kind in ('analyze', 'review'):
        tk = str(body.get('ticker', '')).strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-^=]{1,16}", tk):
            return jsonify(error='티커 형식이 올바르지 않습니다(예: AAPL, 005930, 7203.T).'), 400
        p['ticker'] = tk
        if body.get('price'):
            try:
                p['price'] = float(body['price'])
            except (TypeError, ValueError):
                return jsonify(error='현재가는 숫자로 입력하세요.'), 400
        p['translate'] = body.get('translate', True)
    elif kind == 'watch':
        tks = [str(x).strip() for x in (body.get('tickers') or []) if re.fullmatch(r"[A-Za-z0-9.\-^=]{1,16}", str(x).strip())][:25]
        if not tks:
            return jsonify(error='관심종목이 없습니다.'), 400
        p['tickers'] = tks
    else:
        groups = []
        for g in (body.get('groups') or [])[:4]:
            tks = [str(x).strip() for x in g if re.fullmatch(r"[A-Za-z0-9.\-^=]{1,16}", str(x).strip())][:6]
            if tks:
                groups.append(tks)
        if not groups:
            return jsonify(error='비교할 티커를 입력하세요.'), 400
        p['groups'] = groups
    prune()
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {'status': 'queued', 'msg': '대기 중', 't0': time.time(), 'kind': kind}
    POOL.submit(run_job, jid, kind, p)
    return jsonify(id=jid)


@app.get('/api/jobs/<jid>')
def api_job_get(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify(error='작업을 찾을 수 없습니다(서버가 다시 시작됐을 수 있습니다). 다시 실행하세요.'), 404
    return jsonify({k: v for k, v in j.items() if k in ('status', 'msg', 'result', 'error', 'kind')})


@app.post('/api/whatif')
def api_whatif():
    b = request.get_json(silent=True) or {}
    try:
        r = whatif(str(b.get('ticker', '')), b.get('g1', 0.05), b.get('shift', 0.0))
    except (TypeError, ValueError):
        return jsonify(error='값이 올바르지 않습니다.'), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f'계산 오류: {str(e)[:150]}'), 500
    if r is None:
        return jsonify(error='서버가 다시 시작되어 이 종목의 계산 재료가 없습니다. 위의 [적정주가 계산]을 다시 눌러 주세요.'), 410
    return jsonify(r)


@app.get('/api/settings')
def api_settings_get():
    s = settings()
    return jsonify(contact=s['contact'], dart_key_set=len(s['dart_key']) == 40, fx=s['fx'])


@app.post('/api/settings')
def api_settings_set():
    b = request.get_json(silent=True) or {}
    cur = {}
    try:
        cur = json.load(open(SETTINGS, encoding='utf-8'))
    except (OSError, ValueError):
        pass
    if 'contact' in b:
        c = str(b['contact']).strip()
        if c and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', c):
            return jsonify(error='이메일 형식이 아닙니다.'), 400
        cur['contact'] = c
    if 'dart_key' in b and str(b['dart_key']).strip():
        k = str(b['dart_key']).strip()
        if not re.fullmatch(r'[A-Za-z0-9]{40}', k):
            return jsonify(error='DART 인증키는 영문·숫자 40자리입니다.'), 400
        cur['dart_key'] = k
    if 'fx' in b and b['fx']:
        try:
            cur['fx'] = float(b['fx'])
        except (TypeError, ValueError):
            return jsonify(error='환율은 숫자로 입력하세요.'), 400
    json.dump(cur, open(SETTINGS, 'w', encoding='utf-8'), ensure_ascii=False)
    return jsonify(ok=True)


@app.get('/api/health')
def health():
    return jsonify(ok=True, version='v47-app3', password_set=bool(os.environ.get('APP_PASSWORD')))


@app.get('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 7860)))
