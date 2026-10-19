'use strict';
const $ = id => document.getElementById(id);
let activeRoute = 'binance', allState = null;
let state = null, candles = [], range = 120, hovered = null, chartGeometry = null, polling = false;
let receivedAt = performance.now();
const STEP = 300000;
const number = (n, digits = 2) => n == null || !Number.isFinite(Number(n)) ? '—' : Number(n).toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const fa = n => Number(n).toLocaleString('fa-IR');
const time = t => t == null ? '—' : new Date(t).toLocaleTimeString('en-GB', {timeZone: 'UTC', hour: '2-digit', minute: '2-digit'});
const date = t => t == null ? '—' : new Date(t).toLocaleDateString('en-GB', {timeZone: 'UTC', day: '2-digit', month: 'short', year: 'numeric'});
const phaseText = {paused: 'متوقف', importing: 'آماده‌سازی تاریخچه', loading: 'بارگذاری مدل', syncing: 'تکمیل تاریخچه', waiting: 'منتظر کندل بعدی', live: 'پایش فعال', predicting: 'در حال پیش‌بینی', error: 'نیازمند بررسی'};
const statusText = {pending: 'منتظر نتیجه', scored: 'ارزیابی‌شده', late: 'دیرهنگام', invalid: 'نامعتبر'};
const candleColors = {green: {emoji: '🟢', label: 'صعودی'}, red: {emoji: '🔴', label: 'نزولی'}, flat: {emoji: '⚪', label: 'بدون تغییر'}};
const colorText = color => candleColors[color] ? `${candleColors[color].emoji} ${candleColors[color].label}` : '—';
const colorMeaning = 'رنگ کندل بر مبنای مقایسهٔ بسته‌شدن با بازشدن همان کندل است.';
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('kronos-theme', theme); } catch (_) {}
  $('theme').textContent = theme === 'dark' ? '☀' : '☾';
  const next = theme === 'dark' ? 'روشن' : 'تیره';
  $('theme').setAttribute('aria-label', `تغییر به حالت ${next}`);
  $('theme').title = `تغییر به حالت ${next}`;
  drawChart();
}
let initialTheme;
try { initialTheme = localStorage.getItem('kronos-theme'); } catch (_) {}
setTheme(initialTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('theme').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));


const signalText = signal => signal === 'up' ? '🟢 صعودی' : signal === 'down' ? '🔴 نزولی' : '—';
const rateText = stats => stats?.accuracy == null ? '—' : fa(Number((100*stats.accuracy).toFixed(1)))+'٪';
function renderRoutes() {
  if (!allState?.routes) return;
  allState.routes.forEach(route => {
    const key=route.id, card=$(key === 'binance' ? 'routeBinance' : 'routeTwap');
    card.classList.toggle('selected',key===activeRoute);
    card.setAttribute('aria-pressed',key===activeRoute);
    $(key+'Phase').textContent=phaseText[route.phase] || route.phase;
    $(key+'MarketRate').textContent=rateText(route.market_success)+(route.market_success.evaluated?'\n'+fa(route.market_success.evaluated)+' دور':'');
    $(key+'Direction').textContent=signalText(route.latest_prediction?.market_predicted)+(route.latest_prediction?.market_predicted?' · '+time(route.latest_prediction.target)+' UTC':'');
  });
  const compare=allState.comparison;
  $('pairedSummary').textContent=compare?.paired ? fa(compare.paired)+' دور مشترکِ ارزیابی‌شده' : 'منتظر نتیجهٔ دورهای مشترک';
  $('pairedBinance').textContent=rateText(compare?.routes?.binance);
  $('pairedTwap').textContent=rateText(compare?.routes?.twap);
  $('officialStatus').textContent=allState.official?.error
      ? 'دریافت مرجع مشترک نیازمند بررسی است؛ نتایج تأییدنشده وارد درصد نمی‌شوند.'
      : 'مرجع مشترک: نتیجهٔ رسمی BTC پنج‌دقیقه‌ای Limitless؛ فقط دورهایی که هر دو مسیر پیش‌بینی معتبر دارند.';
}
document.querySelectorAll('[data-route]').forEach(button => button.addEventListener('click',()=>{
  activeRoute=button.dataset.route;
  candles=[];chartGeometry=null;hovered=null;
  $('tooltip').hidden=true;
  state=allState?.routes?.find(route=>route.id===activeRoute) || null;
  renderRoutes();renderState();renderHistory([]);drawChart();poll();
}));

