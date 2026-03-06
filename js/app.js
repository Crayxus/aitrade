// ── Beijing Time ──
function bjNow() {
  const now = new Date();
  return new Date(now.getTime() + now.getTimezoneOffset() * 60000 + 8 * 3600000);
}

function pad2(n) { return String(n).padStart(2, '0'); }

function updateClock() {
  const bj = bjNow();
  const h = bj.getHours(), m = bj.getMinutes(), s = bj.getSeconds();
  document.getElementById('clock').textContent = `${pad2(h)}:${pad2(m)}:${pad2(s)}`;

  const seattleStr = new Date().toLocaleTimeString('en-US', {
    timeZone: 'America/Los_Angeles', hour12: false,
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
  document.getElementById('clock-seattle').textContent = seattleStr;

  const min = h * 60 + m;
  const dot = document.getElementById('session-dot');
  const lbl = document.getElementById('session-label');
  const pill = dot.parentElement;

  // London entry: 15:00–15:45 BJ = min 900–945
  // NY entry:     21:00–22:00 BJ = min 1260–1320
  // Force-exit:   03:00–09:00 BJ = h in [3,9)
  // Otherwise: in position or pre-market
  if (min >= 900 && min < 945) {
    dot.className = 'dot dot-green';
    lbl.textContent = 'LONDON ENTRY';
    pill.style.borderColor = 'rgba(0,217,126,.3)';
  } else if (min >= 1260 && min < 1320) {
    dot.className = 'dot dot-green';
    lbl.textContent = 'NY ENTRY';
    pill.style.borderColor = 'rgba(0,217,126,.3)';
  } else if (h >= 3 && h < 9) {
    dot.className = 'dot dot-red';
    lbl.textContent = 'CLOSED · 15:00';
    pill.style.borderColor = '';
  } else if (min >= 945 && min < 1260) {
    dot.className = 'dot dot-yellow';
    lbl.textContent = 'NY OPENS 21:00';
    pill.style.borderColor = 'rgba(245,200,66,.3)';
  } else if (min >= 540 && min < 900) {
    dot.className = 'dot dot-yellow';
    lbl.textContent = 'LONDON 15:00';
    pill.style.borderColor = 'rgba(245,200,66,.3)';
  } else {
    dot.className = 'dot dot-yellow';
    lbl.textContent = 'EXIT 03:00';
    pill.style.borderColor = 'rgba(245,200,66,.3)';
  }
}

function updateDate() {
  const bj = bjNow();
  const d = bj.toLocaleDateString('en-US', { weekday:'short', month:'short', day:'numeric', year:'numeric' });
  document.getElementById('date-display').textContent = d.toUpperCase();
}

// ── Sparkline SVG ──
function buildSparkline(points, isUp) {
  if (!points || points.length < 2) return '';
  const W = 200, H = 40, pad = 2;
  const n = points.length;
  const xs = points.map((_, i) => pad + (i / (n - 1)) * (W - pad * 2));
  const ys = points.map(v => H - pad - (v / 100) * (H - pad * 2));
  const d = xs.map((x, i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(' ');
  const fillD = `${d} L${xs[n-1].toFixed(1)},${H} L${xs[0].toFixed(1)},${H} Z`;
  const color = isUp ? '#00d97e' : '#ff4d6a';
  const fillColor = isUp ? 'rgba(0,217,126,.08)' : 'rgba(255,77,106,.08)';
  return `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <path d="${fillD}" fill="${fillColor}" />
      <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" />
    </svg>`;
}

// ── Range Bar ──
function buildRangeBar(s, pnl) {
  const sl = s.stop_loss, tp = s.take_profit;
  const el = s.entry_low, eh = s.entry_high;
  const span = tp - sl;
  if (span === 0) return '';

  const pctOf = v => Math.min(100, Math.max(0, ((v - sl) / span) * 100));

  const entryLeft    = pctOf(el);
  const entryWidth   = pctOf(eh) - entryLeft;
  const entryMidPct  = pctOf(s.entry_mid != null ? s.entry_mid : (el + eh) / 2);

  let nowLeft = null, nowClass = 'neutral', fillHtml = '';
  if (pnl && pnl.status !== 'pending') {
    nowLeft  = pctOf(pnl.current_price);
    nowClass = pnl.status;
    if (pnl.status === 'winning' || pnl.status === 'losing') {
      const fl = Math.min(entryMidPct, nowLeft);
      const fw = Math.abs(nowLeft - entryMidPct);
      const fc = pnl.status === 'winning' ? 'rgba(0,217,126,.35)' : 'rgba(255,77,106,.35)';
      fillHtml = `<div class="range-fill" style="left:${fl.toFixed(1)}%;width:${Math.max(1,fw).toFixed(1)}%;background:${fc}"></div>`;
    }
  }

  const fmt = v => String(v);

  return `
<div class="range-bar-wrap">
  <div class="range-labels">
    <span class="lbl-sl">SL</span>
    <span class="lbl-mid">ENTRY ZONE</span>
    <span class="lbl-tp">TP</span>
  </div>
  <div class="range-track">
    ${fillHtml}
    <div class="range-entry" style="left:${entryLeft.toFixed(1)}%;width:${Math.max(2,entryWidth).toFixed(1)}%"></div>
    ${nowLeft !== null ? `<div class="range-now ${nowClass}" style="left:${nowLeft.toFixed(1)}%"></div>` : ''}
  </div>
  <div class="range-val-row">
    <span class="rv-sl">${fmt(sl)}</span>
    <span>${fmt(el)} – ${fmt(eh)}</span>
    <span class="rv-tp">${fmt(tp)}</span>
  </div>
</div>`;
}

// ── Confidence / Signal Row ──
function buildSignalRow(s) {
  if (!s.signals) return '';
  const sigNames = { daily_ema: 'EMA', weekly_mom: 'MOM', gap: 'GAP', rsi: 'RSI', hourly: 'HR', ema50: 'E50' };
  const sigs = Object.entries(s.signals).map(([k, icon]) => {
    const cls = icon === '▲' ? 'sig-up' : icon === '▼' ? 'sig-dn' : 'sig-flat';
    return `<span class="sig-item ${cls}">${icon} ${sigNames[k] || k}</span>`;
  }).join('');
  const barColor = s.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
  const pct = s.confidence_pct || '0%';
  return `
<div class="conf-row">
  <div class="conf-left">
    <span class="conf-label">CONF</span>
    <div class="conf-track"><div class="conf-fill" style="width:${pct};background:${barColor}"></div></div>
    <span class="conf-pct" style="color:${barColor}">${pct}</span>
  </div>
  <div class="sig-grid">${sigs}</div>
</div>`;
}

// ── Stars ──
function stars(n) {
  let s = '';
  for (let i = 0; i < 5; i++) s += i < n ? '★' : '<span class="e">★</span>';
  return `<span class="stars">${s}</span>`;
}

// ── P&L Chip ──
function pnlChip(pnl) {
  if (!pnl) return `<span class="pnl-chip neutral">–</span>`;
  if (pnl.status === 'pending') return `<span class="pnl-chip neutral">⏳ ${escHtml(pnl.entry_start)} BJ</span>`;
  const icons = { winning: '▲', losing: '▼', hit_tp: '✓', hit_sl: '✕', time_exit: '⏰' };
  return `<span class="pnl-chip ${pnl.status}">${icons[pnl.status] || ''} ${escHtml(pnl.pnl_pct)}</span>`;
}

// ── Card ──
function buildPnlBanner(pnl) {
  if (!pnl || pnl.status === 'pending') return '';
  if (pnl.status === 'winning' || pnl.status === 'hit_tp') {
    return `<div class="pnl-banner w">
      <div><div class="pnl-entry-label">ENTRY</div><div class="pnl-entry-val">${escHtml(String(pnl.entry_mid))}</div></div>
      <span class="pnl-big w">▲ ${escHtml(pnl.pnl_pct)}</span>
      <span class="pnl-usd-big w">${escHtml(pnl.pnl_usd)}</span>
    </div>`;
  }
  if (pnl.status === 'losing' || pnl.status === 'hit_sl') {
    return `<div class="pnl-banner l">
      <div><div class="pnl-entry-label">ENTRY</div><div class="pnl-entry-val">${escHtml(String(pnl.entry_mid))}</div></div>
      <span class="pnl-big l">▼ ${escHtml(pnl.pnl_pct)}</span>
      <span class="pnl-usd-big l">${escHtml(pnl.pnl_usd)}</span>
    </div>`;
  }
  return '';
}

function renderCard(s, idx, pnl) {
  const isLong = s.direction === 'LONG';
  const dirClass = isLong ? 'long' : 'short';
  const momPos = s.mom_pct && !s.mom_pct.startsWith('-');
  const selClass = idx === _selectedIdx ? ' selected' : '';
  const posClass = pnl && (pnl.status === 'winning' || pnl.status === 'hit_tp') ? ' open-win'
                 : pnl && (pnl.status === 'losing'  || pnl.status === 'hit_sl') ? ' open-loss'
                 : '';

  return `
<div class="card ${dirClass}${selClass}${posClass}" id="card-${idx}" onclick="selectCard(${idx})">
  <div class="card-top">
    <div class="sym-block">
      <div class="sym">${escHtml(s.symbol)}</div>
      <div class="sym-name">${escHtml(s.display_name)}</div>
    </div>
    <div class="dir-block">
      <div class="dir-badge ${dirClass}">${isLong ? '▲ LONG' : '▼ SHORT'}</div>
      <div class="strategy-tag" style="margin-top:2px;color:var(--blue)">⏰ ${escHtml(s.entry_start||'')} BJ</div>
    </div>
  </div>

  <div class="price-row">
    <div class="price-main">${escHtml(String(s.current))}</div>
    <div class="price-mom ${momPos ? 'pos' : 'neg'}">${escHtml(s.mom_pct)}</div>
  </div>

  ${buildRangeBar(s, pnl)}
  <div class="card-pnl-foot">${pnlChip(pnl)}</div>
</div>`;
}

function escHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function parsePnlUsd(str) {
  if (!str) return 0;
  return parseFloat(str.replace(/[$,+]/g, '')) || 0;
}

// ── Live Bar ──
let _pnlData = [];

function updateCountdown() {
  const el = document.getElementById('countdown');
  const lb = document.getElementById('countdown-label');
  if (!el) return;
  const bj  = bjNow();
  const h   = bj.getHours();
  const min = h * 60 + bj.getMinutes();

  // Determine what we're counting down TO
  let target = new Date(bj.getTime());
  let label  = 'CLOSE IN';

  if (h >= 3 && h < 9) {
    // Between sessions — count to London open 15:00
    target.setHours(15, 0, 0, 0);
    label = 'LONDON IN';
  } else if (min >= 540 && min < 900) {
    // Pre-London (09:00–15:00 BJ) — count to London open
    target.setHours(15, 0, 0, 0);
    label = 'LONDON IN';
  } else if (min >= 945 && min < 1260) {
    // Post-London, pre-NY — count to NY open 21:00
    target.setHours(21, 0, 0, 0);
    label = 'NY IN';
  } else {
    // In a session or approaching close — count to 03:00 next day
    if (h >= 9) target.setDate(target.getDate() + 1);
    target.setHours(3, 0, 0, 0);
    label = 'CLOSE IN';
  }

  if (lb) lb.textContent = label;

  const diff = target - bj;
  if (diff <= 0) { el.textContent = '00:00:00'; return; }

  const totalSecs = Math.floor(diff / 1000);
  const hrs  = Math.floor(totalSecs / 3600);
  const mins = Math.floor((totalSecs % 3600) / 60);
  const secs = totalSecs % 60;
  el.textContent = `${pad2(hrs)}:${pad2(mins)}:${pad2(secs)}`;
  el.className = `live-value ${hrs < 1 ? 'red' : hrs < 2 ? 'gold' : 'blue'}`;
}

function updatePortfolioPnl() {
  if (!_pnlData.length) return;

  const active = _pnlData.filter(p => p.status !== 'pending');
  const total = active.reduce((sum, p) => sum + parsePnlUsd(p.pnl_usd), 0);
  const sign  = total >= 0 ? '+' : '';
  const pnlEl = document.getElementById('portfolio-pnl');
  if (pnlEl) {
    pnlEl.textContent = active.length ? `${sign}$${Math.round(total)}` : '–';
    pnlEl.className   = `live-value ${total > 0 ? 'green' : total < 0 ? 'red' : 'muted'}`;
  }

  const cntEl = document.getElementById('position-count');
  if (cntEl) {
    const open    = _pnlData.filter(p => p.status === 'winning' || p.status === 'losing').length;
    const closed  = _pnlData.filter(p => ['hit_tp','hit_sl','time_exit'].includes(p.status)).length;
    const pending = _pnlData.filter(p => p.status === 'pending').length;
    let txt = `${open} OPEN · ${closed} CLOSED`;
    if (pending > 0) txt += ` · ${pending} PENDING`;
    cntEl.textContent = txt;
    cntEl.className   = 'live-value muted';
  }

  updatePerfBar();
}

// ── Today's Actual P&L Range (tracked intraday high/low) ──
function updateTodayRange(range) {
  const el = document.getElementById('today-range');
  if (!el) return;
  if (!range || range.high == null || range.low == null) { el.style.display = 'none'; return; }

  const high    = Math.round(range.high);
  const low     = Math.round(range.low);
  const current = Math.round(range.current);
  const span    = Math.max(high - low, 1);

  // Zero line position: where P&L == 0 on the bar
  const hasNeg  = low < 0;
  const zeroPct = hasNeg ? Math.max(0, Math.min(100, (-low / span * 100))) : 0;
  const nowPct  = Math.max(0, Math.min(100, ((current - low) / span * 100)));

  const fmt = (v) => { const s = v >= 0 ? '+' : ''; return `${s}$${Math.abs(v)}`; };
  const curCls  = current > 0 ? 'green' : current < 0 ? 'red' : 'muted2';
  const highCls = high    > 0 ? 'green' : high    < 0 ? 'red' : 'muted2';
  const lowCls  = low     < 0 ? 'red'   : 'green';

  el.style.display = 'block';
  el.innerHTML = `
    <div class="trange-wrap">
      <div class="trange-top">
        <span class="trange-title">TODAY'S RANGE · 今日实际盈亏区间</span>
        <span class="trange-vals">
          <span class="trange-item ${lowCls}">低点&nbsp;<b>${fmt(low)}</b></span>
          <span class="trange-sep">|</span>
          <span class="trange-item ${curCls}">当前&nbsp;<b>${fmt(current)}</b></span>
          <span class="trange-sep">|</span>
          <span class="trange-item ${highCls}">高点&nbsp;<b>${fmt(high)}</b></span>
        </span>
      </div>
      <div class="trange-bar">
        ${hasNeg ? `<div class="trange-neg" style="width:${zeroPct.toFixed(1)}%"></div>` : ''}
        <div class="trange-pos" style="width:${(100 - zeroPct).toFixed(1)}%"></div>
        ${hasNeg ? `<div class="trange-zero" style="left:${zeroPct.toFixed(1)}%"></div>` : ''}
        <div class="trange-marker" style="left:${nowPct.toFixed(1)}%" title="${fmt(current)} 当前">
          <div class="trange-dot"></div>
        </div>
      </div>
      <div class="trange-axis">
        <span class="${lowCls}">${fmt(low)}</span>
        ${hasNeg ? `<span class="trange-zero-lbl" style="left:${zeroPct.toFixed(1)}%">±0</span>` : ''}
        <span class="${highCls}">${fmt(high)}</span>
      </div>
    </div>`;
}

function updatePerfBar() {
  if (!_pnlData.length) return;
  const total   = _pnlData.length;
  const wins    = _pnlData.filter(p => p.status === 'winning'  || p.status === 'hit_tp').length;
  const losses  = _pnlData.filter(p => p.status === 'losing'   || p.status === 'hit_sl').length;
  const exits   = _pnlData.filter(p => p.status === 'time_exit').length;

  const segsEl  = document.getElementById('perf-segs');
  const statsEl = document.getElementById('perf-stats');

  if (segsEl) {
    segsEl.innerHTML = _pnlData.map(p => {
      const cls = p.status === 'pending'  ? 'neutral'
                : (p.status === 'winning' || p.status === 'hit_tp') ? 'win'
                : (p.status === 'losing'  || p.status === 'hit_sl') ? 'loss'
                : p.status === 'time_exit' ? 'exit'
                : 'neutral';
      const sym    = escHtml(p.symbol).replace('USD','').replace('=X','');
      const pctTxt = p.status === 'pending' ? `⏳${escHtml(p.entry_start)}` : escHtml(p.pnl_pct);
      return `<div class="perf-seg ${cls}" title="${escHtml(p.symbol)} ${escHtml(p.pnl_pct)} ${escHtml(p.pnl_usd)}">
        <span class="perf-sym">${sym}</span>
        <span class="perf-pct">${pctTxt}</span>
      </div>`;
    }).join('');
  }

  if (statsEl) {
    const rate = total > 0 ? Math.round(wins / total * 100) : 0;
    const cls  = wins > losses ? 'green' : wins < losses ? 'red' : 'muted2';
    statsEl.innerHTML =
      `<span class="${cls} fw">${wins}W ${losses}L</span>&nbsp;<span class="muted2">${rate}%</span>`;
  }
}

// ── Ticker Tape ──
function buildTicker(strategies) {
  if (!strategies || !strategies.length) return;
  const items = [...strategies, ...strategies].map(s => `
    <span class="tick-item">
      <span class="tick-sym">${s.symbol}</span>
      <span class="tick-val">${s.current}</span>
      <span class="tick-chg ${s.direction === 'LONG' ? 'pos' : 'neg'}">${s.direction === 'LONG' ? '▲' : '▼'} ${s.mom_pct}</span>
    </span>`).join('');
  document.getElementById('ticker-inner').innerHTML = items;
}

// ── Skeleton ──
function showSkeletons() {
  document.getElementById('grid').innerHTML = Array(6).fill(0).map(() => `
    <div class="skeleton-card">
      <div class="skel s"></div><div class="skel m"></div>
      <div class="skel xl" style="margin:8px 0"></div>
      <div class="skel l"></div><div class="skel l"></div>
      <div class="skel m"></div>
    </div>`).join('');
}

// ── State ──
let _strategies = [];
let _xauData    = null;
let _selectedIdx = 0;
let isLoading = false;

// ── Fetch Strategies ──
async function fetchStrategies(force = false) {
  if (isLoading) return;
  isLoading = true;

  const btn    = document.getElementById('refresh-btn');
  const icon   = document.getElementById('btn-icon');
  const overlay = document.getElementById('loading-overlay');
  const statusText = document.getElementById('status-text');
  const cachedLabel = document.getElementById('cached-label');

  btn.disabled = true;
  icon.className = 'spinning';
  icon.textContent = '↻';
  overlay.classList.add('visible');
  showSkeletons();

  if (force) { try { await fetch('/api/cache/clear', { method: 'POST' }); } catch {} }

  try {
    const res  = await fetch('/api/strategies', { method: 'POST' });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);

    _strategies = data.strategies;
    renderAll(_strategies, null);

    statusText.textContent = `${_strategies.length} SIGNALS  ·  ${data.date}`;
    cachedLabel.textContent = data.cached ? 'CACHED' : 'LIVE';
    cachedLabel.style.display = 'inline';

    // If hero still shows no data (xauusd fetch beat strategies), render now
    const selSym = _strategies[_selectedIdx] ? _strategies[_selectedIdx].symbol : null;
    if (selSym === 'XAUUSD' && (!_xauData || !(_xauData.signals || []).length)) {
      const s = _strategies[_selectedIdx];
      if (s) renderCardHero(s, null);
    }

    // Fetch P&L right after strategies load
    await fetchPnl();

  } catch (err) {
    document.getElementById('grid').innerHTML = `
      <div class="error-card">
        <h3>CONNECTION ERROR</h3>
        <p>${escHtml(err.message)}</p>
      </div>`;
    statusText.textContent = 'ERROR';
    cachedLabel.style.display = 'none';
  } finally {
    isLoading = false;
    btn.disabled = false;
    icon.className = '';
    icon.textContent = '↻';
    overlay.classList.remove('visible');
  }
}

// ── Render All ──
function renderAll(strategies, pnlMap) {
  document.getElementById('grid').innerHTML =
    strategies.map((s, i) => renderCard(s, i, pnlMap ? pnlMap[s.symbol] : null)).join('');
}

// ── Select Card (hero panel) ──
function selectCard(idx) {
  _selectedIdx = idx;
  document.querySelectorAll('#grid .card').forEach((el, i) => {
    el.classList.toggle('selected', i === idx);
  });
  const s = _strategies[idx];
  if (!s) return;

  // Show XAUUSD log only when XAUUSD is selected
  const logWrap = document.getElementById('xau-log-wrap');
  if (logWrap) logWrap.style.display = s.symbol === 'XAUUSD' ? 'block' : 'none';

  if (s.symbol === 'XAUUSD' && _xauData && (_xauData.signals || []).length > 0) {
    renderXauHero(_xauData, _pnlData.find(p => p.symbol === 'XAUUSD') || null);
  } else {
    const pnl = _pnlData.find(p => p.symbol === s.symbol) || null;
    renderCardHero(s, pnl);
  }

  // Scroll hero into view so user sees the update
  const sec = document.getElementById('xau-section');
  if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ── Generic Hero (non-XAUUSD) ──
function buildHeroPnlHtml(pnl) {
  if (!pnl || pnl.status === 'pending') return `<span class="xau-pnl neutral">⏳ ${escHtml(pnl ? pnl.entry_start || '–' : '–')} BJ</span>`;
  if (pnl.status === 'winning' || pnl.status === 'hit_tp') {
    return `<div class="pnl-hero-box w">
      <div class="pnl-hero-pct">▲ ${escHtml(pnl.pnl_pct)}</div>
      <div class="pnl-hero-usd">${escHtml(pnl.pnl_usd)}</div>
      <div class="pnl-hero-entry">入场 @ ${escHtml(String(pnl.entry_mid))}</div>
    </div>`;
  }
  if (pnl.status === 'losing' || pnl.status === 'hit_sl') {
    return `<div class="pnl-hero-box l">
      <div class="pnl-hero-pct">▼ ${escHtml(pnl.pnl_pct)}</div>
      <div class="pnl-hero-usd">${escHtml(pnl.pnl_usd)}</div>
      <div class="pnl-hero-entry">入场 @ ${escHtml(String(pnl.entry_mid))}</div>
    </div>`;
  }
  const cls = xauStatusClass(pnl.status);
  return `<span class="xau-pnl ${cls}">${xauStatusLabel(pnl.status)}&ensp;${escHtml(pnl.pnl_pct || '–')}</span>`;
}

function renderCardHero(s, pnl) {
  const hero = document.getElementById('xau-hero');
  if (!hero) return;

  const isLong   = s.direction === 'LONG';
  const dirClass = isLong ? 'long' : 'short';
  const dirLabel = isLong ? '▲ LONG' : '▼ SHORT';
  const sym  = s.symbol;
  const base = sym.endsWith('USD') ? sym.slice(0, -3) : sym;
  const quot = sym.endsWith('USD') ? 'USD' : '';

  hero.innerHTML = `
    <div class="xau-hero-inner ${dirClass}">
      <div class="xau-hero-left">
        <div class="xau-sym">${escHtml(base)}<span>${escHtml(quot)}</span></div>
        <div class="xau-hero-price">${escHtml(String(s.current))}</div>
        <div class="xau-hero-sess">${escHtml(s.session || '–')} · ${escHtml(s.entry_start||'–')} BJ</div>
      </div>
      <div class="xau-hero-mid">
        <div class="xau-dir-badge ${dirClass}">${dirLabel}</div>
      </div>
      <div class="xau-hero-right">
        <div class="xau-levels">
          <div class="xau-level-row">
            <span class="xau-lbl">TP</span>
            <span class="xau-val green">${escHtml(String(s.take_profit))}</span>
            <span class="xau-pct green">${escHtml(s.tp_pct||'')}</span>
          </div>
          <div class="xau-level-row xau-entry-row">
            <span class="xau-lbl">ENTRY</span>
            <span class="xau-val">${escHtml(String(s.entry_mid || s.entry_low || '–'))}</span>
            <span class="xau-pct muted2">ATR ${escHtml(String(s.atr||'–'))}</span>
          </div>
          <div class="xau-level-row">
            <span class="xau-lbl">SL</span>
            <span class="xau-val red">${escHtml(String(s.stop_loss))}</span>
            <span class="xau-pct red">${escHtml(s.sl_pct||'')}</span>
          </div>
        </div>
        ${buildHeroPnlHtml(pnl)}
      </div>
    </div>`;
}

// ── Fetch P&L ──
async function fetchPnl() {
  if (!_strategies.length) return;
  try {
    const res  = await fetch('/api/pnl', { method: 'POST' });
    if (!res.ok) return;
    const data = await res.json();
    if (!data.pnl) return;

    const pnlMap = {};
    data.pnl.forEach(p => { pnlMap[p.symbol] = p; });
    _pnlData = data.pnl;
    updatePortfolioPnl();
    if (data.pnl_range) updateTodayRange(data.pnl_range);

    renderAll(_strategies, pnlMap);
    buildTicker(_strategies);

    // Refresh hero panel with live P&L
    const sel = _strategies[_selectedIdx];
    if (sel) {
      if (sel.symbol === 'XAUUSD' && _xauData && (_xauData.signals || []).length) {
        renderXauHero(_xauData, pnlMap['XAUUSD'] || null);
      } else {
        renderCardHero(sel, pnlMap[sel.symbol] || null);
      }
    }
  } catch {}
}

// ── Day Summary ──
async function fetchSummary() {
  try {
    const res  = await fetch('/api/summary', { method: 'POST' });
    if (!res.ok) return;
    const d = await res.json();
    if (d.error) return;

    const panel = document.getElementById('summary-panel');
    const avgPos = !d.avg_pnl.startsWith('-');
    const winColor  = d.wins > d.losses ? 'green' : d.wins < d.losses ? 'red' : 'blue';
    const heading   = d.day_done ? '⏰ 今日交易结束' : '📊 实时汇总';

    // Per-trade pills
    const pills = (d.detail || []).map(p => {
      const cls  = p.status === 'hit_tp' ? 'win'
                 : p.status === 'hit_sl' ? 'loss'
                 : p.status === 'time_exit' ? 'exit'
                 : p.pnl_value > 0 ? 'win' : 'loss';
      const icon = cls === 'win' ? '▲' : cls === 'loss' ? '▼' : '⏰';
      return `<span class="trade-pill ${cls}">${icon} ${escHtml(p.symbol)} ${escHtml(p.pnl_pct)}</span>`;
    }).join('');

    panel.style.display = 'block';
    panel.innerHTML = `
      <div class="summary-header">
        <span class="summary-title">${heading}</span>
        <span class="summary-date">${escHtml(d.date)} &nbsp;·&nbsp; 更新 ${escHtml(d.updated_at)}</span>
      </div>
      <div class="summary-body">
        <div class="summary-cell">
          <span class="sc-label">胜率</span>
          <span class="sc-value ${winColor}">${d.win_rate}%</span>
          <span class="sc-sub">${d.wins}胜 / ${d.losses}负</span>
        </div>
        <div class="summary-cell">
          <span class="sc-label">平均盈亏</span>
          <span class="sc-value ${avgPos ? 'green' : 'red'}">${escHtml(d.avg_pnl)}</span>
          <span class="sc-sub">每笔策略</span>
        </div>
        <div class="summary-cell">
          <span class="sc-label">最佳</span>
          <span class="sc-value green">${d.best ? escHtml(d.best.pnl) : '–'}</span>
          <span class="sc-sub">${d.best ? escHtml(d.best.symbol) : ''}</span>
        </div>
        <div class="summary-cell">
          <span class="sc-label">最差</span>
          <span class="sc-value red">${d.worst ? escHtml(d.worst.pnl) : '–'}</span>
          <span class="sc-sub">${d.worst ? escHtml(d.worst.symbol) : ''}</span>
        </div>
        <div class="summary-cell">
          <span class="sc-label">平仓时间</span>
          <span class="sc-value gold">03:00</span>
          <span class="sc-sub">次日北京时间</span>
        </div>
      </div>
      <div class="summary-trades">${pills}</div>`;
  } catch {}
}

// ── Trading History ──
async function fetchHistory() {
  try {
    const res = await fetch('/api/history');
    if (!res.ok) return;
    const data = await res.json();
    const history = data.history || [];

    updateYesterdayBar(history);
    renderHistoryTable(history);
  } catch {}
}

function updateYesterdayBar(history) {
  const segsEl  = document.getElementById('yesterday-segs');
  const statsEl = document.getElementById('yesterday-stats');
  if (!segsEl || !statsEl) return;

  const yesterday = history[0]; // newest first
  if (!yesterday) {
    segsEl.innerHTML = `<div class="perf-seg neutral" style="flex:3">
      <span class="perf-sym" style="color:var(--muted)">N/A</span></div>`;
    statsEl.innerHTML = '<span class="muted2">N/A</span>';
    return;
  }

  const detail = yesterday.detail || [];
  if (detail.length) {
    segsEl.innerHTML = detail.map(p => {
      const cls = p.status === 'hit_tp'    ? 'win'
                : p.status === 'hit_sl'    ? 'loss'
                : p.status === 'time_exit' ? 'exit'
                : p.pnl_value > 0          ? 'win' : 'loss';
      const sym = escHtml(p.symbol).replace('USD','').replace('=X','');
      return `<div class="perf-seg ${cls}" title="${escHtml(p.symbol)} ${escHtml(p.pnl_pct)} ${escHtml(p.pnl_usd||'')}">
        <span class="perf-sym">${sym}</span>
        <span class="perf-pct">${escHtml(p.pnl_pct)}</span>
      </div>`;
    }).join('');
  } else {
    segsEl.innerHTML = `<div class="perf-seg neutral" style="flex:3">
      <span class="perf-sym" style="color:var(--muted)">${escHtml(yesterday.date)}</span></div>`;
  }

  const wc = yesterday.wins > yesterday.losses ? 'green'
           : yesterday.wins < yesterday.losses ? 'red' : 'muted2';
  const uc = yesterday.total_usd && !yesterday.total_usd.startsWith('-') ? 'green' : 'red';
  statsEl.innerHTML =
    `<span class="${wc} fw">${yesterday.wins}W ${yesterday.losses}L</span>` +
    `&nbsp;<span class="muted2">${yesterday.win_rate}%</span>` +
    (yesterday.total_usd ? `&nbsp;<span class="${uc}" style="font-size:11px">${escHtml(yesterday.total_usd)}</span>` : '');
}

function renderHistoryTable(history) {
  const scrollEl = document.querySelector('#history-panel .hist-scroll');
  const subEl    = document.getElementById('hist-sub');
  if (!scrollEl) return;

  if (!history.length) {
    scrollEl.innerHTML = `<div style="text-align:center;color:var(--muted);padding:32px;font-size:13px;letter-spacing:1px">暂无记录 — 今天交易完成后自动保存</div>`;
    if (subEl) subEl.textContent = '近30天记录';
    return;
  }

  if (subEl) subEl.textContent = `近30天记录 · 共 ${history.length} 天`;

  // Scale bars by max absolute P&L
  const maxAbs = Math.max(...history.map(d => Math.abs(parsePnlUsd(d.total_usd) || 0)), 1);

  const rows = history.map(d => {
    const usd     = parsePnlUsd(d.total_usd);
    const isPos   = usd >= 0;
    const barPct  = Math.min(100, Math.abs(usd) / maxAbs * 100);
    const usdStr  = d.total_usd || (isPos ? `+$${Math.round(usd)}` : `$${Math.round(usd)}`);
    const wc      = d.wins > d.losses ? 'green' : d.wins < d.losses ? 'red' : 'muted2';
    const dateShort = (d.date || '').slice(5); // "03-05"

    const detail = (d.detail || []).map(p => {
      const pv  = p.pnl_value != null ? p.pnl_value : parseFloat(p.pnl_pct);
      const cls = pv > 0 ? 'green' : 'red';
      const sym = p.symbol.replace(/USD$/, '').replace('=X', '');
      return `<span class="${cls}">${sym}&nbsp;${escHtml(p.pnl_pct)}</span>`;
    }).join('<span class="muted2"> · </span>');

    // Intraday P&L range: use actual tracked high/low if available
    let rangeHtml;
    if (d.pnl_range_high != null && d.pnl_range_low != null) {
      const hi   = Math.round(d.pnl_range_high);
      const lo   = Math.round(d.pnl_range_low);
      const fmtR = v => `${v >= 0 ? '+' : ''}$${Math.abs(v)}`;
      const hCls = hi >= 0 ? 'green' : 'red';
      const lCls = lo <  0 ? 'red'   : 'green';
      rangeHtml = `<span class="hist-day-range">` +
        `<span class="${lCls}">${fmtR(lo)}</span>` +
        `<span class="muted2">~</span>` +
        `<span class="${hCls}">${fmtR(hi)}</span>` +
        `</span>`;
    } else {
      // Fallback: gross win/loss from detail when no range data stored
      const grossWin  = (d.detail || []).reduce((s, p) => { const v = parsePnlUsd(p.pnl_usd); return s + (v > 0 ? v : 0); }, 0);
      const grossLoss = (d.detail || []).reduce((s, p) => { const v = parsePnlUsd(p.pnl_usd); return s + (v < 0 ? v : 0); }, 0);
      rangeHtml = (grossWin > 0 || grossLoss < 0)
        ? `<span class="hist-day-range">` +
          (grossLoss < 0 ? `<span class="red">-$${Math.round(Math.abs(grossLoss))}</span>` : '') +
          (grossLoss < 0 && grossWin > 0 ? `<span class="muted2">~</span>` : '') +
          (grossWin  > 0 ? `<span class="green">+$${Math.round(grossWin)}</span>` : '') +
          `</span>`
        : '<span class="hist-day-range muted2">–</span>';
    }

    return `<div class="hist-day-row">
      <span class="hist-day-date">${escHtml(dateShort)}</span>
      <div class="hist-day-bars">
        <div class="hist-bar-left">
          <div class="hist-neg-bar" style="width:${isPos ? 0 : barPct.toFixed(1)}%"></div>
        </div>
        <div class="hist-center-line"></div>
        <div class="hist-bar-right">
          <div class="hist-pos-bar" style="width:${isPos ? barPct.toFixed(1) : 0}%"></div>
        </div>
      </div>
      <span class="hist-day-usd ${isPos ? 'green' : 'red'}">${escHtml(usdStr)}</span>
      <span class="hist-day-wl ${wc}">${d.wins}W/${d.losses}L</span>
      ${rangeHtml}
      <span class="hist-day-detail">${detail || '<span class="muted2">–</span>'}</span>
    </div>`;
  }).join('');

  scrollEl.innerHTML = `<div class="hist-day-list">${rows}</div>`;
}

// ── XAUUSD Focus ──────────────────────────────────────────────────────────────

function xauStatusClass(status) {
  if (status === 'hit_tp')    return 'win';
  if (status === 'hit_sl')    return 'loss';
  if (status === 'time_exit') return 'exit';
  if (status === 'open')      return 'open';
  return 'neutral';
}

function xauStatusLabel(status) {
  const m = { hit_tp:'✓ TP HIT', hit_sl:'✕ SL HIT', time_exit:'⏰ 03:00', open:'● OPEN' };
  return m[status] || (status || '–');
}

function renderXauHero(data, livePnl) {
  const hero = document.getElementById('xau-hero');
  if (!hero) return;

  const sig  = (data.signals || []).find(s => s.session === 'London') ||
               (data.signals || [])[0];
  const log  = data.today;
  const sess = data.session_now || '–';
  const isLong = sig ? sig.direction === 'LONG' : null;
  const dirClass = isLong === null ? 'neutral' : isLong ? 'long' : 'short';
  const dirLabel = isLong === null ? '待计算' : isLong ? '▲ LONG' : '▼ SHORT';

  // Live P&L takes priority over log
  let pnlHtml;
  if (livePnl && livePnl.status !== 'pending') {
    pnlHtml = buildHeroPnlHtml(livePnl);
  } else if (log && log.status !== 'open') {
    const cls = xauStatusClass(log.status);
    pnlHtml = `<span class="xau-pnl ${cls}">${xauStatusLabel(log.status)}&ensp;${escHtml(log.pnl_pct || '–')}</span>`;
  } else {
    pnlHtml = `<span class="xau-pnl neutral">–</span>`;
  }

  hero.innerHTML = `
    <div class="xau-hero-inner ${dirClass}">
      <div class="xau-hero-left">
        <div class="xau-sym">XAU<span>USD</span></div>
        <div class="xau-hero-price">${sig ? escHtml(String(sig.current)) : '–'}</div>
        <div class="xau-hero-sess">${escHtml(sess)}</div>
      </div>
      <div class="xau-hero-mid">
        <div class="xau-dir-badge ${dirClass}">${dirLabel}</div>
      </div>
      <div class="xau-hero-right">
        <div class="xau-levels">
          <div class="xau-level-row">
            <span class="xau-lbl">TP</span>
            <span class="xau-val green">${sig ? escHtml(String(sig.take_profit)) : '–'}</span>
            <span class="xau-pct green">${sig ? escHtml(sig.tp_pct||'') : ''}</span>
          </div>
          <div class="xau-level-row xau-entry-row">
            <span class="xau-lbl">ENTRY</span>
            <span class="xau-val">${sig ? escHtml(String(sig.entry_mid)) : '–'}</span>
            <span class="xau-pct muted2">ATR ${sig ? escHtml(String(sig.atr)) : '–'}</span>
          </div>
          <div class="xau-level-row">
            <span class="xau-lbl">SL</span>
            <span class="xau-val red">${sig ? escHtml(String(sig.stop_loss)) : '–'}</span>
            <span class="xau-pct red">${sig ? escHtml(sig.sl_pct||'') : ''}</span>
          </div>
        </div>
        ${pnlHtml}
      </div>
    </div>`;
}

function renderXauLog(log) {
  const body = document.getElementById('xau-log-body');
  const wrap = document.getElementById('xau-log-wrap');
  const sub  = document.getElementById('xau-log-sub');
  if (!body || !wrap) return;
  if (!log || !log.length) { wrap.style.display = 'none'; return; }
  // Only show log when XAUUSD is the selected card
  const selSym2 = _strategies[_selectedIdx] ? _strategies[_selectedIdx].symbol : 'XAUUSD';
  wrap.style.display = selSym2 === 'XAUUSD' ? 'block' : 'none';

  const done   = log.filter(d => d.status && d.status !== 'open').length;
  const wins   = log.filter(d => d.status === 'hit_tp').length;
  const losses = log.filter(d => d.status === 'hit_sl').length;
  const exits  = log.filter(d => d.status === 'time_exit').length;
  const wr     = done > 0 ? Math.round(wins / done * 100) : 0;
  if (sub) sub.textContent =
    `${wins}W / ${losses}L / ${exits}⏰  ·  胜率 ${wr}%  ·  共 ${log.length} 天`;

  body.innerHTML = log.map(d => {
    const sc   = xauStatusClass(d.status);
    const sl   = xauStatusLabel(d.status);
    const dirC = d.direction === 'LONG' ? 'long' : 'short';
    const pnlC = d.pnl_pct && !d.pnl_pct.startsWith('-') ? 'green' : 'red';
    return `<tr>
      <td class="hist-date">${escHtml(d.date||'–')}</td>
      <td><span class="xau-sess-tag">${escHtml(d.session||'–')}</span></td>
      <td><span class="dir-mini ${dirC}">${d.direction==='LONG'?'▲ LONG':'▼ SHORT'}</span></td>
      <td class="muted2">${escHtml(d.confidence||'–')}</td>
      <td class="mono">${escHtml(String(d.entry||'–'))}</td>
      <td class="mono red">${escHtml(String(d.sl||'–'))}</td>
      <td class="mono green">${escHtml(String(d.tp||'–'))}</td>
      <td class="mono muted2">${escHtml(String(d.close_px||'–'))}</td>
      <td><span class="status-chip ${sc}">${sl}</span></td>
      <td class="fw ${pnlC}">${escHtml(d.pnl_pct||'–')}</td>
    </tr>`;
  }).join('');
}

async function fetchXauusd() {
  try {
    const res  = await fetch('/api/xauusd');
    if (!res.ok) return;
    const data = await res.json();
    _xauData = data;
    const selSym = _strategies[_selectedIdx] ? _strategies[_selectedIdx].symbol : 'XAUUSD';
    if (selSym === 'XAUUSD') renderXauHero(data, _pnlData.find(p => p.symbol === 'XAUUSD') || null);
    renderXauLog(data.log || []);
  } catch (e) {
    const hero = document.getElementById('xau-hero');
    if (hero) hero.innerHTML =
      `<div class="xau-hero-loading" style="color:var(--red)">Error: ${escHtml(e.message)}</div>`;
  }
}

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  updateClock();
  updateDate();
  updateCountdown();
  setInterval(updateClock, 1000);
  setInterval(updateCountdown, 1000);

  document.getElementById('refresh-btn').addEventListener('click', () => fetchStrategies(true));

  fetchXauusd();
  fetchStrategies(false);
  fetchHistory();
  setInterval(() => { fetchPnl(); fetchXauusd(); }, 5 * 60 * 1000);
  setInterval(fetchHistory, 15 * 60 * 1000);
});
