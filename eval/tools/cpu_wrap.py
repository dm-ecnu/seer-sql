"""Run a tool worker and record into argv[1]: the CPU seconds (user + sys) of it and every descendant it reaped, and
the seconds its main thread spent runnable but waiting for a CPU (run-queue delay, /proc/<pid>/schedstat; 0 where
unavailable). The wrapper is the process-group leader of the call; stdin/stdout pass through unchanged."""
import os
import resource
import subprocess
import sys

p = subprocess.Popen(sys.argv[3:])
delay = 0.0
try:
    os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)        # exited, not yet reaped: schedstat still readable
    delay = int(open(f"/proc/{p.pid}/schedstat").read().split()[1]) / 1e9
except (AttributeError, OSError, ValueError, IndexError):
    pass
rc = p.wait()
ru = resource.getrusage(resource.RUSAGE_CHILDREN)
with open(sys.argv[1], "w") as f:
    f.write(f"{ru.ru_utime + ru.ru_stime:.3f} {delay:.3f}")
sys.exit(rc if rc >= 0 else 128 - rc)
