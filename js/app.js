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

  const min = h * 60 + m;
  const dot = document.getElementById('session-dot');
  const lbl = document.getElementById('session-label');
  const pill = dot.parentElement;

  if (min >= 570 && min < 630) {
    dot.className = 'dot dot-green';
    lbl.textContent = 'ENTRY WINDOW';
    pill.style.borderColor = 'rgba(0,217,126,.3)';
  } else if (min >= 630 && min < 1320) {
    dot.className = 'dot dot-yellow';
    lbl.textContent = 'IN POSITION';
    pill.style.borderColor = 'rgba(245,200,66,.3)';
  } else {
    dot.className = 'dot dot-red';
    lbl.textContent = 'MARKET CLOSED';
    pill.style.borderColor = '';
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

  const entryLeft  = pctOf(el);
  const entryWidth = pctOf(eh) - entryLeft;

  let nowLeft = null, nowClass = 'neutral';
  if (pnl) {
    nowLeft = pctOf(pnl.current_price);
    nowClass = pnl.status;
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
  const icons = { winning: '▲', losing: '▼', hit_tp: '✓', hit_sl: '✕', time_exit: '⏰' };
  return `<span class="pnl-chip ${pnl.status}">${icons[pnl.status] || ''} ${escHtml(pnl.pnl_pct)}</span>`;
}

// ── Card ──
function renderCard(s, idx, pnl) {
  const isLong = s.direction === 'LONG';
  const dirClass = isLong ? 'long' : 'short';
  const momPos = s.mom_pct && !s.mom_pct.startsWith('-');

  return `
<div class="card ${dirClass}" id="card-${idx}">
  <div class="card-top">
    <div class="sym-block">
      <div class="sym">${escHtml(s.symbol)}</div>
      <div class="sym-name">${escHtml(s.display_name)}</div>
    </div>
    <div class="dir-block">
      <div class="dir-badge ${dirClass}">${isLong ? '▲ LONG' : '▼ SHORT'}</div>
      <div class="strategy-tag">${escHtml(s.strategy)}</div>
    </div>
  </div>

  <div class="price-row">
    <div class="price-main">${escHtml(String(s.current))}</div>
    <div class="price-mom ${momPos ? 'pos' : 'neg'}">${escHtml(s.mom_pct)}</div>
  </div>

  <div class="spark-wrap">${buildSparkline(s.sparkline, isLong)}</div>

  ${buildRangeBar(s, pnl)}
  ${buildSignalRow(s)}

  <div class="card-meta">
    <div class="meta-cell">
      <span class="meta-label">推荐手数</span>
      <span class="meta-value" style="color:var(--gold)">${escHtml(String(s.recommended_lots))} <span style="font-size:10px;color:var(--muted)">lot</span></span>
      <span style="font-size:10px;color:var(--muted2);font-family:var(--mono)">${escHtml(s.rr_ratio)}</span>
    </div>
    <div class="meta-cell" style="border-left:1px solid var(--border)">
      <span class="meta-label">止盈 / 止损</span>
      <span style="font-family:var(--mono);font-size:12px">
        <span style="color:var(--green);font-weight:700">+$${escHtml(String(s.profit_usd))}</span>
        <span style="color:var(--muted);margin:0 3px">/</span>
        <span style="color:var(--red);font-weight:700">-$${escHtml(String(s.risk_usd))}</span>
      </span>
      <span style="font-size:10px;color:var(--muted)">预计盈亏 (USD)</span>
    </div>
    <div class="meta-cell" style="border-left:1px solid var(--border)">
      <span class="meta-label">P&L</span>
      ${pnlChip(pnl)}
      <span style="font-size:10px;color:var(--muted)">${stars(s.win_rate)}</span>
    </div>
  </div>
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
  if (!el) return;
  const bj = bjNow();
  const h = bj.getHours();

  let closeTime = new Date(bj.getTime());
  if (h >= 9) {
    closeTime.setDate(closeTime.getDate() + 1);
    closeTime.setHours(3, 0, 0, 0);
  } else if (h < 3) {
    closeTime.setHours(3, 0, 0, 0);
  } else {
    el.textContent = 'CLOSED';
    el.className = 'live-value red';
    return;
  }

  const diff = closeTime - bj;
  if (diff <= 0) {
    el.textContent = 'CLOSED';
    el.className = 'live-value red';
    return;
  }

  const totalSecs = Math.floor(diff / 1000);
  const hrs  = Math.floor(totalSecs / 3600);
  const mins = Math.floor((totalSecs % 3600) / 60);
  const secs = totalSecs % 60;
  el.textContent = `${pad2(hrs)}:${pad2(mins)}:${pad2(secs)}`;
  el.className = `live-value ${hrs < 1 ? 'red' : hrs < 3 ? 'gold' : 'blue'}`;
}

function updatePortfolioPnl() {
  if (!_pnlData.length) return;

  const total = _pnlData.reduce((sum, p) => sum + parsePnlUsd(p.pnl_usd), 0);
  const sign  = total >= 0 ? '+' : '';
  const pnlEl = document.getElementById('portfolio-pnl');
  if (pnlEl) {
    pnlEl.textContent = `${sign}$${Math.round(total)}`;
    pnlEl.className   = `live-value ${total > 0 ? 'green' : total < 0 ? 'red' : 'muted'}`;
  }

  const cntEl = document.getElementById('position-count');
  if (cntEl) {
    const open   = _pnlData.filter(p => p.status === 'winning' || p.status === 'losing').length;
    const closed = _pnlData.filter(p => ['hit_tp','hit_sl','time_exit'].includes(p.status)).length;
    cntEl.textContent = `${open} OPEN · ${closed} CLOSED`;
    cntEl.className   = 'live-value muted';
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
    buildTicker(_strategies);

    statusText.textContent = `${_strategies.length} SIGNALS  ·  ${data.date}`;
    cachedLabel.textContent = data.cached ? 'CACHED' : 'LIVE';
    cachedLabel.style.display = 'inline';

    // Fetch P&L + summary right after strategies load
    await fetchPnl();
    await fetchSummary();

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

    renderAll(_strategies, pnlMap);
    buildTicker(_strategies);

    const el = document.getElementById('pnl-updated');
    if (el) el.textContent = `UPDATED ${data.updated_at}`;
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
    if (!data.history || !data.history.length) return;
    const panel = document.getElementById('history-panel');
    if (!panel) return;

    const rows = data.history.map(d => {
      const wc = d.wins > d.losses ? 'green' : d.wins < d.losses ? 'red' : 'blue';
      const uc = (d.total_usd && !d.total_usd.startsWith('-')) ? 'green' : 'red';
      const ap = (d.avg_pnl && !d.avg_pnl.startsWith('-')) ? 'green' : 'red';
      return `<tr class="hist-row">
        <td class="hist-date">${escHtml(d.date)}</td>
        <td class="${wc}">${d.wins}W / ${d.losses}L</td>
        <td class="${wc}">${d.win_rate}%</td>
        <td class="${ap}">${escHtml(d.avg_pnl)}</td>
        <td class="${uc}">${escHtml(d.total_usd || '–')}</td>
        <td>${d.best ? `<span class="hist-green">${escHtml(d.best.symbol)} ${escHtml(d.best.pnl)}</span>` : '–'}</td>
        <td>${d.worst ? `<span class="hist-red">${escHtml(d.worst.symbol)} ${escHtml(d.worst.pnl)}</span>` : '–'}</td>
      </tr>`;
    }).join('');

    panel.style.display = 'block';
    panel.innerHTML = `
      <div class="hist-header">
        <span class="hist-title">TRADING HISTORY</span>
        <span class="hist-sub">近30天记录</span>
      </div>
      <div class="hist-scroll">
        <table class="hist-table">
          <thead><tr>
            <th>DATE</th><th>W/L</th><th>WIN RATE</th><th>AVG P&L</th><th>TOTAL USD</th><th>BEST</th><th>WORST</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  } catch {}
}

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  updateClock();
  updateDate();
  updateCountdown();
  setInterval(updateClock, 1000);
  setInterval(updateCountdown, 1000);

  document.getElementById('refresh-btn').addEventListener('click', () => fetchStrategies(true));

  fetchStrategies(false);
  fetchHistory();
  setInterval(() => { fetchPnl(); fetchSummary(); }, 5 * 60 * 1000);
  setInterval(fetchHistory, 15 * 60 * 1000);
});
