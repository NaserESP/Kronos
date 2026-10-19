from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen
import json, time, hashlib
ROOT=Path(__file__).resolve().parent
URL='https://download.pytorch.org/whl/cu126/torch-2.8.0%2Bcu126-cp312-cp312-win_amd64.whl'
SIZE=2915418348
CHUNK=16*1024*1024
parts=ROOT/'downloads'/'torch-parts'
parts.mkdir(parents=True,exist_ok=True)
def fetch(i):
    start=i*CHUNK
    end=min(SIZE,start+CHUNK)-1
    path=parts/f'{i:04d}.part'
    if path.exists() and path.stat().st_size==end-start+1:
        return i
    for attempt in range(3):
        try:
            req=Request(URL,headers={'Range':f'bytes={start}-{end}'})
            with urlopen(req,timeout=60) as response:
                if response.status!=206 or response.headers.get('Content-Range')!=f'bytes {start}-{end}/{SIZE}':
                    raise RuntimeError('Unexpected byte range')
                with path.open('wb') as output:
                    while data:=response.read(1024*1024):
                        output.write(data)
            if path.stat().st_size!=end-start+1:
                raise RuntimeError('Incomplete range')
            return i
        except Exception:
            if attempt==2:
                raise
            time.sleep(2)
    raise RuntimeError('Unreachable')
count=(SIZE+CHUNK-1)//CHUNK
completed=0
started=time.monotonic()
with ThreadPoolExecutor(max_workers=8) as pool:
    futures=[pool.submit(fetch,i) for i in range(count)]
    for future in as_completed(futures):
        future.result()
        completed+=1
        if completed%4==0 or completed==count:
            print(f'Torch: {completed}/{count} parts; {min(completed*CHUNK,SIZE)/2**20:.0f} MiB; {time.monotonic()-started:.0f}s',flush=True)
wheel=ROOT/'downloads'/'torch-2.8.0+cu126-cp312-cp312-win_amd64.whl'
digest=hashlib.sha256()
with wheel.open('wb') as output:
    for i in range(count):
        with (parts/f'{i:04d}.part').open('rb') as source:
            while data:=source.read(1024*1024):
                output.write(data)
                digest.update(data)
assert wheel.stat().st_size==SIZE
# Verify against the official index, independently of the downloaded wheel.
with urlopen('https://download.pytorch.org/whl/cu126/torch/',timeout=60) as response:
    import re
    index=response.read().decode()
links=re.findall(r'href="([^"]+)"',index)
matching=[link for link in links if 'torch-2.8.0%2Bcu126-cp312-cp312-win_amd64.whl#sha256=' in link]
if len(matching)!=1:
    raise RuntimeError('Official wheel hash not uniquely found')
expected=matching[0].split('#sha256=')[1]
if digest.hexdigest()!=expected:
    raise RuntimeError('Official wheel SHA256 mismatch')
(ROOT/'downloads'/'torch-download.json').write_text(json.dumps({'url':URL,'size':SIZE,'sha256':expected},indent=2))
print('Wheel complete; official SHA256 verified:',expected,flush=True)
