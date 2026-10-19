"""Loopback-only BTCUSDT research dashboard. Public market data; no trading."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('KRONOS_ROOT', HERE.parent))
STEP = 300_000
LOOKBACK = 400
API = 'https://data-api.binance.vision'
DATASET = ROOT/'data/BTCUSDT-spot-5m-2026Q3/BTCUSDT-5m-kronos.csv'
COLS = ['open', 'high', 'low', 'close', 'volume', 'amount']

def utc(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).isoformat()

def checked_bar(raw):
    if len(raw) != 12:
        raise ValueError('قالب کندل دریافتی معتبر نیست.')
    opened, closed = int(raw[0]), int(raw[6])
    if opened % STEP or closed != opened+STEP-1:
        raise ValueError('مرز زمانی کندل ۵دقیقه‌ای معتبر نیست.')
    nums = [Decimal(raw[i]) for i in (1,2,3,4,5,7)]
    if not all(n.is_finite() for n in nums):
        raise ValueError('دادهٔ عددی نامعتبر دریافت شد.')
    op, hi, lo, cl, vol, amt = nums
    if min(op, hi, lo, cl) <= 0 or lo > min(op, cl) or hi < max(op, cl) or lo > hi or min(vol,amt)<0:
        raise ValueError('قیمت یا حجم کندل معتبر نیست.')
    return [opened]+[str(n) for n in nums]

def valid_prediction(p):
    return (len(p)==6 and all(math.isfinite(float(n)) for n in p)
            and min(p[:4])>0 and p[2]<=min(p[0],p[3])<=max(p[0],p[3])<=p[1]
            and min(p[4:])>=0)

def score_values(pred, actual):
    baseline = float(pred['basis_close'])
    actual_close = float(actual['close'])
    predicted_close = float(pred['close'])
    delta = actual_close-baseline
    sign = lambda x: (x>0)-(x<0)
    return dict(actual_close=actual_close, abs_error=abs(predicted_close-actual_close),
                baseline_error=abs(baseline-actual_close),
                direction_hit=None if delta==0 else int(sign(predicted_close-baseline)==sign(delta)))

def candle_color(opened, closed):
    """Candle body direction, distinct from close-to-previous-close return."""
    if opened is None or closed is None:
        return None
    op, cl = Decimal(str(opened)), Decimal(str(closed))
    if not op.is_finite() or not cl.is_finite():
        return None
    return 'green' if cl > op else 'red' if cl < op else 'flat'

def prediction_view(record):
    row = dict(record)
    observed_open = row.pop('observed_open', None)
    observed_close = row.pop('observed_close', None)
    row['actual_open'] = float(observed_open) if observed_open is not None else None
    row['predicted_color'] = candle_color(row['open'], row['close']) if row['status'] in ('pending', 'scored') else None
    row['actual_color'] = candle_color(observed_open, observed_close)
    row['color_match'] = (row['predicted_color'] == row['actual_color']
                          if row['predicted_color'] is not None and row['actual_color'] is not None
                          and row['status'] == 'scored' else None)
    return row

class Engine:
    def __init__(self, db_path=None, seed_path=None):
        self.db_path = Path(db_path or HERE/'state/dashboard.sqlite3')
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.seed_path = Path(seed_path or DATASET)
        self.lock = threading.Lock()
        self.inference_lock = threading.Lock()
        self.enabled = threading.Event()
        self.wake = threading.Event()
        self.shutdown = threading.Event()
        self.server_ms = None
        self.server_mono = 0
        self.predictor = None
        self.model_revision = None
        self.model_hash = None
        self.meta = dict(phase='paused', message='برای دریافت داده و پیش‌بینی، شروع را بزنید.',
                         error=None, device=None, model_ready=False, last_sync=None,
                         last_prediction_seconds=None, current_price=None, current_price_time=None,
                         sync_pages=0, network='unknown')
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS candles (
              t INTEGER PRIMARY KEY, open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
              close TEXT NOT NULL, volume TEXT NOT NULL, amount TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS predictions (
              target INTEGER PRIMARY KEY, basis INTEGER NOT NULL, basis_close REAL NOT NULL,
              created INTEGER NOT NULL, completed INTEGER NOT NULL, open REAL, high REAL, low REAL,
              close REAL, volume REAL, amount REAL, status TEXT NOT NULL, reason TEXT,
              seconds REAL, seed INTEGER, model_revision TEXT, model_sha256 TEXT, input_sha256 TEXT,
              actual_close REAL, abs_error REAL, baseline_error REAL, direction_hit INTEGER);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        db.create_function("candle_color", 2, candle_color, deterministic=True)
        try:
            with db:
                yield db
        finally:
            db.close()

    def update(self, **values):
        with self.lock:
            self.meta.update(values)

    def now(self):
        if self.server_ms is None:
            return int(time.time()*1000)
        return self.server_ms+int((time.monotonic()-self.server_mono)*1000)

    def fetch(self, path, **params):
        url = API+path+('?' + urlencode(params) if params else '')
        req = Request(url, headers={'User-Agent':'Kronos-Local/1.0', 'Accept':'application/json'})
        try:
            with urlopen(req, timeout=15) as response:
                if int(response.headers.get('Content-Length',0))>3_000_000:
                    raise ValueError('پاسخ منبع بیش از حد بزرگ است.')
                data = response.read(3_000_001)
                if len(data)>3_000_000:
                    raise ValueError('پاسخ منبع بیش از حد بزرگ است.')
                result = json.loads(data)
        except HTTPError as exc:
            raise RuntimeError(f'بایننس پاسخ HTTP {exc.code} داد؛ دریافت داده متوقف شد.') from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError('اتصال به دادهٔ عمومی بایننس برقرار نشد؛ دوباره تلاش می‌شود.') from None
        if isinstance(result, dict) and 'code' in result and int(result['code'])<0:
            raise RuntimeError('منبع داده درخواست را نپذیرفت.')
        return result

    def import_seed(self):
        with self.db() as db:
            if db.execute('SELECT COUNT(*) FROM candles').fetchone()[0]:
                return
        self.update(phase='importing', message='در حال آماده‌کردن تاریخچهٔ سه‌ماهه…')
        validation_path = self.seed_path.with_name('validation.json')
        report = json.loads(validation_path.read_text(encoding='utf-8'))
        if hashlib.sha256(self.seed_path.read_bytes()).hexdigest()!=report['output_sha256']:
            raise ValueError('هش تاریخچه با گزارش اعتبارسنجی مطابقت ندارد.')
        rows=[]
        with self.seed_path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                dt = datetime.strptime(row['timestamps'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                t = int(dt.timestamp()*1000)
                # Reuse the strict live validation with the documented timestamp conversion.
                raw=[t,row['open'],row['high'],row['low'],row['close'],row['volume'],t+STEP-1,row['amount'],0,0,0,0]
                rows.append(checked_bar(raw))
        if len(rows)!=report['rows'] or not rows or any(b[0]-a[0]!=STEP for a,b in zip(rows,rows[1:])):
            raise ValueError('تاریخچه ناقص یا نامرتب است.')
        with self.db() as db:
            db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?)', rows)
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('seed_sha256',report['output_sha256']))

    def load_model(self):
        self.update(phase='loading',message='در حال بارگذاری Kronos-base…')
        os.environ['HF_HOME']=str(ROOT/'cache/huggingface')
        os.environ['HF_HUB_OFFLINE']='1'
        os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']='1'
        manifest=json.loads((ROOT/'models/manifest.json').read_text(encoding='utf-8'))
        for name,entry in manifest.items():
            for filename,expected in entry['sha256'].items():
                if hashlib.sha256((ROOT/'models'/name/filename).read_bytes()).hexdigest()!=expected:
                    raise ValueError('وزن مدل با هش ثبت‌شده تطبیق ندارد.')
        import torch
        sys.path.insert(0, str(ROOT if (ROOT/'model').is_dir() else ROOT/'repo'))
        from model import Kronos, KronosTokenizer, KronosPredictor
        torch.set_num_threads(4)
        self.device='cuda:0' if torch.cuda.is_available() else 'cpu'
        tokenizer=KronosTokenizer.from_pretrained(str(ROOT/'models/Kronos-Tokenizer-base')).eval()
        model=Kronos.from_pretrained(str(ROOT/'models/Kronos-base')).eval()
        self.predictor=KronosPredictor(model,tokenizer,device=self.device,max_context=512)
        self.model_revision=manifest['Kronos-base']['revision']
        self.model_hash=manifest['Kronos-base']['sha256']['model.safetensors']
        self.update(model_ready=True,device=torch.cuda.get_device_name(0) if self.device!='cpu' else 'CPU')

    def sync(self):
        if self.server_ms is None or time.monotonic()-self.server_mono>60:
            timing=self.fetch('/api/v3/time')
            self.server_ms=int(timing['serverTime'])
            self.server_mono=time.monotonic()
        now=self.now()
        latest_closed=(now//STEP-1)*STEP
        with self.db() as db:
            latest=db.execute('SELECT MAX(t) FROM candles').fetchone()[0]
        if latest is None:
            raise ValueError('تاریخچهٔ اولیه پیدا نشد.')
        cursor=latest+STEP
        pages=0
        while cursor<=latest_closed and self.enabled.is_set() and not self.shutdown.is_set():
            self.update(phase='syncing',message='در حال تکمیل کندل‌های عقب‌افتاده…',sync_pages=pages)
            raw=self.fetch('/api/v3/klines',symbol='BTCUSDT',interval='5m',startTime=cursor,
                           endTime=latest_closed+STEP-1,limit=1000)
            if not isinstance(raw,list) or not raw:
                raise RuntimeError('کندل موردنیاز هنوز در منبع موجود نیست؛ دریافت دوباره انجام می‌شود.')
            batch=[checked_bar(r) for r in raw]
            if batch[0][0]!=cursor or any(b[0]-a[0]!=STEP for a,b in zip(batch,batch[1:])):
                raise ValueError('در تاریخچهٔ بایننس فاصلهٔ گمشده دیده شد؛ پیش‌بینی متوقف است.')
            if batch[-1][0]>latest_closed:
                raise ValueError('دریافت کندل باز به‌جای کندل بسته‌شده رد شد.')
            with self.db() as db:
                db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?)',batch)
            cursor=batch[-1][0]+STEP
            pages+=1
        if not self.enabled.is_set():
            return
        # Current candle is display-only. It never enters the model's history.
        current=self.fetch('/api/v3/klines',symbol='BTCUSDT',interval='5m',limit=2)
        bars=[checked_bar(r) for r in current]
        if not bars or bars[-1][0] != self.now()//STEP*STEP:
            raise ValueError('کندل جاری منبع تازه نیست؛ دادهٔ قدیمی به‌عنوان زنده نمایش داده نمی‌شود.')
        self.update(current_price=float(bars[-1][4]),current_price_time=self.now(),
                    current_open=float(bars[-1][1]),current_candle_time=bars[-1][0],last_sync=self.now(),
                    network='connected',error=None,sync_pages=pages)
        self.score()
        self.forecast()

    def score(self):
        with self.db() as db:
            matches=db.execute('SELECT p.*, c.close AS observed_close FROM predictions p JOIN candles c ON p.target=c.t WHERE p.status="pending"').fetchall()
            for row in matches:
                metrics=score_values(row,{'close':row['observed_close']})
                db.execute('UPDATE predictions SET status="scored",actual_close=?,abs_error=?,baseline_error=?,direction_hit=? WHERE target=?',
                           (*metrics.values(),row['target']))

    def valid_output(self, values):
        return valid_prediction(values)

    def forecast(self):
        with self.inference_lock:
            return self._forecast()

    def _forecast(self):
        if not self.enabled.is_set() or self.predictor is None:
            return
        now=self.now()
        with self.db() as db:
            history=list(reversed(db.execute('SELECT * FROM candles ORDER BY t DESC LIMIT ?', (LOOKBACK,)).fetchall()))
            if len(history)!=LOOKBACK:
                raise ValueError('برای پیش‌بینی ۴۰۰ کندل لازم است.')
            basis=history[-1]['t']
            target=basis+STEP
            if db.execute('SELECT 1 FROM predictions WHERE target=?',(target,)).fetchone():
                self.update(phase='live',message='پیش‌بینی ثبت شد؛ منتظر کندل بسته‌شدهٔ بعدی.',error=None)
                return
        if target!=now//STEP*STEP:
            raise ValueError('دادهٔ بسته‌شده به‌روز نیست؛ پیش‌بینی انجام نشد.')
        if now-target>45_000:
            self.update(phase='waiting',message='داده آماده است؛ پیش‌بینی از شروع کندل بعدی ثبت می‌شود.',error=None)
            return
        if any(b['t']-a['t']!=STEP for a,b in zip(history,history[1:])):
            raise ValueError('ورودی مدل پیوسته نیست.')
        import numpy as np
        import pandas as pd
        import torch
        seed=target//STEP % (2**31)
        input_hash=hashlib.sha256(json.dumps([dict(r) for r in history],sort_keys=True).encode()).hexdigest()
        frame=pd.DataFrame([{key:float(r[key]) for key in COLS} for r in history])
        xs=pd.Series(pd.to_datetime([r['t'] for r in history],unit='ms'))
        ys=pd.Series(pd.to_datetime([target],unit='ms'))
        self.update(phase='predicting',message='مدل در حال ساخت کندل بعدی است…')
        torch.manual_seed(seed)
        np.random.seed(seed)
        started=time.perf_counter()
        if self.device!='cpu': torch.cuda.synchronize()
        with torch.inference_mode():
            result=self.predictor.predict(frame,xs,ys,pred_len=1,T=1.0,top_p=0.9,sample_count=1,verbose=False)
        if self.device!='cpu': torch.cuda.synchronize()
        seconds=time.perf_counter()-started
        values=[float(result.iloc[0][col]) for col in COLS]
        completed=self.now()
        valid=self.valid_output(values)
        status='pending' if valid else 'invalid'
        reason=None if valid else 'خروجی مدل قیود قیمت یا حجم را رعایت نکرد؛ از ارزیابی حذف شد.'
        if completed-target>60_000:
            status='late'
            reason='خروجی دیر آماده شد؛ در ارزیابی زنده محاسبه نمی‌شود.'
        if not self.enabled.is_set():
            return
        # SQLite does not support JSON NaN: invalid non-finite outputs remain null.
        safe=[v if math.isfinite(v) else None for v in values]
        with self.db() as db:
            db.execute('''INSERT OR IGNORE INTO predictions
            (target,basis,basis_close,created,completed,open,high,low,close,volume,amount,status,reason,seconds,seed,model_revision,model_sha256,input_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (target,basis,float(history[-1]['close']),now,completed,*safe,status,reason,seconds,seed,self.model_revision,self.model_hash,input_hash))
        self.update(phase='live',message=reason or 'پیش‌بینی ثبت شد؛ منتظر نتیجهٔ واقعی.',last_prediction_seconds=seconds)

    def prediction_records(self, limit=1000):
        with self.db() as db:
            rows=db.execute('''SELECT p.*, c.open AS observed_open, c.close AS observed_close
              FROM predictions p LEFT JOIN candles c ON c.t=p.target
              ORDER BY p.target DESC LIMIT ?''',(limit,)).fetchall()
        return [prediction_view(row) for row in rows]

    def state(self):
        with self.lock:
            meta=dict(self.meta)
        with self.db() as db:
            coverage=dict(db.execute('SELECT COUNT(*) AS count, MIN(t) AS first, MAX(t) AS last FROM candles').fetchone())
            stats=dict(db.execute('''SELECT COUNT(*) AS count, AVG(abs_error) AS mae,
              AVG(baseline_error) AS baseline_mae, AVG(direction_hit) AS direction_accuracy
              FROM predictions WHERE status="scored"''').fetchone())
            color_stats=db.execute("""SELECT COUNT(*) AS evaluated,
              COALESCE(SUM(candle_color(p.open,p.close)=candle_color(c.open,c.close)),0) AS correct
              FROM predictions p JOIN candles c ON c.t=p.target
              WHERE p.status='scored' AND candle_color(p.open,p.close) IS NOT NULL
              AND candle_color(c.open,c.close) IS NOT NULL""").fetchone()
            evaluated,correct=color_stats['evaluated'],color_stats['correct']
            pending=db.execute("SELECT COUNT(*) FROM predictions WHERE status='pending'").fetchone()[0]
            stats['color_success']=dict(evaluated=evaluated,correct=correct,incorrect=evaluated-correct,
                                        accuracy=correct/evaluated if evaluated else None,pending=pending)
            excluded=db.execute('SELECT COUNT(*) FROM predictions WHERE status IN ("late","invalid")').fetchone()[0]
        current_time=self.now()
        latest=self.prediction_records(limit=1)
        meta.update(enabled=self.enabled.is_set(),server_time=current_time,model='Kronos-base',symbol='BTCUSDT',
                    interval='5m',lookback=LOOKBACK,coverage=coverage,latest_prediction=latest[0] if latest else None,
                    statistics=stats,excluded_predictions=excluded,
                    stale=meta['last_sync'] is None or current_time-meta['last_sync']>30_000,
                    source='Binance Spot', data_endpoint=API)
        return meta

    def worker(self):
        try:
            self.import_seed()
            self.update(phase='paused',message='تاریخچه آماده است؛ شروع را بزنید.')
        except Exception as exc:
            self.update(phase='error',error=str(exc),message='تاریخچه قابل استفاده نیست.')
            return
        while not self.shutdown.is_set():
            if self.enabled.is_set():
                try:
                    if self.predictor is None:
                        self.load_model()
                    self.sync()
                except Exception as exc:
                    self.update(phase='error',error=str(exc),network='error',message='دریافت یا پردازش متوقف شد؛ تلاش بعدی خودکار است.')
            else:
                self.update(phase='paused',message='دریافت و پیش‌بینی متوقف است.')
            self.wake.wait(8 if self.meta['phase']!='error' else 20)
            self.wake.clear()

    def control(self, action):
        if action=='start': self.enabled.set()
        elif action=='stop': self.enabled.clear()
        elif action!='refresh': raise ValueError('فرمان نامعتبر است.')
        self.wake.set()

def make_handler(engine, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def allowed(self):
            return self.headers.get('Host') in (f'127.0.0.1:{port}',f'localhost:{port}')

        def send(self, body, kind='application/json; charset=utf-8', status=200):
            if isinstance(body,(dict,list)):
                body=json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8')
            elif isinstance(body,str): body=body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed(): return self.send({'error':'Host rejected'},status=403)
            parsed=urlparse(self.path)
            query=parse_qs(parsed.query)
            try:
                if parsed.path=='/api/state': return self.send(engine.state())
                if parsed.path=='/api/comparison' and hasattr(engine,'comparison'):
                    return self.send(engine.comparison())
                if parsed.path=='/api/candles':
                    route=engine.route(query.get('route',['binance'])[0]) if hasattr(engine,'route') else engine
                    limit=max(20,min(500,int(query.get('limit',['120'])[0])))
                    with route.db() as db:
                        rows=list(reversed(db.execute('SELECT * FROM candles ORDER BY t DESC LIMIT ?',(limit,)).fetchall()))
                    return self.send([dict(t=r['t'],**{k:float(r[k]) for k in COLS}) for r in rows])
                if parsed.path in ('/api/predictions','/api/export.csv'):
                    route=engine.route(query.get('route',['binance'])[0]) if hasattr(engine,'route') else engine
                    rows=route.prediction_records()
                    if parsed.path.endswith('.csv'):
                        out=io.StringIO(newline='')
                        if rows:
                            writer=csv.DictWriter(out,fieldnames=rows[0].keys())
                            writer.writeheader(); writer.writerows([dict(r) for r in rows])
                        return self.send(out.getvalue(),'text/csv; charset=utf-8')
                    return self.send([dict(r) for r in rows[:100]])
                files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),
                       '/style.css':('style.css','text/css; charset=utf-8'),
                       '/Vazirmatn-Regular.ttf':('Vazirmatn-Regular.ttf','font/ttf'),
                       '/Vazirmatn-Bold.ttf':('Vazirmatn-Bold.ttf','font/ttf')}
                if parsed.path in files:
                    name,kind=files[parsed.path]
                    return self.send((HERE/'static'/name).read_bytes(),kind)
                return self.send({'error':'Not found'},status=404)
            except (ValueError,KeyError): return self.send({'error':'درخواست نامعتبر است.'},status=400)
            except Exception: return self.send({'error':'خواندن اطلاعات ممکن نشد.'},status=500)

        def do_POST(self):
            if not self.allowed(): return self.send({'error':'Host rejected'},status=403)
            if self.headers.get('Origin') not in (None,f'http://127.0.0.1:{port}',f'http://localhost:{port}'):
                return self.send({'error':'Origin rejected'},status=403)
            if self.path!='/api/control': return self.send({'error':'Not found'},status=404)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=256 or self.headers.get('Content-Type')!='application/json':
                    raise ValueError()
                action=json.loads(self.rfile.read(length))['action']
                if action=='shutdown':
                    engine.enabled.clear()
                    engine.shutdown.set()
                    self.send({'ok':True,'shutdown':True})
                    threading.Thread(target=self.server.shutdown,daemon=True).start()
                    return
                engine.control(action)
                self.send({'ok':True,'enabled':engine.enabled.is_set()})
            except (ValueError,KeyError): self.send({'error':'فرمان نامعتبر است.'},status=400)
    return Handler

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=7071)
    parser.add_argument('--autostart',action='store_true')
    args=parser.parse_args()
    from dual import MultiEngine
    engine=MultiEngine()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),make_handler(engine,args.port))
    server.daemon_threads=True
    if args.autostart: engine.enabled.set()
    thread=threading.Thread(target=engine.worker,daemon=True)
    thread.start()
    print(f'Kronos dashboard: http://127.0.0.1:{args.port}',flush=True)
    try: server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt: pass
    finally:
        engine.shutdown.set(); engine.wake.set(); server.server_close()

if __name__=='__main__': main()
