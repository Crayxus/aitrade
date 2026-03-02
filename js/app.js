// ── Beijing Time ──
function getBeijingTime() {
  const now = new Date();
  const utc = now.getTime() + now.getTimezoneOffset() * 60000;
  return new Date(utc + 8 * 3600000);
}

function pad2(n) { return String(n).padStart(2, '0'); }

function updateClock() {
  const bj = getBeijingTime();
  const h = bj.getHours(), m = bj.getMinutes(), s = bj.getSeconds();
  document.getElementById('clock').textContent =
    `${pad2(h)}:${pad2(m)}:${pad2(s)}`;

  const totalMin = h * 60 + m;
  const badge = document.getElementById('session-badge');
  const dot   = document.getElementById('session-dot');
  const label = document.getElementById('session-label');

  if (totalMin >= 9 * 60 + 30 && totalMin < 10 * 60 + 30) {
    dot.className = 'dot-green';
    label.textContent = '入场窗口 🟢';
    badge.style.borderColor = 'var(--green)';
  } else if (totalMin >= 10 * 60 + 30 && totalMin < 22 * 60) {
    dot.className = 'dot-yellow';
    label.textContent = '持仓期 🟡';
    badge.style.borderColor = 'var(--yellow)';
  } else {
    dot.className = 'dot-red';
    label.textContent = '已收盘 🔴';
    badge.style.borderColor = 'var(--red)';
  }
}

// ── Date Display ──
function updateDateDisplay() {
  const bj = getBeijingTime();
  const opts = { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' };
  document.getElementById('date-display').textContent =
    bj.toLocaleDateString('zh-CN', opts);
}

// ── Stars ──
function renderStars(n) {
  const filled = Math.min(5, Math.max(0, n));
  let html = '';
  for (let i = 0; i < 5; i++) {
    html += i < filled ? '★' : '<span class="empty">★</span>';
  }
  return html;
}

// ── Render Cards ──
function renderCard(s, idx) {
  const isLong = s.direction === 'LONG';
  const dirClass = isLong ? 'long' : 'short';
  const dirArrow = isLong ? '▲' : '▼';
  const dirLabel = isLong ? 'LONG' : 'SHORT';

  return `
<div class="card" id="card-${idx}" onclick="toggleDetail(${idx})">
  <div class="card-header">
    <div class="symbol-group">
      <span class="symbol">${escHtml(s.symbol)}</span>
      <span class="display-name">${escHtml(s.display_name)}</span>
    </div>
    <div class="direction-badge ${dirClass}">
      <span class="dir-arrow">${dirArrow}</span>
      ${dirLabel}
    </div>
  </div>

  <div class="card-body">
    <div class="price-row">
      <div class="price-cell">
        <div class="price-label">入场区间</div>
        <div class="price-value entry-value">${fmtNum(s.entry_low)} - ${fmtNum(s.entry_high)}</div>
      </div>
      <div class="price-cell">
        <div class="price-label">止盈目标</div>
        <div class="price-value tp-value">${fmtNum(s.take_profit)}</div>
        <div class="price-pct pct-green">${escHtml(s.tp_pct)}</div>
      </div>
      <div class="price-cell">
        <div class="price-label">止损</div>
        <div class="price-value sl-value">${fmtNum(s.stop_loss)}</div>
        <div class="price-pct pct-red">${escHtml(s.sl_pct)}</div>
      </div>
    </div>

    <div class="time-row">
      <div class="time-cell">
        <span class="time-icon">⏰</span>
        <div class="time-info">
          <span class="time-label">入场时机</span>
          <span class="time-val">${escHtml(s.entry_time)}</span>
        </div>
      </div>
      <div class="time-cell">
        <span class="time-icon">🏁</span>
        <div class="time-info">
          <span class="time-label">平仓时机</span>
          <span class="time-val">${escHtml(s.exit_time)}</span>
        </div>
      </div>
    </div>

    <div class="meta-row">
      <div class="stars">${renderStars(s.win_rate)}</div>
      <div class="rr-badge">风险回报 ${escHtml(s.rr_ratio)}</div>
    </div>

    <div class="logic-text">${escHtml(s.logic)}</div>
  </div>

  <div class="card-detail" id="detail-${idx}">
    <div class="detail-row">
      <span class="detail-label">品种代码</span>
      <span class="detail-value">${escHtml(s.symbol)}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">交易方向</span>
      <span class="detail-value" style="color:var(--${isLong ? 'green' : 'red'})">${dirArrow} ${dirLabel}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">入场区间</span>
      <span class="detail-value">${fmtNum(s.entry_low)} — ${fmtNum(s.entry_high)}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">止盈目标</span>
      <span class="detail-value" style="color:var(--green)">${fmtNum(s.take_profit)} (${escHtml(s.tp_pct)})</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">止损位</span>
      <span class="detail-value" style="color:var(--red)">${fmtNum(s.stop_loss)} (${escHtml(s.sl_pct)})</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">胜率参考</span>
      <span class="detail-value">${renderStars(s.win_rate)} (${s.win_rate}/5)</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">风险回报比</span>
      <span class="detail-value">${escHtml(s.rr_ratio)}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">入场时机</span>
      <span class="detail-value">${escHtml(s.entry_time)}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">平仓时机</span>
      <span class="detail-value">${escHtml(s.exit_time)}</span>
    </div>
  </div>
</div>`;
}

function toggleDetail(idx) {
  const el = document.getElementById(`detail-${idx}`);
  el.classList.toggle('open');
}

function escHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtNum(n) {
  if (n == null) return '—';
  const num = Number(n);
  if (isNaN(num)) return String(n);
  // Format with appropriate decimals
  if (num >= 10000) return num.toLocaleString('en-US', { maximumFractionDigits: 0 });
  if (num >= 100) return num.toLocaleString('en-US', { maximumFractionDigits: 2 });
  return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 5 });
}

