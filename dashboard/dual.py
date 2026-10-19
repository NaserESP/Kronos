"""Independent Binance and sparsely sampled TWAP research routes; never trades."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from app import Engine, HERE, STEP, LOOKBACK, COLS, candle_color, valid_prediction, score_values

FEED = '0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f'
BASE = 'https://api.limitless.exchange'
VALID = ('pending', 'scored')

def milliseconds(value):
    from datetime import datetime
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()*1000)

def positive(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('قیمت مرجع TWAP معتبر نیست.')
    return str(result)

def decode_market(data, target):
    meta = data.get('metadata', {})
    feed = meta.get('chainlinkDataStream', {})
    if (feed.get('feedId') != FEED or feed.get('streamType') != 'twap'
            or feed.get('twapWindowSeconds') != 60 or feed.get('priceDecimals') != 18):
        raise ValueError('هویت یا نوع خوراک TWAP مطابقت ندارد.')
    if milliseconds(data['startAt']) != target or int(data['expirationTimestamp']) != target+STEP:
        raise ValueError('مرز دور بازار با بازهٔ پنج‌دقیقه‌ای مطابقت ندارد.')
    if meta.get('openPrice') is None or not meta.get('openPriceCapturedAt') or not meta.get('openPriceSelectedAt'):
        return None
    if milliseconds(meta['openPriceCapturedAt']) != target:
        raise ValueError('زمان قیمت آغاز بازار معتبر نیست.')
    selected = milliseconds(meta['openPriceSelectedAt'])
    if not target <= selected <= target+5000:
        raise ValueError('قیمت آغاز خارج از پنجرهٔ معتبر است.')
    opened = positive(meta['openPrice'])
    closed = outcome = None
    resolution = meta.get('chainlinkTwapResolution')
    if data.get('status') == 'RESOLVED' and resolution:
        if resolution.get('feedId') != FEED:
            raise ValueError('خوراک نتیجه با خوراک ورودی یکسان نیست.')
        for name, boundary in (('open', target), ('close', target+STEP)):
            part = resolution[name]
            if milliseconds(part['targetAt']) != boundary or not boundary <= milliseconds(part['selectedAt']) <= boundary+5000:
                raise ValueError('زمان نتیجهٔ اوراکل معتبر نیست.')
        if Decimal(resolution['open']['raw'])/10**18 != Decimal(opened):
            raise ValueError('قیمت آغاز با رسید تسویه اختلاف دارد.')
        closed = positive(Decimal(resolution['close']['raw'])/10**18)
        winner = 0 if Decimal(closed) >= Decimal(opened) else 1
        if data.get('winningOutcomeIndex') != winner or resolution.get('winningIndex') != winner:
            raise ValueError('نتیجهٔ رسمی با قیمت‌های مرجع ناسازگار است.')
        outcome = 'up' if winner == 0 else 'down'
    return dict(target=target, slug=data['slug'], opened=opened, closed=closed,
                outcome=outcome, selected=selected, feed=FEED)

class MarketStore:
    def __init__(self, path, enabled, shutdown, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enabled, self.shutdown = enabled, shutdown
        self.clock = clock or (lambda: int(time.time()*1000))
        self.lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.last_request = 0
        self.meta = dict(last_sync=None, error=None, backfill_done=False, backfill_count=0,
                         message='در انتظار دریافت مرجع رسمی TWAP')
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS markets (
              target INTEGER PRIMARY KEY, slug TEXT NOT NULL, opened TEXT NOT NULL,
              closed TEXT, outcome TEXT, selected INTEGER NOT NULL, feed TEXT NOT NULL,
              observed INTEGER NOT NULL, open_observed INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS signals (
              route TEXT NOT NULL, target INTEGER NOT NULL, signal TEXT,
              predicted_close REAL, source_open TEXT, reference_open TEXT NOT NULL,
              created INTEGER NOT NULL, completed INTEGER NOT NULL, status TEXT NOT NULL,
              PRIMARY KEY(route,target));
            """)
    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()
    def update(self, **values):
        with self.lock:
            self.meta.update(values)
    def fetch(self, path, **params):
        if not self.enabled.is_set() or self.shutdown.is_set():
            raise RuntimeError('دریافت مرجع متوقف است.')
        # Bound the shared foreground/backfill request rate; no unbounded retries.
        with self.request_lock:
            delay = max(0, .25-(time.monotonic()-self.last_request))
            if delay:
                self.shutdown.wait(delay)
            self.last_request = time.monotonic()
        url = BASE+path+('?' + urlencode(params) if params else '')
        try:
            with urlopen(Request(url, headers={'User-Agent':'Kronos-Local/2.0',
                                               'Accept':'application/json'}), timeout=15) as response:
                raw = response.read(5_000_001)
                if len(raw)>5_000_000:
                    raise ValueError()
                return json.loads(raw, parse_float=Decimal)
        except Exception:
            raise RuntimeError('دریافت دادهٔ عمومی Limitless موفق نبود؛ دادهٔ جایگزین ساخته نمی‌شود.') from None
    def put(self, item, observed=None):
        if item is None:
            return
        observed = observed or self.clock()
        if item['selected'] > observed:
            raise ValueError('قیمت مرجع متعلق به آینده است.')
        with self.db() as db:
            old = db.execute('SELECT * FROM markets WHERE target=?',(item['target'],)).fetchone()
            if old and (old['opened'] != item['opened'] or
                        old['closed'] is not None and item['closed'] is not None and old['closed'] != item['closed']):
                raise ValueError('مرجع رسمی نسبت به رسید ذخیره‌شده تغییر کرده است.')
            db.execute("""INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?)
              ON CONFLICT(target) DO UPDATE SET
              closed=COALESCE(excluded.closed,markets.closed),
              outcome=COALESCE(excluded.outcome,markets.outcome), observed=excluded.observed""",
              (item['target'], item['slug'], item['opened'], item['closed'], item['outcome'],
               item['selected'], item['feed'], observed, observed))
    def timeline(self, before):
        data = self.fetch('/markets/timeline', symbol='BTC', frequency='minutely',
                          subFrequency='minutes_5', before=before, after=1)
        slots = {x['slug']: x for x in data.get('batch', [])}
        for key in ('current','next'):
            if data.get(key):
                slots[data[key]['slug']] = data[key]
        result = []
        now = self.clock()
        for slot in slots.values():
            target = milliseconds(slot['startAt'])
            if target <= now and milliseconds(slot['endAt']) == target+STEP:
                result.append((target, slot['slug']))
        return sorted(result, reverse=True)
    def capture(self, slot):
        target, slug = slot
        self.put(decode_market(self.fetch('/markets/'+slug), target))
        return target
    def context(self, target):
        with self.db() as db:
            row = db.execute('SELECT * FROM markets WHERE target=?',(target,)).fetchone()
        return dict(row) if row else None
    def live_worker(self):
        while not self.shutdown.is_set():
            if self.enabled.is_set():
                try:
                    slots = self.timeline(4)
                    with self.db() as db:
                        pending = db.execute('''SELECT DISTINCT m.target,m.slug FROM markets m
                          JOIN signals s USING(target) WHERE m.outcome IS NULL AND m.target+?<?
                          ORDER BY m.target LIMIT 5''', (STEP,self.clock())).fetchall()
                    known = {slot[0] for slot in slots}
                    slots += [(row['target'],row['slug']) for row in pending if row['target'] not in known]
                    for slot in slots:
                        if self.shutdown.is_set() or not self.enabled.is_set():
                            break
                        old = self.context(slot[0])
                        if old and old['outcome']:
                            continue
                        self.capture(slot)
                    self.update(last_sync=self.clock(), error=None,
                                message='قیمت مرجع و نتایج رسمی دریافت می‌شوند.')
                except Exception as exc:
                    self.update(error=str(exc))
            self.shutdown.wait(8)
    def backfill_worker(self):
        while not self.shutdown.is_set():
            if not self.enabled.is_set():
                self.shutdown.wait(1)
                continue
            try:
                slots = self.timeline(LOOKBACK+15)
                missing = [slot for slot in slots if self.context(slot[0]) is None]
                failures = []
                def bounded(slot):
                    if not self.enabled.is_set() or self.shutdown.is_set():
                        return
                    try:
                        self.capture(slot)
                    except Exception as exc:
                        failures.append(str(exc))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    for _ in pool.map(bounded, missing):
                        with self.db() as db:
                            count = db.execute('SELECT COUNT(*) FROM markets').fetchone()[0]
                        self.update(backfill_count=count, message=f'آماده‌سازی نمونه‌های واقعی TWAP: {count}')
                if failures:
                    raise RuntimeError(failures[0])
                self.update(backfill_done=True)
                return
            except Exception as exc:
                self.update(error=str(exc), message='آماده‌سازی TWAP هنوز کامل نشده است.')
            self.shutdown.wait(30)
    def record(self, route, prediction, context, source_open):
        if not context or source_open is None or prediction['target'] != context['target']:
            return
        if (context['open_observed'] > prediction['created'] or
                context['selected'] > prediction['created'] or
                prediction['completed'] >= prediction['target']+STEP):
            return
        signal = None
        if prediction['status'] in VALID:
            signal = 'up' if Decimal(str(prediction['close'])) >= Decimal(str(source_open)) else 'down'
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?)',
                       (route, prediction['target'], signal, prediction['close'], str(source_open),
                        context['opened'], prediction['created'], prediction['completed'], prediction['status']))
    def decorate(self, route, rows):
        with self.db() as db:
            results = {r['target']: dict(r) for r in db.execute("""
              SELECT s.*, m.outcome, m.closed AS official_close FROM signals s
              JOIN markets m USING(target) WHERE route=?""", (route,))}
        for row in rows:
            evidence = results.get(row['target'])
            row.update(market_predicted=None, market_actual=None, market_hit=None, market_reference=None)
            if evidence:
                valid = evidence['status'] in VALID and row['status'] in VALID
                row.update(market_predicted=evidence['signal'] if valid else None,
                           market_actual=evidence['outcome'], market_reference=evidence['reference_open'],
                           market_hit=(evidence['signal'] == evidence['outcome'])
                           if valid and evidence['outcome'] else None)
                if route == 'twap':
                    row['predicted_color'] = {'up':'green','down':'red'}.get(row['market_predicted'])
                    row['actual_color'] = {'up':'green','down':'red'}.get(row['market_actual'])
                    row['color_match'] = row['market_hit']
                    row['actual_open'] = float(evidence['reference_open'])
                    row['actual_close'] = float(evidence['official_close']) if evidence['official_close'] else None
                    row['abs_error'] = abs(row['close']-row['actual_close']) if row['actual_close'] else None
                    if row['status'] == 'scored' and not evidence['outcome']:
                        row['status'] = 'pending'
        return rows
    def summary(self, route):
        with self.db() as db:
            row = db.execute("""SELECT
              SUM(CASE WHEN m.outcome IS NOT NULL THEN 1 ELSE 0 END) AS evaluated,
              SUM(CASE WHEN m.outcome=s.signal THEN 1 ELSE 0 END) AS correct,
              SUM(CASE WHEN m.outcome IS NULL THEN 1 ELSE 0 END) AS pending
              FROM signals s JOIN markets m USING(target)
              WHERE s.route=? AND s.status IN ('pending','scored') AND s.signal IS NOT NULL""",(route,)).fetchone()
        n, correct, pending = row['evaluated'] or 0, row['correct'] or 0, row['pending'] or 0
        return dict(evaluated=n, correct=correct, incorrect=n-correct,
                    accuracy=correct/n if n else None, pending=pending)
    def comparison(self):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("""SELECT b.target, b.signal AS binance,
              t.signal AS twap, m.outcome AS actual
              FROM signals b JOIN signals t ON b.target=t.target
              JOIN markets m ON m.target=b.target
              WHERE b.route='binance' AND t.route='twap'
              AND b.status IN ('pending','scored') AND t.status IN ('pending','scored')
              AND b.signal IS NOT NULL AND t.signal IS NOT NULL AND m.outcome IS NOT NULL
              ORDER BY b.target DESC""")]
        n = len(rows)
        rates = {}
        for route in ('binance','twap'):
            correct = sum(r[route] == r['actual'] for r in rows)
            rates[route] = dict(evaluated=n, correct=correct, incorrect=n-correct,
                                accuracy=correct/n if n else None)
        return dict(paired=n, routes=rates, rows=rows[:30])

