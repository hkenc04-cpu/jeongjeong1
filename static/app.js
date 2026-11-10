'use strict';
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
};
let KEY = LS.get('key', '');
let tab = 'watch';
const state = { analyze: null, compare: null, review: null, busy: null, watch: null, edit: false };
let pend = null;                                   // 다른 탭에서 넘어올 때 자동 실행할 종목
const nkey = t => String(t || '').trim().toUpperCase().replace(/\.(KS|KQ)$/, '');
const today = () => new Date().toISOString().slice(0, 10);
const getW = () => LS.get('watch', []);
const inW = t => getW().some(x => x.t === nkey(t));
function toggleW(t, snap) {
  t = nkey(t); let w = getW();
  if (w.some(x => x.t === t)) w = w.filter(x => x.t !== t); else w.unshift({ t, add: today(), snap: snap || null });
  LS.set('watch', w); return w.some(x => x.t === t);
}
const getN = t => (LS.get('notes', {}) || {})[nkey(t)] || {};
function setN(t, patch) { const all = LS.get('notes', {}) || {}; const k = nkey(t); all[k] = { ...(all[k] || {}), ...patch, at: today() }; LS.set('notes', all); }
function nav(tabName, tk) { pend = { tab: tabName, tk }; tab = tabName; render(); window.scrollTo(0, 0); }
function paintOut(k) {
  const el = $('#out'); if (!el) return; el.innerHTML = outHtml(k);
  if (!state.busy && state[k] && !state[k].error) { if (k === 'analyze') bindAnalysisExtras(state.analyze.data); if (k === 'review') bindReviewExtras(state.review.data); }
}

async function api(path, opt = {}) {
  const r = await fetch(path, { ...opt, headers: { 'Content-Type': 'application/json', 'X-App-Key': KEY, ...(opt.headers || {}) } });
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (r.status === 401) { KEY = ''; LS.set('key', ''); render(); throw new Error('비밀번호가 맞지 않습니다.'); }
  if (!r.ok) throw new Error(j.error || ('서버 오류 ' + r.status));
  return j;
}

async function runJob(kind, body, onMsg) {
  const { id } = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ kind, ...body }) });
  const t0 = Date.now();
  for (;;) {
    await new Promise(r => setTimeout(r, 1800));
    const j = await api('/api/jobs/' + id);
    onMsg && onMsg(j.msg, Math.round((Date.now() - t0) / 1000));
    if (j.status === 'done') return j.result;
    if (j.status === 'error') throw new Error(j.error);
    if (Date.now() - t0 > 12 * 60 * 1000) throw new Error('시간이 너무 오래 걸립니다. 잠시 후 다시 시도하세요.');
  }
}

// ---------- 숫자 표기 ----------
function money(v, cur) {
  if (v == null) return '-';
  if (cur === '원') return Math.round(v).toLocaleString('ko-KR') + '원';
  if (cur === '$') return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return v.toLocaleString('en-US', { maximumFractionDigits: v < 100 ? 2 : 0 }) + ' ' + (cur || '');
}
const pct = (v, d = 0) => v == null ? '-' : (v * 100).toFixed(d) + '%';
const spct = (v, d = 0) => v == null ? '-' : (v >= 0 ? '+' : '') + (v * 100).toFixed(d) + '%';
const mm = v => v == null ? '-' : Math.abs(v) >= 1000 ? (v / 1000).toFixed(1) + 'B' : Math.round(v).toLocaleString();

function verdictPill(v) {
  const c = v === '매수 기준 이하' ? 'p-good' : v === '매수 기준 초과' ? 'p-warn' : 'p-gray';
  return `<span class="pill ${c}">${esc(v)}</span>`;
}
const mktName = { US: '미국 SEC 10개년', KR: '한국 DART 최대 10개년', WORLD: 'Yahoo 근사(4~5개년)' };

// ---------- 화면 ----------
function render() {
  const app = $('#app'); LS.set('tab', tab);
  const hi = tab === 'analyze' ? 'watch' : tab;
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === hi));
  $('#tabs').style.display = KEY ? 'flex' : 'none';
  if (!KEY) { app.innerHTML = loginHtml(); bindLogin(); return; }
  if (tab === 'watch') { app.innerHTML = watchHtml(); bindWatch(); }
  else if (tab === 'cands') { app.innerHTML = candsHtml(); bindCands(); }
  else if (tab === 'analyze') { app.innerHTML = analyzeHtml(); bindAnalyze(); }
  else if (tab === 'compare') { app.innerHTML = compareHtml(); bindCompare(); }
  else if (tab === 'review') { app.innerHTML = reviewHtml(); bindReview(); }
  else { app.innerHTML = settingsHtml(); bindSettings(); }
}
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => { tab = b.dataset.t; pend = null; render(); window.scrollTo(0, 0); });

