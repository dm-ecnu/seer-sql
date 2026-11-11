"""Native SQLSolver binding and explicit engine-semantics admission profile."""
from __future__ import annotations
import json
from pathlib import Path
import sqlglot
from sqlglot import exp
from .prover import SQLSolverRuntime, PersistentSQLSolver, sha256

def hashlib_sha(text):
    import hashlib
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


PROFILE = 'integer-relational-count-bag-v1'
_ALLOWED = {
    'Select', 'From', 'Table', 'Identifier', 'Column', 'Star', 'Where', 'Join',
    'And', 'Or', 'Not', 'Paren', 'EQ', 'NEQ', 'GT', 'GTE', 'LT', 'LTE',
    'Is', 'Null', 'Boolean', 'Literal', 'Alias', 'TableAlias', 'Distinct',
    'Group', 'Having', 'Count', 'Union', 'Except', 'Intersect', 'Subquery',
}


def semantics_profile(schema, sqls, dialect, backend):
    """A sufficient profile, not a claim to SQLSolver's entire language.

    Pure integer relational operators/count avoid numeric evaluation, collation,
    ordering and execution-error differences between the prover and DBMS.
    Every other native EQ is preserved but remains unadmitted.
    """
    if dialect != backend or backend not in ('sqlite', 'postgres', 'mysql'):
        return None
    if any(c.type != 'int' for t in schema.tables.values() for c in t.columns):
        return None
    for sql in sqls:
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except Exception:
            return None
        for node in tree.walk():
            if type(node).__name__ not in _ALLOWED:
                return None
            if isinstance(node, exp.Literal) and (node.is_string or not node.is_int):
                return None
        # SQLite's bare aggregate projection chooses an arbitrary input row.
        for sel in tree.find_all(exp.Select):
            counts = [n for n in sel.find_all(exp.Count) if n.find_ancestor(exp.Select) is sel]
            if counts:
                grouped = {e.sql() for e in (sel.args.get('group').expressions if sel.args.get('group') else [])}
                for e in sel.expressions:
                    e = e.this if isinstance(e, exp.Alias) else e
                    if not isinstance(e, exp.Count) and e.sql() not in grouped:
                        return None
    return PROFILE


class NativeSQLSolver:
    def __init__(self, runtime, schema, dialect, backend, evidence_dir):
        self.runtime = runtime
        self.schema, self.dialect, self.backend = schema, dialect, backend
        self.directory = Path(evidence_dir)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.session = PersistentSQLSolver(runtime, self.directory)
        self.calls = 0

    @classmethod
    def from_json(cls, path, **kwargs):
        raw = json.loads(Path(path).read_text())
        if 'extra_jars' in raw:
            raw['extra_jars'] = tuple(raw['extra_jars'])
        return cls(SQLSolverRuntime(**raw), **kwargs)

    def can_admit(self, a, b):
        """Whether an EQ receipt could pass the fixed semantics gate."""
        return semantics_profile(self.schema, [a, b], self.dialect, self.backend) == PROFILE

    def __call__(self, a, b, budget):
        if budget <= 0:
            return {'status': 'timeout', 'reason': 'batch_budget_before_launch'}
        self.calls += 1
        work = self.directory / f'call-{self.calls:08d}'
        result = self.session.prove(a, b, self.schema.ddl, work, budget)
        profile = semantics_profile(self.schema, [a, b], self.dialect, self.backend)
        result['admission'] = {'q1_sha256': hashlib_sha(a), 'q2_sha256': hashlib_sha(b), 'profile': profile, 'dialect': self.dialect,
                               'backend': self.backend, 'receipt_path': str(work/'receipt.json'),
                               'receipt_sha256': sha256(work/'receipt.json')}
        return result

    def close(self):
        self.session.close()

    def admit(self, a, b, receipt):
        gate = receipt.get('admission', {})
        if gate.get('profile') != PROFILE or receipt.get('status') != 'native_eq':
            return False
        path = Path(gate['receipt_path'])
        if path.parent.parent != self.directory or sha256(path) != gate['receipt_sha256']:
            return False
        native = json.loads(path.read_text())
        request = json.loads((path.parent/'request.json').read_text())
        if (sha256(path.parent/'request.json') != native['request_sha256'] or
                request['q1_sha256'] != gate['q1_sha256'] or
                request['q2_sha256'] != gate['q2_sha256']):
            return False
        return (native['status'] == 'native_eq' and native['evidence'] == receipt['evidence']
                and native['request_sha256'] == receipt['request_sha256'])
