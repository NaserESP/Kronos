"""Synthetic isolated fixtures; no writes to research databases."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from app import Engine, STEP, utc
from dual import MarketStore, MultiEngine, SharedModel, ResearchRoute, TwapRoute, decode_market, FEED

TARGET = 1_800_000_000_000//STEP*STEP
def fixture(target=TARGET, opened='100', closed='101', winner=0):
    return dict(slug='fixture',startAt=utc(target),expirationTimestamp=target+STEP,
        status='RESOLVED',winningOutcomeIndex=winner,metadata=dict(
            openPrice=opened,openPriceCapturedAt=utc(target),openPriceSelectedAt=utc(target),
            chainlinkDataStream=dict(feedId=FEED,streamType='twap',twapWindowSeconds=60,priceDecimals=18),
            chainlinkTwapResolution=dict(feedId=FEED,winningIndex=winner,
                open=dict(raw=str(int(float(opened)*10**18)),targetAt=utc(target),selectedAt=utc(target)),
                close=dict(raw=str(int(float(closed)*10**18)),targetAt=utc(target+STEP),selectedAt=utc(target+STEP)))))

class DualTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.run,self.stop=threading.Event(),threading.Event();self.run.set()
        self.store=MarketStore(self.root/'official.sqlite3',self.run,self.stop,lambda:TARGET+1000)
    def tearDown(self):
        self.tmp.cleanup()
    def test_partial_live_reference_waits_without_assuming_timestamp(self):
        data=fixture();data['metadata'].pop('openPriceSelectedAt')
        self.assertIsNone(decode_market(data,TARGET))

    def test_feed_identity_boundaries_and_tie(self):
        data=fixture(opened='100',closed='100')
        self.assertEqual(decode_market(data,TARGET)['outcome'],'up')
        data['metadata']['chainlinkDataStream']['streamType']='spot'
        with self.assertRaises(ValueError):decode_market(data,TARGET)
        data=fixture();data['expirationTimestamp']+=1
        with self.assertRaises(ValueError):decode_market(data,TARGET)
        data=fixture(winner=1)
        with self.assertRaises(ValueError):decode_market(data,TARGET)
    def test_pair_cohort_excludes_single_route_late_and_missing_reference(self):
        item=decode_market(fixture(),TARGET)
        self.store.put(item,observed=TARGET+500)
        context=self.store.context(TARGET)
        p=dict(target=TARGET,created=TARGET+1000,completed=TARGET+1500,close=102,status='pending')
        self.store.record('binance',p,context,100)
        self.assertEqual(self.store.comparison()['paired'],0)
        self.store.record('twap',dict(p,status='late'),context,100)
        self.assertEqual(self.store.comparison()['paired'],0)
        with self.store.db() as db:db.execute('DELETE FROM signals WHERE route="twap"')
        self.store.record('twap',dict(p,close=99),context,100)
        self.assertEqual(self.store.comparison()['paired'],1)
        self.assertEqual(self.store.comparison()['routes']['binance']['accuracy'],1)
        self.assertEqual(self.store.comparison()['routes']['twap']['accuracy'],0)
        # A price first observed after the forecast cannot be attached retrospectively.
        with self.store.db() as db:db.execute('DELETE FROM signals')
        self.store.record('binance',dict(p,created=TARGET+100),context,100)
        self.assertEqual(self.store.summary('binance')['evaluated'],0)
    def test_reference_price_not_predicted_open_decides_signal_and_no_rewrite(self):
        self.store.put(decode_market(fixture(),TARGET),observed=TARGET+100)
        p=dict(target=TARGET,created=TARGET+1000,completed=TARGET+1200,open=110,close=105,status='pending')
        self.store.record('binance',p,self.store.context(TARGET),100)
        self.store.record('binance',dict(p,close=90),self.store.context(TARGET),100)
        self.assertEqual(self.store.summary('binance')['correct'],1)
        changed=decode_market(fixture(opened='101',closed='102'),TARGET)
        with self.assertRaises(ValueError):self.store.put(changed)
    def test_two_databases_keep_same_target_independent(self):
        engine=MultiEngine(self.root)
        for index,route in enumerate(engine.routes.values()):
            with route.db() as db:
                db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?)',(TARGET,str(100+index),'110','90','101','0','0'))
        with engine.route('binance').db() as db:self.assertEqual(db.execute('SELECT open FROM candles').fetchone()[0],'100')
        with engine.route('twap').db() as db:self.assertEqual(db.execute('SELECT open FROM candles').fetchone()[0],'101')
        with self.assertRaises(KeyError):engine.route('other')
    def test_no_volume_series_validates_prices_without_inventing_volume(self):
        route=TwapRoute('twap',self.store,SharedModel(),db_path=self.root/'volume.sqlite3')
        self.assertTrue(route.valid_output([100,103,98,101,-1,-2]))
        self.assertTrue(route.valid_output([100,99,98,101,0,0]))
        self.assertFalse(route.valid_output([100,103,98,float('nan'),0,0]))
        self.assertFalse(route.valid_output([100,103,98,-1,0,0]))
        self.assertFalse(Engine.valid_output(route,[100,103,98,101,-1,-2]))

    def test_twap_scores_official_close_not_next_round_sample(self):
        route=TwapRoute('twap',self.store,SharedModel(),db_path=self.root/'score.sqlite3')
        self.store.put(decode_market(fixture(),TARGET),observed=TARGET+100)
        with route.db() as db:
            db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?)',(TARGET,'90','90','90','90','0','0'))
            db.execute('INSERT INTO predictions (target,basis,basis_close,created,completed,open,close,status) VALUES (?,?,?,?,?,?,?,?)',
                       (TARGET,TARGET-STEP,100,TARGET+1000,TARGET+1500,100,102,'pending'))
        route.score()
        with route.db() as db:
            row=db.execute('SELECT * FROM predictions').fetchone()
        self.assertEqual(row['actual_close'],101)
        self.assertEqual(row['status'],'scored')
        self.assertEqual(row['abs_error'],1)

    def test_single_model_load_and_shared_inference_lock(self):
        runtime=SharedModel()
        first=ResearchRoute('binance',self.store,runtime,db_path=self.root/'b.sqlite3')
        second=TwapRoute('twap',self.store,runtime,db_path=self.root/'t.sqlite3')
        def load(engine):
            engine.predictor=object();engine.device='cpu';engine.model_revision='test';engine.model_hash='test'
            engine.update(model_ready=True,device='CPU')
        with patch.object(Engine,'load_model',side_effect=load) as mocked:
            first.load_model();second.load_model()
            self.assertEqual(mocked.call_count,1)
        self.assertIs(first.predictor,second.predictor)
        self.assertIs(first.inference_lock,second.inference_lock)
    def test_twap_no_binance_seed_no_future_point_or_filled_gap(self):
        runtime=SharedModel()
        route=TwapRoute('twap',self.store,runtime,db_path=self.root/'t.sqlite3')
        route.enabled=self.run
        route.now=lambda:TARGET+1000
        self.store.put(decode_market(fixture(target=TARGET-STEP),TARGET-STEP),observed=TARGET+10)
        self.store.put(decode_market(fixture(),TARGET),observed=TARGET+10)
        self.store.put(decode_market(fixture(target=TARGET+STEP),TARGET+STEP),observed=TARGET+STEP+10)
        route.import_seed();route.sync()
        with route.db() as db:
            rows=db.execute('SELECT * FROM candles ORDER BY t').fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[-1]['t'],TARGET-STEP)
        self.assertTrue(all(r['open']==r['high']==r['low']==r['close'] for r in rows))
        self.assertEqual(route.meta['phase'],'syncing')
        with self.store.db() as db:db.execute('DELETE FROM markets WHERE target=?',(TARGET-STEP,))
        route.sync()
        # Real old observations stay stored; no invented intervening candles are added.
        with route.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM candles').fetchone()[0],2)

if __name__=='__main__':unittest.main()
