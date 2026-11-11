"""Read-only /proc RSS sampling of every process in the live BIRD scopes."""
import json,math,os,sys,time
from pathlib import Path

def sample(duration):
 root=Path('/sys/fs/cgroup/user.slice')/f'user-{os.getuid()}.slice'/f'user@{os.getuid()}.service/app.slice'
 start=time.time();groups={};samples=0
 while time.time()-start<duration:
  for cg in root.glob('c69-prod-bird-g*.scope'):
   name=cg.name;rec=groups.setdefault(name,dict(peak_tree_rss_bytes=0,peak_tree_hwm_bytes=0,cgroup_memory_peak_bytes=0,methods_seen=[],samples=0,max_processes=0))
   rss=hwm=0;count=0;seen=set(rec['methods_seen'])
   for proc in cg.rglob('cgroup.procs'):
    try:pids=proc.read_text().split()
    except OSError:continue
    for pid in pids:
     try:
      p=Path('/proc')/pid
      info=dict(x.split(':',1) for x in (p/'status').read_text().splitlines() if ':'in x)
      rss+=int(info.get('VmRSS','0 kB').split()[0])*1024
      hwm+=int(info.get('VmHWM','0 kB').split()[0])*1024
      cmd=(p/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace');count+=1
      if 'parseval007_pool_worker' in cmd:seen.add('parseval')
      if '--method sqlwitness' in cmd:seen.add('sqlwitness')
      if '--method seer_stream_sqlwitness' in cmd:seen.add('seer_stream_sqlwitness')
     except (OSError,ValueError):pass
   try:mem_peak=int((cg/'memory.peak').read_text())
   except (OSError,ValueError):mem_peak=0
   rec.update(peak_tree_rss_bytes=max(rec['peak_tree_rss_bytes'],rss),peak_tree_hwm_bytes=max(rec['peak_tree_hwm_bytes'],hwm),
    cgroup_memory_peak_bytes=max(rec['cgroup_memory_peak_bytes'],mem_peak),methods_seen=sorted(seen),samples=rec['samples']+1,max_processes=max(rec['max_processes'],count))
  samples+=1;time.sleep(1)
 vals=sorted(x['peak_tree_rss_bytes'] for x in groups.values() if x['peak_tree_rss_bytes'])
 print(json.dumps(dict(started=start,finished=time.time(),sampling_interval_s=1,samples=samples,groups=groups,
  sampled_groups=len(vals),tree_rss_p99_bytes=vals[math.ceil(.99*len(vals))-1],tree_rss_max_bytes=max(vals),
  scope='Sum VmRSS over cgroup processes and descendants; shared pages may be counted twice. No method executions added.'),indent=2),flush=True)
if __name__=='__main__':sample(float(sys.argv[1]))
