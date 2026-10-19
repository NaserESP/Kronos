from pathlib import Path
from urllib.request import urlopen
import json,hashlib,time
root=Path(__file__).resolve().parent
(root/'downloads').mkdir(exist_ok=True)
with urlopen('https://pypi.org/pypi/torch/2.8.0/json',timeout=60) as response:
    metadata=json.load(response)
entry=next(x for x in metadata['urls'] if x['filename']=='torch-2.8.0-cp312-cp312-win_amd64.whl')
path=root/'downloads'/entry['filename']
sha=hashlib.sha256()
count=0
last=time.monotonic()
with urlopen(entry['url'],timeout=60) as response, path.open('wb') as output:
    while data:=response.read(1024*1024):
        output.write(data)
        sha.update(data)
        count+=len(data)
        if time.monotonic()-last>20:
            print('CPU Torch',round(count/2**20),'of',round(entry['size']/2**20),'MiB',flush=True)
            last=time.monotonic()
assert count==entry['size']
assert sha.hexdigest()==entry['digests']['sha256']
(root/'downloads/torch-cpu-download.json').write_text(json.dumps({'url':entry['url'],'sha256':sha.hexdigest(),'bytes':count},indent=2))
print('CPU Torch downloaded and PyPI SHA256 verified',flush=True)