function renderState() {
  if (!state) return;
  renderRoutes();
  document.querySelectorAll('.quote-unit').forEach(el=>el.textContent=state.unit || 'USDT');
  $('quoteName').textContent=activeRoute==='twap'?'دلار':'تتر';
  $('headingPair').textContent='BTC / '+(state.unit || 'USDT');
  $('currentLabel').textContent=state.current_label || 'قیمت جاری';
  $('chartSource').textContent='زمان: UTC · '+(state.source || 'Binance Spot')+' · '+(state.unit || 'USDT');
  $('routeDescription').textContent=(state.representation || '')+' '+(state.direction_note || '');
  $('metricLabel').textContent=state.metric_label || 'موفقیت رنگ کندل';
  $('directionLabel').textContent=activeRoute==='twap' ? 'جهت بازار رسمی' : 'رنگ کندل بعدی';
  $('volumeLegend').hidden=activeRoute==='twap';
  $('realLegend').textContent=activeRoute==='twap'?'نمونه واقعی مرزی':'کندل واقعی';
  document.querySelectorAll('.ohlc>div').forEach((el,index)=>el.hidden=activeRoute==='twap' && index<3);
  $('chart').setAttribute('aria-label',activeRoute==='twap'?'نقاط واقعی TWAP مرزی و مقدار پایانی پیش‌بینی‌شده':'کندل‌های واقعی بایننس و کندل پیش‌بینی‌شده');
  $('qualityNote').textContent=activeRoute==='twap'
    ? 'تطابق جهت پیش‌بینی با نتیجه رسمی TWAP؛ موارد منتظر یا نامعتبر وارد درصد نمی‌شوند. این مسیر OHLC کامل نیست.'
    : 'این درصد، رنگ کندل واقعی بایننس را می‌سنجد. مقایسه با نتیجه رسمی TWAP در کارت‌های بالای صفحه جدا نمایش داده می‌شود.';
  $('exportLink').href='/api/export.csv?route='+activeRoute;
  $('exportLink').download='kronos-'+activeRoute+'.csv';
  const p = state.latest_prediction;
  const valid = p && ['pending', 'scored'].includes(p.status);
  const badge = $('engineBadge');
  badge.textContent = phaseText[state.phase] || 'در حال آماده‌سازی';
  badge.className = 'badge' + (state.phase === 'error' ? ' error' : state.enabled && !state.stale ? ' live' : '');
  $('toggle').textContent = state.enabled ? 'توقف پایش  Ⅱ' : 'شروع پایش  ↗';
  $('toggle').classList.toggle('stopping', state.enabled);
  $('refresh').disabled = !state.enabled;
  $('noticeText').textContent = state.error || state.message;
  $('notice').classList.toggle('error', Boolean(state.error));
  $('syncTime').textContent = state.last_sync ? `آخرین دریافت ${time(state.last_sync)} UTC` : '';
  $('price').textContent = number(state.current_price);
  $('priceNote').textContent = state.stale ? 'دادهٔ جاری تازه نیست' : (state.price_note || 'آخرین قیمت دریافت‌شده از بایننس');
  $('predicted').textContent = valid ? number(p.close) : '—';
  const predictedColor = valid ? p.predicted_color : null;
  $('predictedColor').textContent = candleColors[predictedColor]?.emoji || '—';
  $('predictedColor').setAttribute('aria-label', `رنگ پیش‌بینی: ${colorText(predictedColor)}`);
  $('predictedColor').title = state.direction_note || `${colorText(predictedColor)}؛ ${colorMeaning}`;
  $('forecastColor').textContent = colorText(predictedColor);
  $('forecastColor').title = state.direction_note || colorMeaning;
  $('predictedNote').textContent = p ? `${time(p.target)} UTC · ${statusText[p.status]}` : 'از شروع کندل بعدی ثبت می‌شود';
  let change = valid ? 100 * (p.close / p.basis_close - 1) : null;
  $('change').textContent = change == null ? '—' : `${change >= 0 ? '+' : ''}${number(change, 3)}%`;
  $('change').className = change == null ? '' : change >= 0 ? 'positive' : 'negative';
  $('targetTime').textContent = p ? `${time(p.target)} → ${time(p.target + STEP)}` : '—';
  $('forecastStatus').textContent = p ? `${date(p.target)} · ${statusText[p.status]}` : 'پس از شروع پایش و بسته‌شدن کندل';
  for (const key of ['open', 'high', 'low', 'close']) $(key).textContent = p ? number(p[key]) : '—';
  $('forecastNote').textContent = p?.reason || (activeRoute==='twap' ? 'ورودی: ۴۰۰ نمونه واقعی مرزی TWAP؛ فقط پیش‌بینی پایان بازه سنجیده می‌شود.' : 'ورودی: ۴۰۰ کندل بسته‌شده · یک مسیر نمونه‌گیری. این خروجی درصد اطمینان ندارد.');
  $('device').textContent = state.device || (state.phase === 'loading' ? 'در حال بارگذاری…' : 'مدل بارگذاری نشده');
  const colorStats = state.statistics.color_success;
  $('colorSuccess').textContent = colorStats.accuracy == null ? '—' : fa(Number((100 * colorStats.accuracy).toFixed(1))) + '٪';
  $('colorSummary').textContent = colorStats.evaluated ? `${fa(colorStats.correct)} درست از ${fa(colorStats.evaluated)} کندل ارزیابی‌شده` : 'هنوز کندلی ارزیابی نشده';
  $('colorCorrect').textContent = fa(colorStats.correct);
  $('colorWrong').textContent = fa(colorStats.incorrect);
  $('colorPending').textContent = fa(colorStats.pending);
  $('excluded').textContent = fa(state.excluded_predictions);
  $('coverageCount').textContent = fa(state.coverage.count);
  $('coveragePeriod').textContent = state.coverage.first == null ? 'در حال آماده‌شدن…' : `${date(state.coverage.first)} → ${date(state.coverage.last)} · UTC`;
  $('lastLatency').textContent = state.last_prediction_seconds == null ? 'مدل base · افق ۱ کندل' : `زمان آخرین پیش‌بینی: ${number(state.last_prediction_seconds, 2)} ثانیه`;
  $('chartLast').textContent = candles.length ? `${time(candles.at(-1).t+(activeRoute==='twap'?STEP:0))} UTC` : '—';
  updateClock();
}

