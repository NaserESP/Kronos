"""Bounded offline tests. Synthetic rows never enter the dashboard database."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
from http.server import ThreadingHTTPServer
import threading

from app import Engine, STEP, checked_bar, valid_prediction, score_values, make_handler, candle_color, prediction_view

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.engine=Engine(db_path=Path(self.tmp.name)/'test.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def history(self,target):
        with self.engine.db() as db:
            db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?)',
                           [(target-(400-i)*STEP,'100','103','98','101','5','505') for i in range(400)])

    def test_candle_colors_and_historical_match(self):
        self.assertEqual(candle_color('100','101'),'green')
        self.assertEqual(candle_color('100','99'),'red')
        self.assertEqual(candle_color('100.0','100.00'),'flat')
        self.assertIsNone(candle_color(None,100))
        # Close-to-previous-close is down, but this candle body is green.
        source=dict(open=90,close=95,basis_close=100,status='scored',observed_open='100',observed_close='98')
        row=prediction_view(source)
        self.assertEqual(row['predicted_color'],'green')
        self.assertEqual(row['actual_color'],'red')
        self.assertFalse(row['color_match'])
        self.assertEqual(row['actual_open'],100)
        source.update(open=100,close=100,observed_close='100')
        self.assertTrue(prediction_view(source)['color_match'])
        source.update(status='pending',observed_open=None,observed_close=None)
        self.assertIsNone(prediction_view(source)['actual_color'])
        self.assertIsNone(prediction_view(source)['color_match'])
        source.update(status='invalid')
        self.assertIsNone(prediction_view(source)['predicted_color'])

    def test_cumulative_color_success_ignores_price_error_and_unfinished(self):
        empty=self.engine.state()['statistics']['color_success']
        self.assertEqual(empty['evaluated'],0)
        self.assertIsNone(empty['accuracy'])
        # Huge price error, same green body: success. Almost exact close, red body: failure.
        cases=[('scored',1,10000,100,101),('scored',102,101.01,100,101),
               ('scored',100,100,100,100),('pending',100,101,100,101),
               ('invalid',100,101,100,101),('late',100,101,100,101)]
        with self.engine.db() as db:
            for i,(status,op,cl,actual_op,actual_cl) in enumerate(cases):
                target=(i+1)*STEP
                db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?)',
                           (target,str(actual_op),str(max(actual_op,actual_cl)+1),str(min(actual_op,actual_cl)-1),str(actual_cl),'1','100'))
                db.execute("INSERT INTO predictions (target,basis,basis_close,created,completed,open,close,status) VALUES (?,?,?,?,?,?,?,?)",
                           (target,target-STEP,100,target,target,op,cl,status))
        result=self.engine.state()['statistics']['color_success']
        self.assertEqual(result['evaluated'],3)
        self.assertEqual(result['correct'],2)
        self.assertEqual(result['incorrect'],1)
        self.assertEqual(result['pending'],1)
        self.assertAlmostEqual(result['accuracy'],2/3)
        # The next finalized candle changes the cumulative rate immediately.
        with self.engine.db() as db:
            db.execute("UPDATE predictions SET status='scored' WHERE status='pending'")
        result=self.engine.state()['statistics']['color_success']
        self.assertEqual(result['evaluated'],4)
        self.assertEqual(result['correct'],3)
        self.assertEqual(result['accuracy'],.75)
        self.assertEqual(result['pending'],0)

    def test_color_success_includes_history_beyond_list_limit(self):
        with self.engine.db() as db:
            for i in range(1001):
                target=(i+1)*STEP
                db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?)',(target,'100','102','99','101','1','100'))
                db.execute("INSERT INTO predictions (target,basis,basis_close,created,completed,open,close,status) VALUES (?,?,?,?,?,?,?,?)",
                           (target,target-STEP,100,target,target,100,101,'scored'))
        result=self.engine.state()['statistics']['color_success']
        self.assertEqual(result['evaluated'],1001)
        self.assertEqual(result['correct'],1001)
        self.assertEqual(result['accuracy'],1)

    def test_strict_candle_validation(self):
        raw=[STEP,'100','103','98','101','5',2*STEP-1,'505',1,0,0,0]
        self.assertEqual(checked_bar(raw)[0],STEP)
        for index,value in ((2,'99'),(5,'-1'),(6,2*STEP),(3,'NaN'),(0,STEP+1)):
            invalid=raw.copy(); invalid[index]=value
            with self.assertRaises(ValueError): checked_bar(invalid)

    def test_invalid_output_and_direction_metrics(self):
        self.assertTrue(valid_prediction([100,103,98,101,5,505]))
        self.assertFalse(valid_prediction([100,99,98,101,5,505]))
        self.assertFalse(valid_prediction([100,103,98,101,-1,505]))
        score=score_values({'basis_close':100,'close':102},{'close':101})
        self.assertEqual(score,dict(actual_close=101,abs_error=1,baseline_error=1,direction_hit=1))
        self.assertIsNone(score_values({'basis_close':100,'close':102},{'close':100})['direction_hit'])

    def test_forecast_no_future_prices_and_idempotent(self):
        import pandas as pd
        target=1_800_000_000_000//STEP*STEP
        self.history(target)
        calls=[]
        class Predictor:
            def predict(self,frame,xs,ys,**kwargs):
                calls.append((frame,xs,ys,kwargs))
                return pd.DataFrame([[101,104,99,102,4,408]],columns=['open','high','low','close','volume','amount'])
        self.engine.predictor=Predictor()
        self.engine.device='cpu';self.engine.model_revision='synthetic';self.engine.model_hash='synthetic'
        self.engine.enabled.set()
        with patch.object(self.engine,'now',return_value=target+1000):
            self.engine.forecast();self.engine.forecast()
        self.assertEqual(len(calls),1)
        frame,xs,ys,kwargs=calls[0]
        self.assertEqual(frame.shape,(400,6))
        self.assertLess(xs.iloc[-1],ys.iloc[0])
        self.assertEqual(kwargs['pred_len'],1)
        with self.engine.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM predictions').fetchone()[0],1)
            db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?)',(target,'101','104','99','103','5','515'))
        self.engine.score();self.engine.score()
        with self.engine.db() as db:
            row=db.execute('SELECT * FROM predictions').fetchone()
            self.assertEqual(row['status'],'scored')
            self.assertEqual(row['abs_error'],1)
            self.assertEqual(row['baseline_error'],2)
        projected=self.engine.prediction_records()[0]
        self.assertEqual(projected['actual_open'],101)
        self.assertEqual(projected['predicted_color'],'green')
        self.assertEqual(projected['actual_color'],'green')
        self.assertTrue(projected['color_match'])
        self.assertEqual(self.engine.state()['latest_prediction']['predicted_color'],'green')

    def test_mid_candle_start_skips_inference(self):
        target=1_800_000_000_000//STEP*STEP
        self.history(target)
        self.engine.predictor=object();self.engine.enabled.set()
        with patch.object(self.engine,'now',return_value=target+90_000):
            self.engine.forecast()
        with self.engine.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM predictions').fetchone()[0],0)
        self.assertEqual(self.engine.meta['phase'],'waiting')

    def test_source_gap_is_not_filled(self):
        target=1_800_000_000_000//STEP*STEP
        self.history(target-STEP)
        self.engine.enabled.set()
        self.engine.server_ms=target+1000
        self.engine.server_mono=__import__('time').monotonic()
        bad=[target,'101','104','99','103','5',target+STEP-1,'515',1,0,0,0]
        with patch.object(self.engine,'fetch',return_value=[bad]):
            with self.assertRaises(ValueError): self.engine.sync()
        with self.engine.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candles').fetchone()[0],400)

    def test_loopback_routes_and_control_origin(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.engine,0))
        port=server.server_address[1]
        server.RequestHandlerClass=make_handler(self.engine,port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{port}'
        try:
            with urllib.request.urlopen(url+'/api/state') as response:
                self.assertFalse(json.load(response)['enabled'])
            request=urllib.request.Request(url+'/api/control',data=b'{"action":"start"}',headers={'Content-Type':'application/json','Origin':'https://outside.example'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,403)
            request=urllib.request.Request(url+'/api/control',data=b'{"action":"start"}',headers={'Content-Type':'application/json','Origin':url})
            with urllib.request.urlopen(request) as response:
                self.assertTrue(json.load(response)['enabled'])
            request=urllib.request.Request(url+'/api/state',headers={'Host':'outside.example'})
            with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(request)
            request=urllib.request.Request(url+'/api/control',data=b'{"action":"shutdown"}',headers={'Content-Type':'application/json','Origin':url})
            with urllib.request.urlopen(request) as response:
                self.assertTrue(json.load(response)['shutdown'])
            self.assertTrue(self.engine.shutdown.is_set())
            self.assertFalse(self.engine.enabled.is_set())
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__': unittest.main()