function loginHtml() {
  return `<div class="card"><h3>앱 비밀번호</h3>
  <p class="hint">서버를 배포할 때 정한 비밀번호(APP_PASSWORD)를 입력하세요. 이 폰에 저장됩니다.</p>
  <input id="pw" type="password" placeholder="비밀번호" autocomplete="current-password">
  <button class="btn" id="pwgo">들어가기</button><div id="lerr"></div></div>`;
}
function bindLogin() {
  const go = async () => {
    const v = $('#pw').value.trim(); if (!v) return;
    KEY = v;
    try { await api('/api/settings'); LS.set('key', v); render(); }
    catch (e) { KEY = ''; $('#lerr').innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  };
  $('#pwgo').onclick = go; $('#pw').onkeydown = e => { if (e.key === 'Enter') go(); };
}

function progHtml(b) { return `<div class="prog"><div class="spin"></div><div><b>${esc(b.msg || '시작')}</b><br><span class="hint">${b.sec || 0}초 경과 · 처음 받는 종목은 1~3분 걸릴 수 있습니다</span></div></div>`; }

// ----- 분석 -----
function recent() { return LS.get('recent', []); }
function pushRecent(t) { const r = [t, ...recent().filter(x => x !== t)].slice(0, 12); LS.set('recent', r); }
function analyzeHtml() {
  const chips = recent().map(t => `<button class="chip" data-t="${esc(t)}">${esc(t)}</button>`).join('');
  return `<button class="lnk back" id="back">← 홈</button><div class="card"><h3>종목 분석</h3>
  <label>티커</label><input id="tk" placeholder="AAPL · 005930 · 7203.T · 0700.HK" autocapitalize="characters" autocorrect="off" spellcheck="false" enterkeyhint="go">
  <label>현재가 직접 입력 (선택 · 시세를 못 받을 때만)</label><input id="px" inputmode="decimal" placeholder="비워 두면 자동">
  <button class="btn" id="go">적정주가 계산</button>
  <div class="chips" id="chips">${chips}</div>
  <p class="hint" style="margin-top:10px">미국은 티커(AAPL), 한국은 6자리 코드(005930), 그 밖은 야후 형식(7203.T, ASML.AS). 금융업은 제외됩니다.</p></div>
  <div id="out">${outHtml('analyze')}</div>`;
}
function outHtml(k) {
  const b = state.busy;
  if (b && b.kind === k) return progHtml(b);
  const s = state[k]; if (!s) return '';
  if (s.error) return `<div class="err">${esc(s.error)}</div>`;
  if (k === 'analyze' && s.data) return analysisHtml(s.data);
  return s.html;
}
function bindAnalyze() {
  $('#back').onclick = () => { tab = 'watch'; pend = null; render(); window.scrollTo(0, 0); };
  const go = async (t) => {
    const tk = (t || $('#tk').value).trim(); if (!tk) return;
    const px = $('#px').value.trim();
    state.busy = { kind: 'analyze', msg: '요청 전송', sec: 0 }; state.analyze = null; paintOut('analyze');
    try {
      const r = await runJob('analyze', { ticker: tk, price: px || null }, (m, s) => { state.busy = { kind: 'analyze', msg: m, sec: s }; paintOut('analyze'); });
      pushRecent(nkey(tk)); state.analyze = { html: analysisHtml(r), data: r };
      if (inW(r.ticker)) { const w = getW(); const it = w.find(x => x.t === nkey(r.ticker)); if (it) { it.snap = snapOf(r); LS.set('watch', w); } }
    } catch (e) { state.analyze = { error: e.message }; }
    state.busy = null; if (tab === 'analyze') render();
  };
  $('#go').onclick = () => go(); $('#tk').onkeydown = e => { if (e.key === 'Enter') go(); };
  document.querySelectorAll('#chips .chip').forEach(c => c.onclick = () => { $('#tk').value = c.dataset.t; go(c.dataset.t); });
  if (pend && pend.tab === 'analyze') { const t = pend.tk; pend = null; $('#tk').value = t; go(t); }
  else if (state.analyze || state.busy) paintOut('analyze');
}

const AREA_ORDER = ['해자', '이익의 질', '자본배분', '재무건전성', '경영진·공시 품질'];
function analysisHtml(r) {
  const c = r.cur;
  const areas = AREA_ORDER.filter(a => r.areas[a] != null).map(a => `<div class="arow"><span>${esc(a)}</span><div class="bar"><i style="width:${Math.max(2, r.areas[a] / 5 * 100)}%"></i></div><span>${r.areas[a].toFixed(1)}</span></div>`).join('');
  const gapCls = r.gap == null ? '' : r.gap >= 0 ? 'color:var(--good)' : 'color:var(--bad)';
  const notes = (r.notes || []).map(n => `<div class="${n.startsWith('⚠') ? 'warn' : 'src'}">${esc(n)}</div>`).join('');
  const gates = (r.gates || []).map(g => `<div class="bad">⛔ 경고: ${esc(g)}</div>`).join('');
  const f = r.flags && r.flags.length ? `<h4>5년 실적 주의</h4>` + r.flags.map(x => `<div class="warn">${esc(x)}</div>`).join('') : '';
  const tierCls = r.tier === '우수' ? 'p-good' : r.tier === '양호' ? 'p-good' : r.tier === '보수적 검토' ? 'p-warn' : 'p-bad';
  const peak = (r.gap_avg != null && r.gap != null && r.gap - r.gap_avg > 0.3) ? `<div class="warn">최근 3년 이익 기준 괴리율(${spct(r.gap)})이 전체 평균 이익 기준(${spct(r.gap_avg)})보다 훨씬 큽니다. 최근 이익이 정점인지 먼저 확인하세요.</div>` : '';
  return `<div class="card">
    <div class="head"><div><div class="nm">${esc(r.name)}</div><div class="tk">${esc(r.ticker)} · ${esc(mktName[r.market] || '')}</div></div>${verdictPill(r.verdict)}</div>
    <div class="row" style="margin-top:10px"><button class="btn ghost sm" id="bw"></button><button class="btn ghost sm" id="brv">사업보고서 점검</button></div>
    <div class="nums">
      <div class="num"><small>현재가</small><b>${money(r.price, c)}</b></div>
      <div class="num"><small>적정주가</small><b>${money(r.fair, c)}</b></div>
      <div class="num hl"><small>매수 기준가</small><b>${money(r.buy, c)}</b></div></div>
    <div class="gap">괴리율 <b style="${gapCls}">${spct(r.gap)}</b> <span class="hint">(적정주가 ÷ 현재가 − 1)</span>${r.opt ? `<br><span class="hint">낙관 시나리오 적정주가 ${money(r.opt, c)}</span>` : ''}</div>
    ${peak}${gates}</div>
  <div class="card" id="wfc"><h3>가정 바꿔 보기</h3>
    <p class="hint">기본값은 모델의 보수적 가정입니다. 값을 움직여 적정주가가 얼마나 달라지는지 보세요. 조금만 바꿔도 크게 변하면 그만큼 불확실한 숫자입니다.</p>
    <label>1~5년 이익 성장률 <b id="g1v"></b></label><input type="range" id="g1" min="0" max="20" step="0.5">
    <label>요구수익률 조정 <b id="shv"></b></label><input type="range" id="sh" min="-3" max="5" step="0.5">
    <div class="gr" style="margin-top:10px"><div class="mini"><small>적정주가</small><b id="wf1">-</b></div><div class="mini"><small>매수 기준가</small><b id="wf2">-</b></div>
      <div class="mini"><small>괴리율</small><b id="wf3">-</b></div><div class="mini"><small>내재 수익률</small><b id="wf4">-</b></div></div>
    <div id="wmsg"></div><button class="btn ghost sm" id="wrs">기본값으로</button></div>
  <div class="card"><div class="head"><div><div class="big">${r.score.toFixed(2)}<span class="hint" style="font-size:14px;font-weight:500"> / 5</span></div></div><span class="pill ${tierCls}">${esc(r.tier)}</span></div>
    <div style="margin-top:8px">${areas}</div>
    <div class="gr" style="margin-top:10px">
      <div class="mini"><small>내재 수익률(현재가로 사면 연)</small><b>${pct(r.irr, 1)}</b></div>
      <div class="mini"><small>적용 할인율</small><b>${pct(r.r, 1)}</b></div>
      <div class="mini"><small>시총 ÷ 오너이익</small><b>${r.mult != null ? r.mult.toFixed(1) + '배' : '-'}</b></div>
      <div class="mini"><small>안전마진 · 자료 연수</small><b>${r.margin != null ? pct(r.margin) : '-'} · ${r.n}년</b></div></div>
    ${r.irr != null && r.irr < 0.09 ? '<p class="hint">내재 수익률이 최소 기대수익률 9%보다 낮아 현재가는 비싼 편입니다.</p>' : ''}</div>
  <div class="card"><h3>최근 5년 실적 <span class="tag">백만 USD</span></h3>${fiveTable(r.five)}${f}</div>
  ${notes ? `<div class="card"><h3>종목별 주의</h3>${notes}</div>` : ''}
  <div id="notes">${noteHtml(r.ticker)}</div>
  <p class="foot">참고용 계산이며 투자 권유가 아닙니다. 과거 이익 기준 모델이라 최근 이익이 정점이면 적정주가가 과대할 수 있습니다. 점수가 높은 종목이 더 올랐다는 백테스트 증거는 없었고, 낮은 종목을 걸러내는 용도로만 의미가 있었습니다. 기준일 ${esc(r.asof)}.</p>`;
}
function fiveTable(f) {
  const y = f.years, row = (nm, arr, fm) => `<tr><td>${nm}</td>${arr.map(v => `<td>${fm(v)}</td>`).join('')}</tr>`;
  return `<div class="tblw"><table><tr><th></th>${y.map(a => `<th>${a}</th>`).join('')}</tr>
  ${row('매출', f.rev, mm)}${row('영업이익', f.oi, mm)}${row('영업이익률', f.om, v => pct(v, 1))}${row('순이익', f.ni, mm)}${row('FCF', f.fcf, mm)}${row('주식수(백만)', f.sh, v => v == null ? '-' : v.toFixed(0))}</table></div>
  <div class="gr" style="margin-top:10px">
    <div class="mini"><small>매출 연평균</small><b>${pct(f.rev_cagr, 1)}</b></div><div class="mini"><small>영업이익률 (시작→최근)</small><b>${pct(f.om_first, 0)}→${pct(f.om_last, 0)}</b></div>
    <div class="mini"><small>FCF ÷ 순이익</small><b>${pct(f.fcf_ni)}</b></div><div class="mini"><small>(배당+자사주) ÷ FCF</small><b>${pct(f.payout)}</b></div></div>`;
}

// ----- 비교 -----
function compareHtml() {
  const last = LS.get('cmp', 'DPZ PZZA YUM');
  return `<div class="card"><h3>5년 실적·경쟁사 비교</h3>
  <p class="hint">한 줄에 한 묶음. 첫 티커가 기준 회사, 나머지가 경쟁사입니다. (최대 4줄)</p>
  <textarea id="grp" spellcheck="false" autocapitalize="characters">${esc(last)}</textarea>
  <button class="btn" id="go">비교하기</button></div><div id="out">${outHtml('compare')}</div>`;
}
function bindCompare() {
  $('#go').onclick = async () => {
    const txt = $('#grp').value; LS.set('cmp', txt);
    const groups = txt.split('\n').map(l => l.split(/[\s,]+/).filter(Boolean)).filter(g => g.length);
    if (!groups.length) return;
    state.busy = { kind: 'compare', msg: '요청 전송', sec: 0 }; state.compare = null; paintOut('compare');
    try {
      const r = await runJob('compare', { groups }, (m, s) => { state.busy = { kind: 'compare', msg: m, sec: s }; paintOut('compare'); });
      state.compare = { html: compareResult(r) };
    } catch (e) { state.compare = { error: e.message }; }
    state.busy = null; if (tab === 'compare') render();
  };
}
function cmpCard(x) {
  if (x.error) return `<div class="card"><div class="head"><div class="nm">${esc(x.ticker)}</div><span class="pill p-gray">${esc(x.role)}</span></div><div class="err">${esc(x.error)}</div></div>`;
  const m = x.m;
  return `<div class="card"><div class="head"><div><div class="nm">${esc(x.name)}</div><div class="tk">${esc(x.ticker)}</div></div><span class="pill ${x.role === '기준' ? 'p-good' : 'p-gray'}">${esc(x.role)}</span></div>
  <div class="gr" style="margin-top:10px">${cmpMinis(m)}</div>${(m.flags || []).map(f => `<div class="warn">${esc(f)}</div>`).join('')}
  <details><summary>연도별 표</summary>${fiveTable(m)}</details></div>`;
}
function cmpMinis(m, ref) {
  const it = (l, v, t) => `<div class="mini"><small>${l}</small><b>${t === 'p1' ? pct(v, 1) : t === 'p0' ? pct(v) : v}</b></div>`;
  return it('매출 연평균', m.rev_cagr, 'p1') + it('최근연도 매출', m.last_rev_g, 'p1') + it('영업이익률 최근', m.om_last, 'p1') + it('영업이익률 5년 최고', m.om_max, 'p1') +
    it('FCF ÷ 순이익', m.fcf_ni, 'p0') + it('환원 ÷ FCF', m.payout, 'p0') + it('순이익 연평균', m.ni_cagr, 'p1') + it('주식수 변화', m.sh_chg, 'p1');
}
function compareResult(r) {
  const groups = [...new Set(r.rows.map(x => x.group))];
  return groups.map(g => {
    const rows = r.rows.filter(x => x.group === g);
    const md = r.medians[g];
    const med = md ? `<div class="card" style="border-style:dashed"><div class="head"><div class="nm">경쟁사 중앙값</div><span class="pill p-gray">${rows.filter(x => x.role === '경쟁사' && x.m).length}개사</span></div><div class="gr" style="margin-top:10px">${cmpMinis(md)}</div>
      <p class="hint">기준 회사가 중앙값보다 이익률·FCF 전환이 꾸준히 좋은지 보세요. 같이 나빠졌다면 업종 문제, 혼자 나빠졌다면 회사 문제일 가능성이 큽니다.</p></div>` : '';
    return rows.map(cmpCard).join('') + med;
  }).join('') + `<p class="foot">금액은 백만 USD 환산. FCF = 영업현금흐름 − 설비투자. 경쟁사는 직접 고른 종목이라 사업이 완전히 같지 않을 수 있습니다.</p>`;
}

// ----- 점검 -----
function reviewHtml() {
  return `<div class="card"><h3>사업보고서 점검</h3>
  <p class="hint">미국은 10-K 최근 2건, 한국은 DART 사업보고서 최근 2건을 읽어 주의신호 원문·경쟁우위 근거·확인할 질문을 찍어 줍니다. 보조 도구이며 사업의 좋고 나쁨을 판정하지 않습니다.</p>
  <label>티커</label><input id="tk" placeholder="DPZ · 005930" autocapitalize="characters" autocorrect="off" spellcheck="false">
  <label style="display:flex;gap:8px;align-items:center;color:var(--ink)"><input id="tr" type="checkbox" style="width:auto" checked>미국 영어 원문을 한글로 번역 (더 오래 걸림)</label>
  <button class="btn" id="go">점검 시작</button></div><div id="out">${outHtml('review')}</div>`;
}
function bindReview() {
  const go = async () => {
    const tk = $('#tk').value.trim(); if (!tk) return;
    state.busy = { kind: 'review', msg: '요청 전송', sec: 0 }; state.review = null; paintOut('review');
    try {
      const r = await runJob('review', { ticker: tk, translate: $('#tr').checked }, (m, s) => { state.busy = { kind: 'review', msg: m, sec: s }; paintOut('review'); });
      state.review = { html: reviewResult(r), data: r, tk };
    } catch (e) { state.review = { error: e.message }; }
    state.busy = null; if (tab === 'review') render();
  };
  $('#go').onclick = go; $('#tk').onkeydown = e => { if (e.key === 'Enter') go(); };
  if (pend && pend.tab === 'review') { const t = pend.tk; pend = null; $('#tk').value = t; go(); }
  else if (state.review || state.busy) paintOut('review');
}
function quote(t, ko) { return `<div class="quote">${esc(t)}${ko ? `<div class="ko">${esc(ko)}</div>` : ''}</div>`; }
function reviewResult(r) {
  const v = r.rv;
  const real = v.flags.filter(f => f.kind.startsWith('사실형'));
  const hypo = v.flags.filter(f => !f.kind.startsWith('사실형'));
  const flags = real.length ? real.map(f => `<h4>${esc(f.name)} <span class="tag">${esc(f.kind)}</span></h4>${quote(f.text, f.ko)}`).join('') : '<p class="hint">사실형 주의신호가 잡히지 않았습니다.</p>';
  const hy = hypo.length ? `<details><summary>가정·일반 언급만 걸린 항목 (${hypo.length})</summary>${hypo.map(f => `<h4>${esc(f.name)}</h4>${quote(f.text, f.ko)}`).join('')}</details>` : '';
  const moat = v.moat.length ? v.moat.map(m => `<h4>${esc(m.cat)} <span class="tag">${m.n}문장</span></h4>${m.text.map((t, i) => quote(t, m.ko && m.ko[i])).join('')}`).join('') : '<p class="hint">사업 설명에서 경쟁우위 근거 문장이 거의 잡히지 않았습니다.</p>';
  const newr = (v.risk_new || []).length ? (v.risk_new.map((t, i) => quote(t, v.risk_new_ko && v.risk_new_ko[i])).join('')) : '<p class="hint">없음</p>';
  const cap = [['자사주', v.bb], ['배당', v.div], ['차입·유동성', v.liq]].filter(x => x[1] && x[1].length).map(x => `<h4>${x[0]}</h4>${x[1].map(t => quote(t)).join('')}`).join('');
  const a = r.analysis;
  const top = a ? `<div class="gr" style="margin-top:10px"><div class="mini"><small>점수 · 등급</small><b>${a.score.toFixed(2)} · ${esc(a.tier)}</b></div><div class="mini"><small>괴리율</small><b>${spct(a.gap)}</b></div></div>` : '';
  return `<div class="card"><div class="head"><div><div class="nm">${esc(r.name)}</div><div class="tk">${esc(r.report)} · 제출 ${esc(r.filed || '')}</div></div><span class="pill ${real.length ? 'p-warn' : 'p-good'}">신호 ${real.length}</span></div>${top}
  <p style="margin:10px 0 0"><a href="${esc(r.url)}" target="_blank" rel="noopener">원문 공시 열기 ↗</a></p></div>
  <div class="card"><h3>주의신호 원문</h3>${flags}${hy}</div>
  <div class="card"><h3>직접 확인할 질문 <span class="tag">체크하면 이 폰에 저장</span></h3><div id="rq">${r.questions.map((q, i) => `<label class="ck"><input type="checkbox" data-i="${i}"><span>${esc(q)}</span></label>`).join('')}</div></div>
  <div class="card"><h3>사업 속 경쟁우위 근거</h3><p class="hint">${esc(v.scope || '')} · 회사 스스로의 주장입니다. 점유율 수치·계약 기간 같은 구체성을 직접 확인하세요.</p>${moat}</div>
  <div class="card"><h3>${r.market === 'US' ? '전년에 없던 위험 문장' : '전년 보고서에 없던 문장'} <span class="tag">${v.risk_new_n}/${v.risk_n}</span></h3>${newr}</div>
  ${cap ? `<div class="card"><h3>자본배분·유동성</h3>${cap}</div>` : ''}
  <p class="foot">문장 검색 방식의 보조 도구입니다. ${r.market === 'KR' ? '한국 보고서에는 위험요인 항목이 없어 소송·제재·증자·감사의견 문구로만 찾습니다. 지배구조(최대주주·특수관계인 거래)는 [VII. 주주에 관한 사항]에서 직접 확인하세요.' : '번역은 기계번역이라 어색할 수 있으니 중요한 문장은 원문을 함께 보세요.'}</p>`;
}

// ----- 설정 -----
function settingsHtml() {
  return `<div class="card"><h3>서버 설정</h3><div id="sset" class="hint">불러오는 중…</div></div>
  ${backupHtml()}
  <div class="card"><h3>홈 화면에 앱으로 설치</h3>
  <p><b>아이폰(Safari)</b>: 하단 공유 버튼 → "홈 화면에 추가"<br><b>안드로이드(Chrome)</b>: 우측 위 ⋮ → "앱 설치" 또는 "홈 화면에 추가"<br><span class="hint">설치하면 주소창 없이 앱처럼 열립니다.</span></p></div>
  <div class="card"><h3>이 앱의 한계</h3><ul class="clean hint"><li>과거 이익 기준 모델입니다. 점수가 높은 종목이 더 올랐다는 백테스트 증거는 없었습니다.</li>
  <li>국가별 금리·세율은 반영하지 않습니다(미국·한국만 각자 가정, 그 밖은 미국 가정). 그 밖의 나라는 Yahoo 근사치라 신뢰도가 낮습니다.</li>
  <li>순위표(시총 상위 수백 종목)는 시간이 오래 걸려 PC판에서 만들고, [후보] 탭으로 불러와 씁니다. 이 앱은 종목 단위 조회·비교·점검·관리용입니다.</li>
  <li>무료 서버는 한동안 안 쓰면 잠들어 첫 요청이 1분쯤 걸립니다.</li></ul></div>
  <div class="card"><button class="btn ghost" id="lo" style="margin:0">이 폰에서 로그아웃</button></div>`;
}
async function bindSettings() {
  bindBackup();
  $('#lo').onclick = () => { KEY = ''; LS.set('key', ''); render(); };
  try {
    const s = await api('/api/settings');
    $('#sset').outerHTML = `<label>SEC 연락처 이메일 (미국 종목에 필요)</label><input id="ct" type="email" value="${esc(s.contact)}" placeholder="본인 이메일">
    <label>DART 인증키 40자리 (한국 종목에 필요) ${s.dart_key_set ? '<span class="tag">저장됨</span>' : ''}</label><input id="dk" placeholder="${s.dart_key_set ? '바꾸려면 새 키 입력' : '40자리 키'}" autocapitalize="off" autocorrect="off" spellcheck="false">
    <label>원·달러 환율 (시세를 못 받을 때 대체값)</label><input id="fx" inputmode="decimal" value="${esc(s.fx)}">
    <button class="btn" id="sv">저장</button><div id="smsg"></div>
    <p class="hint">SEC는 접속하는 쪽의 연락처를 요구합니다. 이 이메일은 SEC 요청 머리글에만 쓰이고 서버에 저장됩니다.</p>`;
    $('#sv').onclick = async () => {
      try {
        await api('/api/settings', { method: 'POST', body: JSON.stringify({ contact: $('#ct').value, dart_key: $('#dk').value, fx: $('#fx').value }) });
        $('#smsg').innerHTML = '<div class="warn" style="background:var(--goodbg);color:var(--good)">저장했습니다.</div>'; $('#dk').value = '';
      } catch (e) { $('#smsg').innerHTML = `<div class="err">${esc(e.message)}</div>`; }
    };
  } catch (e) { const el = $('#sset'); if (el) el.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}

if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
render();


// ---------- 공통: 스냅샷·노트·가정 바꿔 보기 ----------
function snapOf(r) { return { name: r.name, sym: r.ticker, cur: r.cur, price: r.price, fair: r.fair, buy: r.buy, gap: r.gap, score: r.score, tier: r.tier, verdict: r.verdict, irr: r.irr, gates: r.gates, trend_down: r.trend_down, asof: r.asof, ok: true }; }
const CK = ['이해할 수 있는 사업이다(내 능력 범위 안)', '경쟁사가 따라 하기 어려운 이유를 한 문장으로 설명할 수 있다', '최근 이익이 정점이 아님을 확인했다(5년 실적표·괴리율)',
  '사업보고서의 위험요인·소송·제재를 읽었다', '부채 만기와 이자 부담을 이익으로 감당할 수 있다', '경영진의 자본배분(자사주·배당·인수)이 합리적이다', '지금 가격이 아니라도 오래 들고 갈 수 있다'];
const DECS = ['', '관심', '매수 검토', '보유', '보류', '제외'];
function noteHtml(t) {
  const n = getN(t), ck = n.ck || {};
  return `<div class="card"><h3>내 판단 노트 <span class="tag">이 폰에만 저장</span></h3>
  <label>결정</label><select id="n_dec">${DECS.map(d => `<option value="${d}" ${n.dec === d ? 'selected' : ''}>${d || '미정'}</option>`).join('')}</select>
  <label>이익이 10년 뒤에도 나올 근거</label><textarea id="n_moat" placeholder="브랜드, 전환비용, 규모… 구체적으로">${esc(n.moat || '')}</textarea>
  <label>가장 큰 위험</label><textarea id="n_risk">${esc(n.risk || '')}</textarea>
  <label>메모</label><textarea id="n_memo">${esc(n.memo || '')}</textarea>
  <h4>멍거 체크리스트</h4>${CK.map((c, i) => `<label class="ck"><input type="checkbox" data-i="${i}" ${ck[i] ? 'checked' : ''}><span>${esc(c)}</span></label>`).join('')}
  <div class="hint" id="n_at">${n.at ? '마지막 저장 ' + esc(n.at) : ''}</div></div>`;
}
function bindNote(t) {
  const save = () => {
    const ck = {}; document.querySelectorAll('#notes input[type=checkbox]').forEach(c => { if (c.checked) ck[c.dataset.i] = true; });
    setN(t, { dec: $('#n_dec').value, moat: $('#n_moat').value, risk: $('#n_risk').value, memo: $('#n_memo').value, ck });
    $('#n_at').textContent = '저장됨 ' + today();
  };
  let tm; const deb = () => { clearTimeout(tm); tm = setTimeout(save, 500); };
  ['#n_moat', '#n_risk', '#n_memo'].forEach(s => $(s).oninput = deb);
  $('#n_dec').onchange = save; document.querySelectorAll('#notes input[type=checkbox]').forEach(c => c.onchange = save);
}
function bindAnalysisExtras(r) {
  const bw = $('#bw'), t = nkey(r.ticker);
  if (bw) { const upd = () => { bw.textContent = inW(t) ? '★ 관심종목' : '☆ 관심 추가'; }; upd(); bw.onclick = () => { toggleW(t, snapOf(r)); upd(); }; }
  const brv = $('#brv'); if (brv) brv.onclick = () => nav('review', t);
  if ($('#notes')) bindNote(t);
  // what-if
  const g1 = $('#g1'), sh = $('#sh'); if (!g1) return;
  const g0 = Math.round((r.g1 || 0) * 200) / 2;
  g1.value = g0; sh.value = 0;
  const show = (v) => { $('#wf1').textContent = money(v.fair, r.cur); $('#wf2').textContent = money(v.buy, r.cur); $('#wf3').textContent = spct(v.gap); $('#wf4').textContent = pct(v.irr, 1); };
  const lab = () => { $('#g1v').textContent = (+g1.value).toFixed(1) + '%'; $('#shv').textContent = (+sh.value >= 0 ? '+' : '') + (+sh.value).toFixed(1) + '%p'; };
  show({ fair: r.fair, buy: r.buy, gap: r.gap, irr: r.irr }); lab();
  let tm, seq = 0;
  const run = () => {
    lab(); clearTimeout(tm);
    tm = setTimeout(async () => {
      const my = ++seq;
      try {
        const v = await api('/api/whatif', { method: 'POST', body: JSON.stringify({ ticker: r.ticker, g1: g1.value / 100, shift: sh.value / 100 }) });
        if (my === seq) { show(v); $('#wmsg').innerHTML = ''; }
      } catch (e) { $('#wmsg').innerHTML = `<div class="err">${esc(e.message)}</div>`; }
    }, 250);
  };
  g1.oninput = run; sh.oninput = run;
  $('#wrs').onclick = () => { g1.value = g0; sh.value = 0; lab(); show({ fair: r.fair, buy: r.buy, gap: r.gap, irr: r.irr }); $('#wmsg').innerHTML = ''; };
}
function bindReviewExtras(r) {
  const t = nkey(state.review.tk || ''); const n = getN(t), rq = n.rq || {};
  document.querySelectorAll('#rq input').forEach(c => {
    const q = r.questions[+c.dataset.i]; c.checked = !!rq[q];
    c.onchange = () => { const cur = getN(t).rq || {}; if (c.checked) cur[q] = true; else delete cur[q]; setN(t, { rq: cur }); };
  });
}

// ---------- 홈(관심종목) ----------
function near(s) { return (s && s.ok !== false && s.price && s.buy) ? s.price / s.buy : null; }
function status(s) {
  if (!s || s.ok === false) return { t: s && s.error ? '조회 실패' : '계산 전', c: 'p-gray', n: 'gray' };
  if (s.verdict === '투자 제외' || s.verdict === '산정 불가') return { t: s.verdict, c: 'p-gray', n: 'gray' };
  const n = near(s); if (n == null) return { t: '계산 전', c: 'p-gray', n: 'gray' };
  if (n <= 1) return { t: '매수 구간', c: 'p-good', n: 'good' };
  if (n <= 1.25) return { t: '가까움', c: 'p-warn', n: 'warn' };
  return { t: '먼 편', c: 'p-gray', n: 'gray' };
}
function watchHtml() {
  const w = getW(), edit = state.edit;
  const items = w.slice().sort((a, b) => { const x = near(a.snap), y = near(b.snap); return (x == null) - (y == null) || (x - y); });
  const when = LS.get('watch_at', ''), at = LS.get('watch_ms', 0);
  const stale = at && (Date.now() - at > 20 * 3600 * 1000);
  const rows = items.map(it => {
    const s = it.snap, st = status(s), n = near(s), nm = (s && s.name) || it.t, n_ = getN(it.t);
    return `<div class="wrow" data-t="${esc(it.t)}"><div class="wl"><div class="wn">${esc(nm)}</div>
      <div class="ws"><span class="pill sm ${st.c}">${st.t}</span> ${esc(it.t)}${n_.dec ? ' · ' + esc(n_.dec) : ''}${s && s.trend_down ? ' · ⚠' : ''}</div></div>
      <div class="wr">${edit ? `<button class="x" data-x="1" aria-label="삭제">✕</button>` : `<div class="wnear c-${st.n}">${n == null ? '–' : Math.round(n * 100) + '%'}</div><div class="wp">${s && s.price ? money(s.price, s.cur) : ''}</div>`}</div></div>`;
  }).join('');
  return `<div class="card hero"><input id="q" placeholder="종목 검색 (AAPL · 005930 · 7203.T)" autocapitalize="characters" autocorrect="off" spellcheck="false" enterkeyhint="search"><button class="btn" id="qg">적정주가 보기</button></div>
  <div class="sec"><h2>관심종목${w.length ? ` <span>${w.length}</span>` : ''}</h2><div class="acts">${w.length ? `<button class="lnk" id="we">${edit ? '완료' : '편집'}</button><button class="lnk" id="wr">새로고침</button>` : ''}</div></div>
  <div id="wst" class="hint ${stale ? 'stale' : ''}">${state.busy && state.busy.kind === 'watch' ? esc(state.busy.msg) + ' …' : (state.watch && state.watch.error ? esc(state.watch.error) : (when ? (stale ? '가격이 오래됐어요 · ' : '') + '갱신 ' + esc(when) : (w.length ? '새로고침을 눌러 가격을 받으세요' : '')))}</div>
  ${w.length ? `<div class="card list">${rows}</div><p class="legend">큰 숫자 = 현재가 ÷ 매수 기준가 &nbsp;·&nbsp; <b class="c-good">100% 이하</b> 매수 구간 &nbsp;·&nbsp; <b class="c-warn">125% 이하</b> 가까움</p>` :
  `<div class="card empty"><b>아직 담은 종목이 없어요</b><ol><li>위에서 종목을 검색해 적정주가를 봅니다</li><li>마음에 들면 <b>☆ 관심 추가</b>를 누릅니다</li><li>여기서 매수 구간에 가까운 순서로 모아 봅니다</li></ol></div>`}`;
}
function bindWatch() {
  const q = $('#q'), go_ = () => { const t = nkey(q.value); if (!t) return; if (!/^[A-Z0-9.\-^=]{1,16}$/.test(t)) { alert('티커 형식이 올바르지 않습니다.'); return; } nav('analyze', t); };
  $('#qg').onclick = go_; q.onkeydown = e => { if (e.key === 'Enter') go_(); };
  document.querySelectorAll('.wrow').forEach(r => {
    const t = r.dataset.t;
    r.onclick = e => {
      if (e.target.closest('[data-x]')) { if (confirm(t + ' 을(를) 관심종목에서 지울까요? (노트는 남습니다)')) { toggleW(t); render(); } return; }
      if (!state.edit) nav('analyze', t);
    };
  });
  const we = $('#we'); if (we) we.onclick = () => { state.edit = !state.edit; render(); };
  const wr = $('#wr'); if (!wr) return;
  wr.onclick = async () => {
    const w = getW(); if (!w.length) return;
    state.busy = { kind: 'watch', msg: '요청 전송', sec: 0 }; state.watch = null; $('#wst').textContent = '요청 전송 …'; wr.disabled = true;
    try {
      const res = await runJob('watch', { tickers: w.map(x => x.t) }, (m) => { state.busy = { kind: 'watch', msg: m, sec: 0 }; const el = $('#wst'); if (el && tab === 'watch') el.textContent = m + ' …'; });
      const cur = getW();
      res.forEach(x => { const it = cur.find(y => y.t === nkey(x.t)); if (it) it.snap = x.ok ? x : { ok: false, error: x.error, name: it.snap && it.snap.name }; });
      LS.set('watch', cur); LS.set('watch_at', new Date().toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })); LS.set('watch_ms', Date.now());
    } catch (e) { state.watch = { error: e.message }; }
    state.busy = null; if (tab === 'watch') render();
  };
}

