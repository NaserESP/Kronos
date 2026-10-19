from pathlib import Path
from urllib.request import Request, urlopen
import hashlib, json, time
ROOT = Path(__file__).resolve().parent
model_root = ROOT/'models'
model_root.mkdir(exist_ok=True)
manifest = {}
def get(url):
    return urlopen(Request(url,headers={'User-Agent':'Kronos-local-test/1.0'}),timeout=90)
for name in ('Kronos-Tokenizer-base','Kronos-base'):
    repo = 'NeoQuasar/'+name
    with get('https://huggingface.co/api/models/'+repo) as response:
        metadata=json.load(response)
    revision=metadata['sha']
    target=model_root/name
    target.mkdir(exist_ok=True)
    hashes={}
    for filename in ('config.json','model.safetensors','README.md'):
        path=target/filename
        digest=hashlib.sha256()
        with get('https://huggingface.co/'+repo+'/resolve/'+revision+'/'+filename) as response:
            expected=response.headers.get('Content-Length')
            downloaded=0
            last=time.monotonic()
            with path.open('wb') as output:
                while chunk:=response.read(1024*1024):
                    output.write(chunk)
                    digest.update(chunk)
                    downloaded+=len(chunk)
                    if time.monotonic()-last>20:
                        print(name,filename,round(downloaded/2**20),'MiB',flush=True)
                        last=time.monotonic()
        if expected and downloaded!=int(expected):
            raise RuntimeError('Download size mismatch')
        hashes[filename]=digest.hexdigest()
        print(name,filename,'complete',downloaded,flush=True)
    manifest[name]={'repo_id':repo,'revision':revision,'sha256':hashes}
(model_root/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print('Both model snapshots downloaded',flush=True)
