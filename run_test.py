from pathlib import Path
import os
ROOT = Path(__file__).resolve().parent
os.environ['HF_HOME'] = str(ROOT / 'cache' / 'huggingface')
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'
os.environ['HF_HUB_DISABLE_XET'] = '1'
import argparse, hashlib, json, sys, time
from datetime import datetime, timezone
parser = argparse.ArgumentParser()
parser.add_argument('--download-only', action='store_true')
parser.add_argument('--offline', action='store_true')
args = parser.parse_args()
from huggingface_hub import HfApi, snapshot_download
manifest_path = ROOT / 'models' / 'manifest.json'
manifest_path.parent.mkdir(exist_ok=True)
if not args.offline:
    manifest = {}
    api = HfApi(token=False)
    for name in ('Kronos-Tokenizer-base', 'Kronos-base'):
        repo_id = 'NeoQuasar/' + name
        revision = api.model_info(repo_id, token=False).sha
        print('Downloading', repo_id, revision, flush=True)
        target = ROOT / 'models' / name
        snapshot_download(repo_id, revision=revision, local_dir=str(target), token=False,
                          allow_patterns=['config.json', 'model.safetensors', 'README.md'], max_workers=2)
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in target.iterdir() if p.is_file()}
        if not (target / 'model.safetensors').exists():
            raise RuntimeError('Expected safetensors weights missing')
        manifest[name] = {'repo_id': repo_id, 'revision': revision, 'sha256': files}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
if args.download_only:
    raise SystemExit(0)
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
for name, entry in manifest.items():
    for filename, expected in entry['sha256'].items():
        if hashlib.sha256((ROOT/'models'/name/filename).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Model file hash mismatch: ' + filename)
import numpy as np
import pandas as pd
import torch
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
SOURCE = ROOT if (ROOT/'model').is_dir() else ROOT/'repo'
sys.path.insert(0, str(SOURCE))
from model import Kronos, KronosTokenizer, KronosPredictor
device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
torch.set_num_threads(4)
tokenizer = KronosTokenizer.from_pretrained(str(ROOT/'models/Kronos-Tokenizer-base')).eval()
model = Kronos.from_pretrained(str(ROOT/'models/Kronos-base')).eval()
predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
source = SOURCE/'finetune_csv/data/HK_ali_09988_kline_5min_all.csv'
df = pd.read_csv(source)
df['timestamps'] = pd.to_datetime(df['timestamps'])
cols = ['open','high','low','close','volume','amount']
assert df['timestamps'].is_monotonic_increasing
assert not df['timestamps'].duplicated().any()
assert np.isfinite(df[cols].to_numpy()).all()
out = ROOT/'results'
out.mkdir(exist_ok=True)
lookback, horizon = 400, 24
rows = []
fig, axes = plt.subplots(3, 1, figsize=(12, 10), constrained_layout=True)
for i in range(3):
    end = len(df) - (2-i)*horizon
    actual = df.iloc[end-horizon:end].reset_index(drop=True)
    history = df.iloc[end-horizon-lookback:end-horizon].reset_index(drop=True)
    assert history['timestamps'].iloc[-1] < actual['timestamps'].iloc[0]
    torch.manual_seed(42+i)
    np.random.seed(42+i)
    if device != 'cpu': torch.cuda.reset_peak_memory_stats()
    if device != 'cpu': torch.cuda.synchronize()
    started = time.perf_counter()
    with torch.inference_mode():
        pred = predictor.predict(history[cols], history['timestamps'], actual['timestamps'],
                                 pred_len=horizon, T=1.0, top_p=0.9, sample_count=1, verbose=False)
    if device != 'cpu': torch.cuda.synchronize()
    elapsed = time.perf_counter()-started
    assert pred.shape == (horizon,6) and np.isfinite(pred.to_numpy()).all()
    baseline = float(history['close'].iloc[-1])
    error = np.abs(pred['close'].to_numpy()-actual['close'].to_numpy())
    base_error = np.abs(actual['close'].to_numpy()-baseline)
    invalid_ohlc = ((pred['high'] < pred[['open','close','low']].max(axis=1)) |
                    (pred['low'] > pred[['open','close','high']].min(axis=1))).sum()
    row = dict(window=i+1, start=str(actual['timestamps'].iloc[0]), end=str(actual['timestamps'].iloc[-1]),
               seed=42+i, seconds=elapsed, mae=float(error.mean()), baseline_mae=float(base_error.mean()),
               peak_gpu_allocated_mb=torch.cuda.max_memory_allocated()/2**20 if device != 'cpu' else 0,
               invalid_ohlc_rows=int(invalid_ohlc), negative_volume_rows=int((pred['volume']<0).sum()))
    rows.append(row)
    comparison = pred.reset_index(drop=True).add_prefix('pred_')
    comparison.insert(0,'timestamp',actual['timestamps'])
    comparison['actual_close'] = actual['close']
    comparison['baseline_close'] = baseline
    comparison.to_csv(out/f'window_{i+1}.csv',index=False)
    axes[i].plot(range(horizon),actual['close'],label='Actual',color='black')
    axes[i].plot(range(horizon),pred['close'],label='Kronos-base',color='#176cc1')
    axes[i].axhline(baseline,label='Last-price baseline',linestyle='--',color='#ce7b16')
    axes[i].set_title(f"Window {i+1}: {row['start']} to {row['end']}")
    axes[i].set_ylabel('HKD (supplied historical series)')
    axes[i].legend()
    print(json.dumps(row),flush=True)
fig.savefig(out/'comparison.png',dpi=150)
summary = dict(created_at=datetime.now(timezone.utc).isoformat(), model='NeoQuasar/Kronos-base',
               model_manifest=manifest, torch=torch.__version__, device=torch.cuda.get_device_name(0) if device != 'cpu' else 'CPU',
               lookback=lookback, horizon=horizon, sample_count=1, temperature=1.0, top_p=0.9,
               dataset=dict(path=str(source.relative_to(ROOT)),sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                            rows=len(df), start=str(df['timestamps'].iloc[0]),end=str(df['timestamps'].iloc[-1])),
               windows=rows, mean_mae=float(np.mean([r['mae'] for r in rows])),
               mean_baseline_mae=float(np.mean([r['baseline_mae'] for r in rows])),
               limitation='Installation smoke and three historical windows only. Training overlap unknown. No profitability or independent accuracy claim.')
(out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print('Saved results to',out,flush=True)