class SharedModel:
    def __init__(self):
        self.lock = threading.Lock()
        self.inference_lock = threading.Lock()
        self.owner = None
    def load(self, engine):
        with self.lock:
            if self.owner is None:
                Engine.load_model(engine)
                self.owner = engine
            else:
                for name in ('predictor','device','model_revision','model_hash'):
                    setattr(engine, name, getattr(self.owner, name))
                engine.update(model_ready=True, device=self.owner.meta['device'])

class ResearchRoute(Engine):
    def __init__(self, route_id, store, runtime, **kwargs):
        super().__init__(**kwargs)
        self.route_id, self.store, self.runtime = route_id, store, runtime
        self.inference_lock = runtime.inference_lock
    def load_model(self):
        self.runtime.load(self)
    def forecast(self):
        target = self.now()//STEP*STEP
        context = self.store.context(target)
        source_open = (context['opened'] if context else None) if self.route_id == 'twap' else (
            self.meta.get('current_open') if self.meta.get('current_candle_time') == target else None)
        if not context and self.now()-target<30_000:
            self.update(phase='waiting', message='منتظر ثبت مرجع آغاز دور برای مقایسهٔ مشترک…')
            return
        super().forecast()
        rows = Engine.prediction_records(self, limit=1)
        if rows and rows[0]['target'] == target:
            self.store.record(self.route_id, rows[0], context, source_open)
    def prediction_records(self, limit=1000):
        return self.store.decorate(self.route_id, super().prediction_records(limit))
    def state(self):
        result = super().state()
        result['id'] = self.route_id
        result['market_success'] = self.store.summary(self.route_id)
        if self.route_id == 'twap':
            result.update(name='TWAP مرز دورها', symbol='BTC/USD', unit='USD',
                          source='Chainlink TWAP 60s · مرجع آغاز دورهای Limitless',
                          representation='دادهٔ این مسیر فقط نمونه‌های واقعی TWAP در مرز دورهاست. مدل فقط برای مقدار پایانی ارزیابی می‌شود؛ OHLC داخل بازه نداریم.',
                          metric_label='موفقیت جهت بازار رسمی',
                          direction_note='بسته‌شدن پیش‌بینی‌شده در برابر TWAP واقعی آغاز دور؛ برابری: Up',
                          current_label='آخرین TWAP مرزی', volume_label='حجم ندارد',
                          price_note='نمونهٔ مرزی پنج‌دقیقه‌ای؛ قیمت لحظه‌ای نیست')
            result['statistics']['color_success'] = result['market_success']
        else:
            result.update(name='بایننس', unit='USDT', source='Binance Spot · BTCUSDT',
                          representation='کندل کامل معاملات و حجم واقعی؛ سوابق قدیمی حفظ شده‌اند.',
                          metric_label='موفقیت رنگ کندل بایننس',
                          direction_note='رنگ کندل از بازشدن و بسته‌شدن همان کندل؛ مقایسه رسمی از تغییر نسبت به بازشدن واقعی.',
                          current_label='قیمت جاری بایننس', volume_label='حجم BTC',
                          price_note='آخرین قیمت دریافت‌شده از بایننس')
        return result