function updateClock() {
  if (!state) return;
  const now = state.server_time + performance.now() - receivedAt;
  const remain = Math.max(0, Math.ceil((STEP - now % STEP) / 1000));
  $('countdown').textContent = `${String(Math.floor(remain / 60)).padStart(2, '0')}:${String(remain % 60).padStart(2, '0')}`;
  $('progress').style.width = `${100 * (STEP - now % STEP) / STEP}%`;
  const stale = state.last_sync == null || now - state.last_sync > 30000;
  if (stale && state.enabled && !['loading', 'syncing', 'importing', 'error'].includes(state.phase)) {
    $('noticeText').textContent = 'دادهٔ تازه دریافت نشده است؛ زمان آخرین دریافت را بررسی کنید.';
    $('notice').classList.add('error');
    $('priceNote').textContent = 'دادهٔ جاری تازه نیست';
    $('engineBadge').classList.remove('live');
  }
}

function renderHistory(rows) {
  const body = $('history');
  body.replaceChildren();
  $('historyEmpty').hidden = rows.length > 0;
  rows.slice(0, 30).forEach(p => {
    const tr = document.createElement('tr');
    const values = [`${date(p.target)} · ${time(p.target)}`, number(p.close), number(p.actual_close), number(p.abs_error)];
    values.forEach((value, i) => {
      const td = document.createElement('td');
      td.textContent = value;
      td.className = 'number';
      if (i === 0) td.title = 'زمان شروع کندل هدف، UTC';
      if (i === 1 || i === 2) {
        const badge = document.createElement('span');
        badge.className = 'history-color';
        const color = i === 1 ? p.predicted_color : p.actual_color;
        badge.textContent = colorText(color);
        badge.title = colorMeaning;
        td.append(badge);
      }
      tr.append(td);
    });
    const match = document.createElement('td');
    match.className = 'color-match';
    match.textContent = p.color_match === true ? '✓ هم‌رنگ' : p.color_match === false ? '✕ متفاوت' : '—';
    if (p.color_match != null) match.classList.add(p.color_match ? 'positive' : 'negative');
    tr.append(match);
    const td = document.createElement('td'), tag = document.createElement('span');
    tag.className = `status-label ${['pending','scored','late','invalid'].includes(p.status) ? p.status : ''}`;
    tag.textContent = statusText[p.status] || p.status;
    if (p.reason) tag.title = p.reason;
    td.append(tag); tr.append(td);
    [signalText(p.market_predicted),signalText(p.market_actual),
     p.market_hit===true?'✓ درست':p.market_hit===false?'✕ نادرست':'—'].forEach(value=>{
      const cell=document.createElement('td');cell.textContent=value;tr.append(cell);
    });
    body.append(tr);
  });
}