// ---------- 후보 (PC 결과 불러오기) ----------
const GRP = { A: ['A', 'p-good'], B: ['B', 'p-good'], C: ['C', 'p-warn'], D: ['D', 'p-gray'] };
function candsHtml() {
  const c = LS.get('cands', null);
  const imp = `<div class="card"><h3>PC판 후보 불러오기</h3>
    <p class="hint">PC판(v47)에서 메뉴 1·3·5를 실행하면 결과 엑셀 옆에 <b>…_핵심후보.json</b> 파일이 생깁니다. 이 파일을 폰으로 보내(나에게 메일·메신저 보내기·클라우드) 아래에서 고르거나, 내용을 붙여 넣으세요.</p>
    <input id="cf" type="file" accept=".json,application/json,text/plain"><label>또는 내용 붙여 넣기</label><textarea id="ct2" placeholder="[{&quot;grp&quot;:&quot;A 이중 통과&quot;, ...}]" style="min-height:60px"></textarea>
    <button class="btn" id="cl">불러오기</button><div id="cmsg"></div></div>`;
  if (!c) return imp;
  const flt = LS.get('cflt', 'AB'), sort = LS.get('csort', 'gap');
  let list = c.list.filter(x => flt === '전체' || flt.includes(String(x.grp || '?')[0]));
  list = list.slice().sort((a, b) => sort === 'score' ? (b.score || 0) - (a.score || 0) : (b.gap || -9) - (a.gap || -9));
  const cnt = k => c.list.filter(x => String(x.grp || '?')[0] === k).length;
  const chip = (v, l) => `<button class="chip ${flt === v ? 'on' : ''}" data-f="${v}">${l}</button>`;
  const rows = list.slice(0, 80).map(x => {
    const g = GRP[String(x.grp || '?')[0]] || ['?', 'p-gray'];
    return `<div class="card" data-t="${esc(x.ticker)}"><div class="head"><div><div class="nm">${esc(x.name)}</div><div class="tk">${esc(x.ticker)} · 점수 ${x.score != null ? (+x.score).toFixed(2) : '-'} ${esc(x.tier || '')}</div></div><span class="pill ${g[1]}">${esc(x.grp || '')}</span></div>
    <div class="gap">괴리율 <b>${spct(x.gap)}</b>${x.gap_avg != null ? ` · 전체평균 기준 ${spct(x.gap_avg)}` : ''}${x.mult != null ? ` · 시총÷오너이익 ${(+x.mult).toFixed(1)}배` : ''}${x.trend_down ? ' · ⚠이익 감소 추세' : ''}</div>
    <div class="row" style="margin-top:10px"><button class="btn ghost sm" data-a="an">분석</button><button class="btn ghost sm" data-a="rv">점검</button><button class="btn ghost sm" data-a="w">${inW(x.ticker) ? '★ 관심' : '☆ 관심'}</button></div></div>`;
  }).join('');
  return `<div class="card"><div class="head"><div><h3 style="margin:0">후보 ${c.list.length}개</h3><div class="hint">${esc(c.name)} · 불러온 날 ${esc(c.at)}</div></div><button class="chip" id="cx">다시 불러오기</button></div>
    <div class="chips">${chip('AB', 'A·B ' + (cnt('A') + cnt('B')))}${chip('A', 'A ' + cnt('A'))}${chip('B', 'B ' + cnt('B'))}${chip('C', 'C ' + cnt('C'))}${chip('D', 'D ' + cnt('D'))}${chip('전체', '전체')}</div>
    <div class="chips"><span class="hint">정렬</span>${`<button class="chip ${sort === 'gap' ? 'on' : ''}" data-s="gap">괴리율순</button><button class="chip ${sort === 'score' ? 'on' : ''}" data-s="score">점수순</button>`}</div>
    <p class="hint" style="margin:8px 0 0">A 이중 통과 · B 최근 이익 기준만 · C 정점·쇠퇴 의심 · D 시장 불신(8배 미만). C·D는 이유를 먼저 확인하세요. ${list.length > 80 ? '상위 80개만 표시합니다.' : ''}</p></div>${rows}`;
}
function bindCands() {
  const c = LS.get('cands', null);
  if (!c) {
    $('#cl').onclick = async () => {
      let txt = $('#ct2').value.trim(), name = '붙여 넣은 목록';
      const f = $('#cf').files && $('#cf').files[0];
      if (f) { txt = await f.text(); name = f.name; }
      try {
        txt = txt.replace(/^\uFEFF/, '');
        if (!txt) throw new Error('선택한 파일이 비어 있습니다. 파일을 다시 고르거나 내용을 붙여 넣으세요.');
        let j; try { j = JSON.parse(txt); } catch (e) { throw new Error('JSON 형식이 아닙니다. PC판이 만든 …_핵심후보.json 파일(엑셀 .xlsx 가 아님)이 맞는지 확인하세요.'); }
        if (!Array.isArray(j)) j = (j && (j.list || j.cands || j.items)) || null;
        if (!Array.isArray(j)) throw new Error('후보 목록 형태가 아닙니다. …_핵심후보.json 파일이 맞는지 확인하세요.');
        if (!j.length) throw new Error('이 파일은 후보가 0개입니다(앱 오류가 아닙니다). 핵심후보는 "자료 8년 이상·시총 $1B 이상·우수/양호·경고 없음·매수 기준 이하"만 담는데, 세계 기타 국가(Yahoo)는 자료가 4~5개년이라 항상 비어 있습니다. 미국·한국(메뉴 1·2·3·5) 결과의 파일을 쓰세요.');
        const list = j.filter(x => x && x.ticker);
        if (!list.length) throw new Error('목록에 ticker 항목이 없어 형식이 다릅니다.');
        LS.set('cands', { name, at: today(), list: list.map(x => ({ grp: x.grp, ticker: x.ticker, name: x.name, gap: x.gap, gap_avg: x.gap_avg, score: x.score, tier: x.tier, mult: x.mult, trend_down: x.trend_down, n: x.n })) });
        render();
      } catch (e) { $('#cmsg').innerHTML = `<div class="err">읽지 못했습니다: ${esc(e.message)}</div>`; }
    };
    return;
  }
  $('#cx').onclick = () => { if (confirm('불러온 목록을 지우고 다시 불러올까요?')) { LS.set('cands', null); render(); } };
  document.querySelectorAll('[data-f]').forEach(b => b.onclick = () => { LS.set('cflt', b.dataset.f); render(); });
  document.querySelectorAll('[data-s]').forEach(b => b.onclick = () => { LS.set('csort', b.dataset.s); render(); });
  document.querySelectorAll('#app .card[data-t]').forEach(card => {
    const t = card.dataset.t;
    card.querySelectorAll('button[data-a]').forEach(b => b.onclick = () => {
      if (b.dataset.a === 'an') nav('analyze', nkey(t)); else if (b.dataset.a === 'rv') nav('review', nkey(t));
      else { const on = toggleW(t); b.textContent = on ? '★ 관심' : '☆ 관심'; }
    });
  });
}

