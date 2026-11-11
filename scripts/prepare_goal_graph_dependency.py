import hashlib,json,urllib.request
from pathlib import Path
out=Path(__file__).resolve().parents[2]/'dependencies/networkx-3.4.2';out.mkdir(parents=True,exist_ok=True)
meta=json.load(urllib.request.urlopen('https://pypi.org/pypi/networkx/3.4.2/json',timeout=15))
wheel=next(x for x in meta['urls'] if x['filename']=='networkx-3.4.2-py3-none-any.whl')
assert wheel['digests']['sha256']=='df5d4365b724cf81b8c6a7312509d0c22386097011ad1abe274afd5e9d3bbc5f'
p=out/wheel['filename']
if not p.exists():
 data=urllib.request.urlopen(wheel['url'],timeout=15).read();assert hashlib.sha256(data).hexdigest()==wheel['digests']['sha256']
 with p.open('xb') as stream:stream.write(data)
assert hashlib.sha256(p.read_bytes()).hexdigest()==wheel['digests']['sha256']
metadata=json.dumps({'name':'networkx','version':'3.4.2','requires_python':meta['info']['requires_python'],'license':meta['info']['license'],'url':wheel['url'],'sha256':wheel['digests']['sha256']},sort_keys=True)+'\n'
if (out/'source.json').exists():assert (out/'source.json').read_text()==metadata
else:(out/'source.json').write_text(metadata)
print(metadata,end='')
