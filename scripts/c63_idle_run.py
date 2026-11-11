import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import os,sys,time,subprocess,json
from pathlib import Path
D=Path((SEER_ROOT + '/repro/dstM3-takeover-v1'))
sys.path.insert(0,str(D));import seed_queue as q
reserved=set(json.loads((D/'m33-plan.json').read_text())['cores'])
def protect_formal_cores():
 root=os.environ.get('SEER_PROTECT_CORE_ROOT')
 if not root:return set()
 protected=set();root=Path(root)
 try:active=json.loads((root.parent/'status.json').read_text())['active']
 except (OSError,ValueError,KeyError):return set(siblings)
 for item in active:
  j=item['job'];config=root/j['panel']/j['batch']/f'seer-s{j["seed"]}'/'config.json'
  try:protected.update(json.loads(config.read_text())['affinity'])
  except (OSError,ValueError,KeyError):pass
 return protected
def ints(s):
 out=[]
 for part in s.strip().split(','):
  if '-' in part:
   a,b=map(int,part.split('-'));out.extend(range(a,b+1))
  else:out.append(int(part))
 return out
siblings={min(v):set(v) for c in os.sched_getaffinity(0) for v in [ints(Path(f'/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list').read_text())]}
def snap():
 out={}
 for line in Path('/proc/stat').read_text().splitlines():
  x=line.split()
  if x[0][3:].isdigit():
   v=list(map(int,x[1:9]));out[int(x[0][3:])]=(sum(v),v[3]+v[4])
 return out
def busy():
 out=set()
 for p in Path('/proc').iterdir():
  if not p.name.isdigit() or int(p.name)==os.getpid():continue
  try:
   aff=os.sched_getaffinity(int(p.name))
   if len(aff)<=32 and (p/'cmdline').read_bytes():out |= aff
  except OSError:pass
 return out
while True:
 available=int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))
 reserve=int(os.environ.get('SEER_IDLE_MEMORY_GIB','128'))
 if available < reserve*1024**2:
  print(f'waiting for {reserve} GiB available memory',flush=True);time.sleep(15);continue
 protected=protect_formal_cores()
 a=snap();time.sleep(2);b=snap();blocked=busy()
 for core,sib in sorted(siblings.items(),reverse=bool(os.environ.get('SEER_PROTECT_CORE_ROOT'))):
  if core in reserved or core in protected or sib&blocked:continue
  if any(b[c][0]==a[c][0] or 1-(b[c][1]-a[c][1])/(b[c][0]-a[c][0])>=.1 for c in sib):continue
  lease=q.lock(D/f'core-leases/{core}')
  if lease is None:continue
  try:
   if sib&busy():continue
   print(json.dumps({'event':'leased_idle_core','core':core,'command':sys.argv[1:]}),flush=True)
   rc=subprocess.call(['taskset','-c',str(core),*sys.argv[1:]])
   sys.exit(rc)
  finally:q.unlock(lease)
 print('waiting for idle core',flush=True);time.sleep(15)