// ── Skeleton Loader ──
function showSkeletons() {
  const grid = document.getElementById('grid');
  grid.innerHTML = Array(9).fill(0).map(() => `
    <div class="skeleton-card">
      <div class="skel-line skel-short"></div>
      <div class="skel-line skel-medium"></div>
      <div class="skel-line skel-full"></div>
      <div class="skel-line skel-full"></div>
      <div class="skel-line skel-medium"></div>
    </div>
  `).join('');
}

// ── Fetch Strategies ──
let isLoading = false;

async function fetchStrategies(force = false) {
  if (isLoading) return;
  isLoading = true;

  const btn = document.getElementById('refresh-btn');
  const btnIcon = document.getElementById('btn-icon');
  const overlay = document.getElementById('loading-overlay');
  const grid = document.getElementById('grid');
  const statusText = document.getElementById('status-text');
  const cachedLabel = document.getElementById('cached-label');

  btn.disabled = true;
  btnIcon.className = 'btn-icon spinning';
  overlay.classList.add('visible');
  showSkeletons();

  if (force) {
    // Clear server cache before fetching
    try { await fetch('/api/cache/clear', { method: 'POST' }); } catch {}
  }

  try {
    const res = await fetch('/api/strategies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const data = await res.json();

    if (!res.ok || data.error) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }

    const strategies = data.strategies;
    if (!Array.isArray(strategies) || strategies.length === 0) {
      throw new Error('未获取到策略数据');
    }

    grid.innerHTML = strategies.map((s, i) => renderCard(s, i)).join('');
    statusText.textContent = `已加载 ${strategies.length} 个策略`;
    cachedLabel.textContent = data.cached ? '📦 今日缓存' : '🔄 实时获取';
    cachedLabel.style.display = 'inline';

  } catch (err) {
    grid.innerHTML = `
      <div class="error-card">
        <h3>⚠️ 获取策略失败</h3>
        <p>${escHtml(err.message)}</p>
        <p style="margin-top:8px">请检查 KIMI_API_KEY 环境变量是否正确设置，然后刷新重试。</p>
      </div>`;
    statusText.textContent = '加载失败';
    cachedLabel.style.display = 'none';
  } finally {
    isLoading = false;
    btn.disabled = false;
    btnIcon.className = 'btn-icon';
    overlay.classList.remove('visible');
  }
}

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  updateClock();
  updateDateDisplay();
  setInterval(updateClock, 1000);

  document.getElementById('refresh-btn').addEventListener('click', () => {
    fetchStrategies(true);  // force re-fetch (clear cache)
  });

  // Auto-load on page open
  fetchStrategies(false);
});
