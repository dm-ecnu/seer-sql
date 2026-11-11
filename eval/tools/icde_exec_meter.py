"""Run an unchanged Docker exec command and emit per-exec CPU accounting."""
import json,os,resource,subprocess,sys,time
started=time.monotonic()
p=subprocess.Popen(sys.argv[1:])
_,status,usage=os.wait4(p.pid,0)
code=os.waitstatus_to_exitcode(status)
p.returncode=code
own=resource.getrusage(resource.RUSAGE_SELF)
print('\n__ICDE_EXEC_CPU__'+json.dumps({'child_cpu_s':usage.ru_utime+usage.ru_stime,'meter_cpu_s':own.ru_utime+own.ru_stime,'wall_s':time.monotonic()-started,'exit_code':code,'cpu_complete':code==0}),flush=True)
sys.exit(code if code>=0 else 128-code)
