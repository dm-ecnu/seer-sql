"""JSON transport around the unchanged official v0.0.7 BIRD pair function."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import dataclasses, hashlib, importlib.util, json, os, sys
from pathlib import Path

R=Path((SEER_ROOT))
OBSERVER=R/'parseval007-repro-wt/eval/tools'
sys.path.insert(0,str(OBSERVER))
from parseval007_snapshot import ROOT,COMMIT,verify_native,observe,metadata,generated_witness
SOURCE=ROOT/'scripts/exp_sqlite_disprover.py'
DRIVER_SHA='9e2c07cc87292392efeb3f645467684f6ef8cd643f1cf76ae3aef21f0a4e3277'

def main():
    # Native stdout remains in its evidence log, never in the JSON channel.
    protocol=os.fdopen(os.dup(1),'w',buffering=1)
    os.dup2(2,1)
    # Match release007-bird-snapshot-full's actual allocator/thread settings.
    # All native threads inherit the single leased CPU affinity.
    os.environ['RAYON_NUM_THREADS']='8'
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest()==DRIVER_SHA, 'official driver changed'
    native=verify_native()
    from parseval.generator import GenerationConfig
    spec=importlib.util.spec_from_file_location('original_bird_driver',SOURCE)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    provenance=dict(upstream_commit=COMMIT,official_driver_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        generation_defaults=dataclasses.asdict(GenerationConfig()),native_pair_timeout_s=360,
        affinity=sorted(os.sched_getaffinity(0)),
        allocator_environment={k:os.environ.get(k) for k in
            ('MALLOC_ARENA_MAX','ARROW_DEFAULT_MEMORY_POOL','OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','RAYON_NUM_THREADS')})
    for line in sys.stdin:
        req=json.loads(line)
        # Native defaults and bag semantics are exactly those of the existing
        # official panel. Only batch scheduling/transport is external.
        uri=f'file:c69_parseval_{os.getpid()}_{req["index"]}?mode=memory&cache=shared'
        with observe(native,uri) as state:
            record=module._process_disprove_case(req['index'],req['q1'],req['q2'],
                req['batch'],req['ddl'],f'sqlite:///{uri}&uri=true',360,uri)
        record.update(metadata(state),**provenance)
        if record['verdict']=='neq':record.update(generated_witness(state))
        protocol.write(json.dumps(record,default=str)+'\n');protocol.flush()

if __name__=='__main__':main()