async function get(path) {
  const response = await fetch(path, {cache: 'no-store'});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
async function poll() {
  if (polling) return;
  polling = true;
  const requestedRoute=activeRoute;
  try {
    const [next, bars, rows] = await Promise.all([get('/api/state'), get('/api/candles?limit='+range+'&route='+requestedRoute), get('/api/predictions?route='+requestedRoute)]);
    if (activeRoute!==requestedRoute) return;
    allState=next;
    state=next.routes?.find(route=>route.id===requestedRoute) || next;
    candles = bars; receivedAt = performance.now();
    renderState(); renderHistory(rows); drawChart();
  } catch (_) {
    $('noticeText').textContent = 'ارتباط با برنامهٔ محلی قطع است. پنجرهٔ اجرای داشبورد باید باز باشد.';
    $('notice').classList.add('error');
    $('engineBadge').textContent = 'برنامه در دسترس نیست';
    $('engineBadge').className = 'badge error';
  } finally { polling = false; if(activeRoute!==requestedRoute) poll(); }
}
async function control(action, button) {
  button.disabled = true;
  try {
    const response = await fetch('/api/control', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action})});
    if (!response.ok) throw new Error();
    await poll();
  } catch (_) { $('noticeText').textContent = 'ارسال فرمان ممکن نشد؛ اتصال به برنامه را بررسی کنید.'; }
  finally { button.disabled = false; }
}
$('toggle').addEventListener('click', () => control(allState?.enabled ? 'stop' : 'start', $('toggle')));
$('refresh').addEventListener('click', () => control('refresh', $('refresh')));
document.querySelectorAll('[data-range]').forEach(button => button.addEventListener('click', () => {
  range = Number(button.dataset.range);
  document.querySelectorAll('[data-range]').forEach(b => { const selected = b === button; b.classList.toggle('selected', selected); b.setAttribute('aria-pressed', selected); });
  hovered = null; poll();
}));

