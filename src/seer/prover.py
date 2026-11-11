"""Evidence-bearing I/O to the unchanged official SQLSolver Java API.

An EQ here is a native prover receipt. Admission as a v2 proved relation also
requires the caller's SQL/dialect/comparison-contract compatibility gate.
NEQ is not a distinguishing certificate and never becomes a refutation here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import resource
import signal
from pathlib import Path
import subprocess
import time


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path, value):
    with Path(path).open('x') as f:
        json.dump(value, f, sort_keys=True, indent=2)
        f.write('\n')


@dataclass(frozen=True)
class SQLSolverRuntime:
    java: str
    jar: str
    classes: str
    library_dir: str
    properties: str
    artifacts: dict[str, str]
    source_commit: str
    timeout_bin: str = '/usr/bin/timeout'
    extra_jars: tuple[str, ...] = ()

    def verify(self):
        required = {str(Path(self.java).resolve()), self.jar, self.properties,
                    str(Path(self.classes) / 'SeerSQLSolver.class'),
                    str(Path(self.library_dir) / 'libz3.so'),
                    str(Path(self.library_dir) / 'libz3java.so'), self.timeout_bin}
        if any(not Path(p).is_absolute() or Path(p).suffix != '.jar' for p in self.extra_jars):
            raise ValueError('extra runtime jars require absolute jar paths')
        required.update(self.extra_jars)
        if not required.issubset(self.artifacts):
            raise ValueError('runtime manifest omits required artifacts')
        for path, expected in self.artifacts.items():
            if sha256(path) != expected:
                raise ValueError('prover runtime artifact changed: ' + path)


def native_result(returncode, output, timed_out=False):
    if timed_out or returncode in (124, 137):
        return {'status': 'timeout', 'native_verdict': None}
    if returncode != 0:
        return {'status': 'runtime_error', 'native_verdict': None}
    answer = output.strip()
    if answer not in {'EQ', 'NEQ', 'UNKNOWN', 'TIMEOUT'}:
        return {'status': 'protocol_error', 'native_verdict': None}
    return {'status': {'EQ': 'native_eq', 'NEQ': 'not_proved',
                       'UNKNOWN': 'not_proved', 'TIMEOUT': 'timeout'}[answer],
            'native_verdict': answer}


def prove(runtime: SQLSolverRuntime, q1: str, q2: str, ddl: str,
          workspace, timeout_s: float):
    """One native call, bounded by the caller's remaining wall budget.

    The dedicated workspace is never reused; stdout/stderr, native answer and
    byte-identical inputs remain inspectable even after a deadline/error.
    GNU timeout owns the whole Java subprocess group, including native Z3.
    """
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError('positive finite remaining budget required')
    if any(not isinstance(x, str) or '\x00' in x for x in (q1, q2, ddl)):
        raise ValueError('SQL and DDL must be text without NUL')
    started = time.monotonic()
    runtime.verify()
    work = Path(workspace).resolve()
    work.mkdir(parents=True, exist_ok=False)
    for name, value in [('q1.sql', q1), ('q2.sql', q2), ('schema.sql', ddl)]:
        (work / name).write_bytes(value.encode('utf-8'))
    (work / 'sqlsolver.properties').write_bytes(Path(runtime.properties).read_bytes())
    request = {'q1_sha256': sha256(work/'q1.sql'), 'q2_sha256': sha256(work/'q2.sql'),
               'ddl_sha256': sha256(work/'schema.sql'), 'timeout_s': timeout_s,
               'source_commit': runtime.source_commit, 'artifacts': runtime.artifacts,
               'affinity': sorted(os.sched_getaffinity(0)),
               'implementation_sha256': sha256(__file__),
               'native_classpath': [runtime.classes, runtime.jar, *runtime.extra_jars],
               'scope': 'official_native_result_pending_semantic_admission'}
    _write_json(work/'request.json', request)
    remaining = timeout_s - (time.monotonic()-started)
    command = [runtime.timeout_bin, '--signal=TERM', '--kill-after=0.1s',
               f'{max(remaining, 1e-9):.9f}s', runtime.java, '-cp',
               os.pathsep.join([runtime.classes, runtime.jar, *runtime.extra_jars]), 'SeerSQLSolver',
               str(work/'q1.sql'), str(work/'q2.sql'), str(work/'schema.sql'),
               str(work/'answer.txt')]
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = runtime.library_dir
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    execution_started = time.monotonic()
    try:
        if remaining <= 0:
            result = {'status': 'timeout', 'native_verdict': None, 'returncode': None,
                      'reason': 'budget_exhausted_before_native_launch'}
        else:
            with (work/'stdout.log').open('xb') as out, (work/'stderr.log').open('xb') as err:
                completed = subprocess.run(command, cwd=work, env=env, stdout=out, stderr=err)
            answer = (work/'answer.txt').read_text() if (work/'answer.txt').exists() else ''
            elapsed = time.monotonic()-execution_started
            deadline_kill = completed.returncode == -9 and elapsed >= remaining
            result = native_result(completed.returncode, answer, timed_out=deadline_kill)
            result['returncode'] = completed.returncode
    except OSError as e:
        result = {'status': 'runtime_error', 'native_verdict': None,
                  'returncode': None, 'error_type': type(e).__name__}
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    result.update(host_child_cpu_s=(child_after.ru_utime + child_after.ru_stime
                                  - child_before.ru_utime - child_before.ru_stime),
                  cpu_scope='sequential_call_waited_child_tree',
                  wall_s=time.monotonic()-started, request_sha256=sha256(work/'request.json'),
                  evidence={p.name: sha256(p) for p in work.iterdir() if p.is_file()},
                  proof_admitted=False)
    _write_json(work/'receipt.json', result)
    return result


class PersistentSQLSolver:
    """Keep the official Verification API in one JVM across a stream batch."""

    def __init__(self, runtime: SQLSolverRuntime, evidence_dir):
        self.runtime = runtime
        self.root = Path(evidence_dir) / '_persistent_runtime'
        self.process = None
        self.stdout = self.stderr = None
        self.starts = 0

    @staticmethod
    def _cpu(pid):
        try:
            stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            return (int(stat[11]) + int(stat[12])) / os.sysconf('SC_CLK_TCK')
        except (OSError, ValueError, IndexError):
            return 0.0

    def _start(self, timeout_s):
        self.runtime.verify()
        self.root.mkdir(parents=True, exist_ok=True)
        classes = self.root / 'classes'
        classes.mkdir(exist_ok=True)
        source = Path(__file__).resolve().parents[2] / 'scripts/java/SeerSQLSolverPersistent.java'
        javac = str(Path(self.runtime.java).with_name('javac'))
        subprocess.run([javac, '-cp', self.runtime.jar, '-d', str(classes), str(source)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=max(0.001, timeout_s))
        (self.root / 'sqlsolver.properties').write_bytes(Path(self.runtime.properties).read_bytes())
        classpath = os.pathsep.join([str(classes), self.runtime.classes,
                                     self.runtime.jar, *self.runtime.extra_jars])
        env = dict(os.environ, LD_LIBRARY_PATH=self.runtime.library_dir)
        self.stdout = (self.root / 'stdout.log').open('ab')
        self.stderr = (self.root / 'stderr.log').open('ab')
        self.process = subprocess.Popen([self.runtime.java, '-cp', classpath,
                                         'SeerSQLSolverPersistent'], cwd=self.root, env=env,
                                        stdin=subprocess.PIPE, stdout=self.stdout,
                                        stderr=self.stderr, start_new_session=True)
        self.starts += 1

    def close(self):
        proc = self.process
        self.process = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)
            except ProcessLookupError:
                pass
            finally:
                if proc.stdin is not None:
                    proc.stdin.close()
        for name in ('stdout', 'stderr'):
            stream = getattr(self, name)
            if stream is not None:
                stream.close()
                setattr(self, name, None)

    def prove(self, q1, q2, ddl, workspace, timeout_s):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('positive finite remaining budget required')
        if any(not isinstance(x, str) or '\x00' in x for x in (q1, q2, ddl)):
            raise ValueError('SQL and DDL must be text without NUL')
        started = time.monotonic()
        work = Path(workspace).resolve()
        work.mkdir(parents=True, exist_ok=False)
        for name, value in [('q1.sql', q1), ('q2.sql', q2), ('schema.sql', ddl)]:
            (work / name).write_bytes(value.encode('utf-8'))
        request = {'q1_sha256': sha256(work/'q1.sql'), 'q2_sha256': sha256(work/'q2.sql'),
                   'ddl_sha256': sha256(work/'schema.sql'), 'timeout_s': timeout_s,
                   'source_commit': self.runtime.source_commit, 'artifacts': self.runtime.artifacts,
                   'affinity': sorted(os.sched_getaffinity(0)),
                   'implementation_sha256': sha256(__file__),
                   'transport_source_sha256': sha256(Path(__file__).resolve().parents[2]
                                                      / 'scripts/java/SeerSQLSolverPersistent.java'),
                   'scope': 'official_native_result_pending_semantic_admission'}
        _write_json(work/'request.json', request)
        result = None
        cpu_before = self._cpu(self.process.pid) if self.process is not None else 0.0
        try:
            if self.process is None or self.process.poll() is not None:
                self.close()
                self._start(max(0.001, timeout_s - (time.monotonic() - started)))
                cpu_before = self._cpu(self.process.pid)
            if time.monotonic() - started >= timeout_s:
                result = {'status': 'timeout', 'native_verdict': None,
                          'reason': 'budget_exhausted_before_native_call'}
            else:
                paths = [str(work/name) for name in ('q1.sql', 'q2.sql', 'schema.sql', 'answer.txt')]
                if any('\t' in path or '\n' in path for path in paths):
                    raise ValueError('native file path contains protocol delimiter')
                self.process.stdin.write(('\t'.join(paths) + '\n').encode())
                self.process.stdin.flush()
                answer = work / 'answer.txt'
                while not answer.exists() and self.process.poll() is None:
                    if time.monotonic() - started >= timeout_s:
                        result = {'status': 'timeout', 'native_verdict': None,
                                  'reason': 'persistent_jvm_deadline'}
                        break
                    time.sleep(0.01)
                if result is None:
                    if answer.exists():
                        value = answer.read_text()
                        result = ({'status': 'runtime_error', 'native_verdict': None}
                                  if value.strip() == 'ERROR' else native_result(0, value))
                    else:
                        result = {'status': 'runtime_error', 'native_verdict': None,
                                  'reason': 'persistent_jvm_exited'}
        except subprocess.TimeoutExpired:
            result = {'status': 'timeout', 'native_verdict': None,
                      'reason': 'persistent_jvm_startup_deadline'}
        except (OSError, subprocess.CalledProcessError) as exc:
            result = {'status': 'runtime_error', 'native_verdict': None,
                      'reason': type(exc).__name__}
        cpu_after = self._cpu(self.process.pid) if self.process is not None else cpu_before
        if result['status'] in ('timeout', 'runtime_error', 'protocol_error'):
            self.close()
        result.update(host_child_cpu_s=max(0.0, cpu_after - cpu_before),
                      cpu_scope='persistent_jvm_proc_stat_delta',
                      wall_s=time.monotonic()-started,
                      request_sha256=sha256(work/'request.json'),
                      evidence={p.name: sha256(p) for p in work.iterdir() if p.is_file()},
                      proof_admitted=False)
        _write_json(work/'receipt.json', result)
        return result