class TwapRoute(ResearchRoute):
    def valid_output(self, values):
        # No traded volume exists for this sampled oracle series. Preserve raw
        # model outputs, but do not score a volume forecast as price validity.
        return len(values)==6 and math.isfinite(float(values[3])) and float(values[3])>0
    def score(self):
        # Adjacent round opens are input samples, not a substitute for the
        # official closing report (the tolerance window may select another tick).
        with self.db() as db:
            pending=db.execute('SELECT * FROM predictions WHERE status="pending"').fetchall()
            for row in pending:
                reference=self.store.context(row['target'])
                if not reference or reference['outcome'] is None:
                    continue
                metrics=score_values(row,{'close':reference['closed']})
                db.execute('UPDATE predictions SET status="scored",actual_close=?,abs_error=?,baseline_error=?,direction_hit=? WHERE target=?',
                           (*metrics.values(),row['target']))

    def import_seed(self):
        # Deliberately never imports Binance data into the TWAP route.
        pass
    def sync(self):
        now = self.now()
        target = now//STEP*STEP
        with self.store.db() as source:
            points = source.execute('SELECT target,opened,selected FROM markets WHERE target<=? ORDER BY target',
                                    (target,)).fetchall()
        with self.db() as db:
            db.executemany('INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?)',
                           [(p['target']-STEP, p['opened'], p['opened'], p['opened'], p['opened'],'0','0')
                            for p in points if p['selected']<=now])
            history = db.execute('SELECT t FROM candles ORDER BY t DESC LIMIT ?', (LOOKBACK,)).fetchall()
        context = self.store.context(target)
        with self.store.lock:
            source_meta = dict(self.store.meta)
        self.update(sync_pages=source_meta['backfill_count'])
        if len(history)<LOOKBACK:
            self.update(phase='syncing', network='warming', error=None,
                        message=f'در حال آماده‌سازی TWAP واقعی: {len(history)} از {LOOKBACK} نمونه')
            return
        if not context or source_meta['last_sync'] is None or now-source_meta['last_sync']>30000:
            self.update(phase='waiting', network='stale', error=source_meta['error'],
                        message='مرجع TWAP تازه نیست؛ پیش‌بینی تا دریافت معتبر متوقف است.')
            return
        self.update(current_price=float(context['opened']), current_price_time=target,
                    last_sync=source_meta['last_sync'], network='connected', error=None)
        self.score()
        self.forecast()

