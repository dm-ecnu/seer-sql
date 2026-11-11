"""Apply an authorized resource limit to unchanged matrix group execution."""
import sys,time
from pathlib import Path
import matrix as m

class ResourceSubprocess:
    def __init__(self,delegate,hard,original):
        self.delegate=delegate;self.hard=hard;self.original=original;self.calls=0
    def __getattr__(self,name):return getattr(self.delegate,name)
    def call(self,args,*a,**kw):
        if args and args[0]=='prlimit':
            assert args[1]==f'--as={self.original}'
            args=[args[0],f'--as={self.hard}',*args[2:]]
            self.calls+=1
        return self.delegate.call(args,*a,**kw)

def run(root,index,policy_path):
    policy=m.read(policy_path);plan=m.read(root/'config.json');job=plan['groups'][index]
    assert plan['stage']=='bird'
    assert policy['source_commit']==plan['source_commit']
    assert m.sha(m.__file__)==policy['matrix_sha256']
    assert m.sha(__file__)==policy['resource_wrapper_sha256']
    hard=policy['bird_hard_bytes']
    cg=next(x.split('::',1)[1]for x in Path('/proc/self/cgroup').read_text().splitlines()if x.startswith('0::'))
    assert int((Path('/sys/fs/cgroup')/cg.lstrip('/')/'memory.max').read_text())==hard
    path=root/'resources'/f'{index}.json';assert not path.exists()
    record=dict(index=index,started=time.time(),policy=str(policy_path),policy_sha256=m.sha(policy_path),
        planned_hard_bytes=job['hard_bytes'],effective_hard_bytes=hard,source_commit=plan['source_commit'],
        note='Resource override only: MemoryMax and per-method prlimit --as. Original matrix/configs retained.')
    m.write(path,record)
    proxy=ResourceSubprocess(m.subprocess,hard,job['hard_bytes']);m.subprocess=proxy
    m.group(root,index)
    record.update(finished=time.time(),prlimit_calls=proxy.calls)
    assert proxy.calls>=len(job['order'])
    m.write(path,record)

if __name__=='__main__':run(Path(sys.argv[1]),int(sys.argv[2]),Path(sys.argv[3]))