function drawChart() {
  const canvas = $('chart');
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr); canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  $('chartEmpty').hidden = candles.length > 0;
  if (!candles.length) return;
  const style = getComputedStyle(document.documentElement);
  const color = key => style.getPropertyValue(`--${key}`).trim();
  const width = rect.width, height = rect.height, right = width < 430 ? 67 : 76;
  const pad = {left: 12, right, top: 16, bottom: 33};
  const chartWidth = width - pad.left - pad.right, priceHeight = height - pad.top - pad.bottom - 55;
  const p = state?.latest_prediction;
  const ghost = p?.status === 'pending' && p.target === candles.at(-1).t + STEP ? p : null;
  const twap=activeRoute==='twap';
  const projected=ghost && twap ? {...ghost,open:candles.at(-1).close,high:Math.max(candles.at(-1).close,ghost.close),low:Math.min(candles.at(-1).close,ghost.close)} : ghost;
  const all = projected ? [...candles, {...projected, t: projected.target, ghost: true}] : candles;
  const highs = all.map(b => b.high), lows = all.map(b => b.low);
  const rawHigh = Math.max(...highs), rawLow = Math.min(...lows), spread = Math.max(rawHigh - rawLow, rawHigh * .0001);
  const max = rawHigh + spread * .12, min = rawLow - spread * .12;
  const step = chartWidth / (all.length + 2);
  const x = i => pad.left + (i + .65) * step;
  const y = price => pad.top + (max - price) / (max - min) * priceHeight;
  ctx.font = '10px Consolas, monospace';
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const price = max - (max - min) * i / 4, lineY = y(price);
    ctx.strokeStyle = color('grid'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(pad.left, lineY); ctx.lineTo(width - right, lineY); ctx.stroke();
    ctx.fillStyle = color('muted'); ctx.fillText(number(price, 0), width - right + 9, lineY);
  }
  const volumeMax = Math.max(...candles.map(b => b.volume), 1);
  if(twap) {
    ctx.strokeStyle=color('green');ctx.lineWidth=1.4;ctx.beginPath();
    candles.forEach((b,i)=>{if(i===0)ctx.moveTo(x(i),y(b.close));else ctx.lineTo(x(i),y(b.close));});ctx.stroke();
    if(projected){ctx.strokeStyle=color('accent');ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(x(candles.length-1),y(candles.at(-1).close));ctx.lineTo(x(candles.length),y(projected.close));ctx.stroke();ctx.setLineDash([]);}
  }
  all.forEach((b, i) => {
    const cx = x(i), barWidth = Math.max(1.5, Math.min(9, step * .62));
    const candleColor = b.ghost ? color('accent') : b.close >= b.open ? color('green') : color('red');
    ctx.strokeStyle = candleColor; ctx.fillStyle = candleColor; ctx.lineWidth = 1;
    if(twap){ctx.beginPath();ctx.arc(cx,y(b.close),b.ghost?3:1.5,0,Math.PI*2);ctx.fill();return;}
    ctx.setLineDash(b.ghost ? [3, 2] : []);
    ctx.beginPath(); ctx.moveTo(cx, y(b.high)); ctx.lineTo(cx, y(b.low)); ctx.stroke();
    const top = Math.min(y(b.open), y(b.close)), h = Math.max(1, Math.abs(y(b.open) - y(b.close)));
    if (b.ghost) ctx.strokeRect(cx - barWidth / 2, top, barWidth, h);
    else ctx.fillRect(cx - barWidth / 2, top, barWidth, h);
    ctx.setLineDash([]);
    if (!b.ghost) {
      ctx.globalAlpha = .23;
      const vh = b.volume / volumeMax * 37;
      ctx.fillRect(cx - barWidth / 2, height - pad.bottom - vh, barWidth, vh);
      ctx.globalAlpha = 1;
    }
  });
  const last = candles.at(-1);
  ctx.strokeStyle = color('muted'); ctx.setLineDash([3, 4]);
  ctx.beginPath(); ctx.moveTo(pad.left, y(last.close)); ctx.lineTo(width - right, y(last.close)); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = color('muted'); ctx.textAlign = 'center';
  const labels = width < 500 ? 3 : 5;
  for (let i = 0; i < labels; i++) {
    const index = Math.round(i * (candles.length - 1) / (labels - 1));
    ctx.fillText(time(candles[index].t+(twap?STEP:0)), x(index), height - 12);
  }
  if (ghost) {
    ctx.fillStyle = color('accent'); ctx.textAlign = 'right';
    ctx.fillText('پیش‌بینی', width - right - 7, 9);
  }
  if (hovered != null && all[hovered]) {
    ctx.strokeStyle = color('muted'); ctx.globalAlpha = .5; ctx.setLineDash([2, 4]);
    ctx.beginPath(); ctx.moveTo(x(hovered), pad.top); ctx.lineTo(x(hovered), height - pad.bottom); ctx.stroke();
    ctx.globalAlpha = 1; ctx.setLineDash([]);
  }
  chartGeometry = {all, step, left: pad.left};
}
$('chart').addEventListener('mousemove', event => {
  if (!chartGeometry) return;
  const box = $('chart').getBoundingClientRect();
  const px = event.clientX - box.left;
  const index = Math.round((px - chartGeometry.left) / chartGeometry.step - .65);
  if (index < 0 || index >= chartGeometry.all.length) { $('tooltip').hidden = true; return; }
  hovered = index;
  const b = chartGeometry.all[index];
  const tip = $('tooltip');
  tip.textContent = activeRoute==='twap' ? date(b.t+STEP)+' '+time(b.t+STEP)+' UTC · '+(b.ghost?'پیش‌بینی پایان: ':'TWAP مرزی: ')+number(b.close) : `${date(b.t)} ${time(b.t)} UTC${b.ghost ? ' · پیش‌بینی' : ''}\nO ${number(b.open)}   H ${number(b.high)}\nL ${number(b.low)}   C ${number(b.close)}`;
  tip.hidden = false;
  tip.style.left = Math.max(5, Math.min(px + 12, box.width - 280)) + 'px';
  tip.style.top = '18px'; drawChart();
});
$('chart').addEventListener('mouseleave', () => { hovered = null; $('tooltip').hidden = true; drawChart(); });
new ResizeObserver(drawChart).observe($('chart').parentElement);
document.fonts.ready.then(drawChart);
poll(); setInterval(poll, 5000); setInterval(updateClock, 1000);
