"""Conservative admission of exact AVG through Polygon's generic FLOAT IR.

The symbolic AVG is an exact rational. PostgreSQL and MySQL return an exact
numeric result for integer/decimal arguments, but not for binary floats.  We
only use that correspondence when the AVG consumes an unmodified base column
and its result is passed through unchanged to the final output. SQL constructs
that inspect or recompute a rounded AVG remain outside this profile.
"""
from __future__ import annotations

import math

from . import ir


class NumericAdmissionError(Exception):
    pass


def _exact(ty):
    return ty == ir.INT or ir.is_dec(ty)


def _float_expr(e):
    if e is None:
        return False
    return any(x.ty == ir.FLOAT for x in ir._walk_e(e))


def _profile(root, schema):
    def visit(r):
        op = r.op
        if op == 'scan':
            if ir.FLOAT in r.types:
                raise NumericAdmissionError('binary_float_column')
            table = schema.tables[r.args[0]]
            if len(table.columns) != r.arity:
                raise NumericAdmissionError('scan_schema_arity')
            return tuple(('base_exact', ty, r.args[0], col.name, col.param)
                         if _exact(ty) else None
                         for ty, col in zip(r.types, table.columns))
        if op == 'values':
            if ir.FLOAT in r.types or any(e.ty == ir.FLOAT for e in ir.walk_exprs(r)):
                raise NumericAdmissionError('floating_value')
            return (None,) * r.arity
        if op == 'filter':
            tags = visit(r.args[0])
            if _float_expr(r.args[1]):
                raise NumericAdmissionError('rounded_value_in_predicate')
            return tags
        if op == 'project':
            child = visit(r.args[0])
            tags = []
            for e in r.args[1]:
                if e.op == 'col':
                    tags.append(child[e.args[0]])
                elif _float_expr(e):
                    raise NumericAdmissionError('floating_expression')
                else:
                    tags.append(None)
            if any(ty == ir.FLOAT and not (tag and tag[0] == 'exact_avg')
                   for ty, tag in zip(r.types, tags)):
                raise NumericAdmissionError('untraced_floating_projection')
            return tuple(tags)
        if op == 'join':
            left, right = visit(r.args[1]), visit(r.args[2])
            if any(t and t[0] == 'exact_avg' for t in left + right):
                raise NumericAdmissionError('aggregate_value_in_join')
            if _float_expr(r.args[3]):
                raise NumericAdmissionError('floating_join_predicate')
            # Semi/anti joins expose only left columns.
            return (left if r.args[0] in ('semi', 'anti') else left + right)
        if op == 'agg':
            child = visit(r.args[0])
            if any(t and t[0] == 'exact_avg' for t in child):
                raise NumericAdmissionError('nested_numeric_aggregate')
            keys, aggregates = r.args[1], r.args[2]
            if any(_float_expr(e) for e in keys):
                raise NumericAdmissionError('floating_group_key')
            tags = [None] * len(keys)
            for a in aggregates:
                if a.filt is not None and _float_expr(a.filt):
                    raise NumericAdmissionError('floating_aggregate_filter')
                if a.ty == ir.FLOAT:
                    if (a.func != 'avg' or len(a.args) != 1 or a.args[0].op != 'col'
                            or not _exact(a.args[0].ty)):
                        raise NumericAdmissionError('non_exact_average')
                    source = child[a.args[0].args[0]]
                    if source is None or source[0] != 'base_exact' or source[1] != a.args[0].ty:
                        raise NumericAdmissionError('computed_average_argument')
                    tags.append(('exact_avg', a.distinct, *source[1:]))
                elif any(_float_expr(e) for e in a.args):
                    raise NumericAdmissionError('aggregate_over_floating_value')
                else:
                    tags.append(None)
            return tuple(tags)
        if op == 'sort':
            tags = visit(r.args[0])
            if any(_float_expr(key) for key, *_ in r.args[1]):
                raise NumericAdmissionError('rounded_value_in_order')
            return tags
        if op in ('distinct', 'setop', 'window'):
            kids = [a for a in r.args if isinstance(a, ir.R)]
            child_tags = tuple(t for child in kids for t in visit(child))
            if ir.FLOAT in r.types or any(t and t[0] == 'exact_avg' for t in child_tags):
                raise NumericAdmissionError('rounded_value_in_relational_operator')
            if any(_float_expr(e) for e in ir.walk_exprs(r)):
                raise NumericAdmissionError('floating_relational_expression')
            return (None,) * r.arity
        raise NumericAdmissionError('unknown_relational_operator_' + op)

    # A scalar subquery with numeric output could expose a rounded value to an
    # outer expression without going through a simple project column.
    if any(e.op == 'sub' and (e.ty == ir.FLOAT or
                            any(ir.FLOAT in node.types for node in ir.walk_rel(e.args[1])) or
                            any(inner.ty == ir.FLOAT for inner in ir.walk_exprs(e.args[1])))
           for e in ir.walk_exprs(root)):
        raise NumericAdmissionError('numeric_scalar_subquery')
    tags = visit(root)
    if len(tags) != root.arity:
        raise NumericAdmissionError('numeric_lineage_arity')
    return tuple(tag if tag and tag[0] == 'exact_avg' else None for tag in tags)


def bounded_numeric_admission(a, b, dialect, schema=None, k=None):
    """Return a receipt fragment. An unadmitted profile never implies NEQ."""
    if a is None or b is None:
        return {'admitted': False, 'reason': 'missing_ir'}
    has_float = any(ir.FLOAT in node.types for root in (a, b) for node in ir.walk_rel(root))
    has_float |= any(e.ty == ir.FLOAT for root in (a, b) for e in ir.walk_exprs(root))
    if not has_float:
        return {'admitted': True, 'profile': 'no_float_ir_v1'}
    if dialect not in ('postgres', 'mysql'):
        return {'admitted': False, 'reason': 'exact_avg_dialect_not_supported'}
    if schema is None:
        return {'admitted': False, 'reason': 'numeric_schema_missing'}
    try:
        left, right = _profile(a, schema), _profile(b, schema)
    except (NumericAdmissionError, KeyError, IndexError) as e:
        return {'admitted': False, 'reason': str(e)}
    if left != right:
        return {'admitted': False, 'reason': 'numeric_output_lineage_mismatch',
                'left': left, 'right': right}
    if dialect == 'mysql':
        if k is None:
            return {'admitted': False, 'reason': 'numeric_row_bound_missing'}
        # MySQL's exact DECIMAL intermediate has at most 65 digits.  Keep a
        # spare digit beyond the largest possible k-row sum.
        for tag in left:
            if tag is None:
                continue
            ty, param = tag[2], tag[5]
            precision = 10 if ty == ir.INT else (param[0] if param else 18)
            if precision + math.ceil(math.log10(max(1, k)+1)) + 1 > 65:
                return {'admitted': False, 'reason': 'mysql_numeric_sum_precision'}
    return {'admitted': True, 'profile': 'direct_exact_avg_v1', 'output_lineage': left}