// ---------- 백업 ----------
const BKEYS = ['watch', 'notes', 'cands', 'recent', 'cmp', 'watch_at'];
const exportData = () => JSON.stringify({ app: 'jj', v: 1, at: new Date().toISOString(), data: Object.fromEntries(BKEYS.map(k => [k, LS.get(k, null)])) });
function backupHtml() {
  return `<div class="card"><h3>백업·복원 <span class="tag">관심종목·노트·후보</span></h3>
  <p class="hint">관심종목과 노트는 이 폰의 브라우저에만 저장됩니다. 폰을 바꾸거나 브라우저 데이터를 지우면 사라지니 가끔 백업해 두세요.</p>
  <div class="row"><button class="btn ghost sm" id="bx">파일로 저장</button><button class="btn ghost sm" id="bc">복사</button></div>
  <label>복원: 백업 파일 선택</label><input id="bf" type="file" accept=".json,application/json,text/plain">
  <label>또는 백업 내용 붙여 넣기</label><textarea id="bt" style="min-height:60px"></textarea>
  <button class="btn ghost" id="bi">복원하기</button><div id="bmsg"></div></div>`;
}
function bindBackup() {
  $('#bx').onclick = () => {
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([exportData()], { type: 'application/json' })); a.download = '적정주가_백업_' + today() + '.json';
    document.body.appendChild(a); a.click(); a.remove();
  };
  $('#bc').onclick = async () => { try { await navigator.clipboard.writeText(exportData()); $('#bmsg').innerHTML = '<div class="ok">복사했습니다. 메모 앱 등에 붙여 두세요.</div>'; } catch (e) { $('#bt').value = exportData(); $('#bmsg').innerHTML = '<div class="ok">아래 칸에 내용을 넣었습니다. 직접 복사하세요.</div>'; } };
  $('#bi').onclick = async () => {
    let txt = $('#bt').value.trim(); const f = $('#bf').files && $('#bf').files[0]; if (f) txt = await f.text();
    try {
      const j = JSON.parse(txt); if (j.app !== 'jj' || !j.data) throw new Error('이 앱의 백업 파일이 아닙니다.');
      if (!confirm('지금 저장된 관심종목·노트·후보를 백업 내용으로 바꿀까요?')) return;
      BKEYS.forEach(k => { if (j.data[k] !== undefined && j.data[k] !== null) LS.set(k, j.data[k]); });
      $('#bmsg').innerHTML = '<div class="ok">복원했습니다.</div>';
    } catch (e) { $('#bmsg').innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  };
}