class MultiEngine:
    def __init__(self, state_dir=None):
        directory = Path(state_dir or HERE/'state')
        self.enabled, self.wake, self.shutdown = threading.Event(), threading.Event(), threading.Event()
        self.runtime = SharedModel()
        self.store = MarketStore(directory/'official-twap.sqlite3', self.enabled, self.shutdown)
        self.routes = {
            'binance': ResearchRoute('binance', self.store, self.runtime, db_path=directory/'dashboard.sqlite3'),
            'twap': TwapRoute('twap', self.store, self.runtime, db_path=directory/'twap.sqlite3')}
        for route in self.routes.values():
            route.enabled, route.shutdown = self.enabled, self.shutdown
        self.store.clock = self.routes['binance'].now
    def route(self, route_id):
        return self.routes[route_id]
    def state(self):
        routes = [r.state() for r in self.routes.values()]
        result = dict(routes[0])
        with self.store.lock:
            official = dict(self.store.meta)
        result.update(version=2, routes=routes, official=official,
                      comparison=self.store.comparison(), enabled=self.enabled.is_set())
        return result
    def comparison(self):
        return self.store.comparison()
    def control(self, action):
        if action=='start':
            self.enabled.set()
        elif action=='stop':
            self.enabled.clear()
        elif action!='refresh':
            raise ValueError('فرمان نامعتبر است.')
        for route in self.routes.values():
            route.wake.set()
        self.wake.set()
    def worker(self):
        jobs = [r.worker for r in self.routes.values()]+[self.store.live_worker,self.store.backfill_worker]
        self.threads = [threading.Thread(target=job,daemon=True) for job in jobs]
        for thread in self.threads:
            thread.start()
        self.shutdown.wait()
        for route in self.routes.values():
            route.wake.set()
        for thread in self.threads:
            thread.join(timeout=20)
