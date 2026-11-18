"""验收对照：每个炉次只对应一个配方版本。

- 默认口径为投料时锁定的旧配方（V1 / 投料版）；
- 显式切换后，被选中炉次统一改用新配方（V2 / 验收版），并记为“改判”；
- 同一炉次在任一时刻只有一个配方版本结论；
- 结论带配方指纹；配方定义更新后按当前定义重算，旧结论保留可追。
"""
from copy import deepcopy
from app.errors import DomainError
from app.numeric import number
from app.recipe import (
    DEFAULT_RECIPE, definition, digest, normalize_version, targets,
)

BASIS_LOCKED = '投料锁定'
BASIS_SWITCH = '切换重判'
BASIS_RESTORE = '恢复旧版'
BASIS_SWITCHED = (BASIS_SWITCH, BASIS_RESTORE)


def default_assignment(version=None):
    return {
        'version': version or DEFAULT_RECIPE,
        'basis': BASIS_LOCKED,
        'switched_from': None,
        'switched_at': None,
    }


def _normalise_item(item):
    """把 'V2' / {'version': 'V2', ...} 归一为分配对象。"""
    if isinstance(item, str):
        item = {'version': item}
    if not isinstance(item, dict):
        raise DomainError('RECIPE_ASSIGNMENT', '配方分配必须是对象')
    version = normalize_version(item.get('version'))
    basis = item.get('basis')
    if basis not in (None, BASIS_LOCKED, BASIS_SWITCH, BASIS_RESTORE):
        raise DomainError('RECIPE_ASSIGNMENT', '版本依据无效：%s' % basis)
    return {
        'version': version,
        'basis': basis or (BASIS_SWITCH if version != DEFAULT_RECIPE else BASIS_LOCKED),
        'switched_from': item.get('switched_from'),
        'switched_at': item.get('switched_at'),
    }


def normalise_assignments(intervals, assignments=None, strict=False, default=None):
    """按炉次解析唯一配方版本；同一炉次出现两个版本即视为冲突。"""
    batches = []
    for row in intervals:
        if row.get('炉次') not in batches:
            batches.append(row.get('炉次'))
    known = set(batches)
    given = {}
    if assignments:
        source = assignments.items() if isinstance(assignments, dict) else [
            ((item or {}).get('batch') or (item or {}).get('炉次'), item)
            for item in assignments
        ]
        for batch, item in source:
            if batch is None:
                raise DomainError('RECIPE_ASSIGNMENT', '配方分配缺少炉次')
            batch = str(batch)
            item = _normalise_item(item)
            if batch in given and given[batch]['version'] != item['version']:
                raise DomainError('RECIPE_CONFLICT', '同一炉次只允许一个配方版本：%s' % batch)
            given[batch] = item
    unknown = sorted(set(given) - known)
    if unknown and strict:
        raise DomainError('BATCH_NOT_FOUND', '炉次不存在：%s' % '、'.join(unknown))
    resolved = {batch: deepcopy(given.get(batch) or default_assignment(default)) for batch in batches}
    return resolved


def verdict(version, element, low, high):
    lo, hi = targets(version, element)
    if lo <= low <= high <= hi:
        return '通过', lo, hi
    if high < lo or low > hi:
        return '不合格', lo, hi
    return '待补测', lo, hi


def evaluate(intervals, assignments=None, strict=False, default=None):
    """返回 (验收明细, 炉次版本一览, 解析后的分配)；不修改入参。"""
    resolved = normalise_assignments(intervals, assignments, strict=strict, default=default)
    rows, summary = [], []
    for batch, decision in resolved.items():
        group = [r for r in intervals if r.get('炉次') == batch]
        passes = 0
        for r in group:
            low, high = number(r['含量下界_pct']), number(r['含量上界_pct'])
            status, lo, hi = verdict(decision['version'], r['元素'], low, high)
            passes += status == '通过'
            rows.append({
                **r,
                '下限_pct': str(lo),
                '上限_pct': str(hi),
                '验收': status,
                '配方版本': decision['version'],
                '口径': definition(decision['version'])['name'],
                '版本依据': decision['basis'],
                '原版本': decision.get('switched_from') or '—',
                '配方指纹': digest(decision['version']),
            })
        switched = decision['basis'] in BASIS_SWITCHED
        summary.append({
            '炉次': batch,
            '配方版本': decision['version'],
            '口径': definition(decision['version'])['name'],
            '版本依据': decision['basis'],
            '原版本': decision.get('switched_from') or '—',
            '改判': '是' if switched else '否',
            '元素数': len(group),
            '通过元素': passes,
            '配方指纹': digest(decision['version']),
        })
    return rows, summary, resolved


def _balanced(ctx):
    exceptional = ('LOT_CONFLICT', 'MOISTURE', 'REVISION')
    return ctx.get('input_count', 0) == (
        len(ctx.get('normalized', []))
        + ctx.get('duplicates', 0)
        + len([r for r in ctx.get('quarantine', []) if r.get('原因码') not in exceptional])
    )


def apply(ctx, assignments=None):
    """pipeline 阶段 6 / 独立接口：按运行指定版本出具结论，缺省为投料版。"""
    default = ctx.get('recipe_version') or DEFAULT_RECIPE
    intervals = ctx.get('intervals', [])
    rows, summary, resolved = evaluate(intervals, assignments, default=default)
    ctx['acceptance'] = rows
    ctx['batch_versions'] = summary
    ctx['assignments'] = resolved
    ctx['balanced'] = _balanced(ctx)
    versions = sorted({s['配方版本'] for s in summary})
    return {
        'rows': rows,
        'batch_versions': summary,
        'metrics': [
            ['验收通过', sum(r['验收'] == '通过' for r in rows), '项'],
            ['改判炉次', sum(s['改判'] == '是' for s in summary), '炉'],
            ['配方版本', ' / '.join(versions) or default, ''],
            ['输入守恒', '相符' if ctx['balanced'] else '差异', ''],
            ['累计隔离', len(ctx.get('quarantine', [])), '条'],
        ],
        'balanced': ctx['balanced'],
    }


# ---- 在已落库结论之上叠加切换与规则更新 --------------------------------

def _changed_batches(before, after):
    """按 (炉次, 元素) 比较验收结论，返回结论变化的炉次。"""
    old = {(r.get('炉次'), r.get('元素')): r.get('验收') for r in before}
    new = {(r.get('炉次'), r.get('元素')): r.get('验收') for r in after}
    return sorted({batch for (batch, element) in set(old) | set(new)
                   if old.get((batch, element)) != new.get((batch, element))})


def _baseline(stored, state, default):
    """上一次生效的结论：切换/恢复时留存快照优先，其次为批次内落库结论。"""
    saved = state.get('conclusions') or []
    return saved if saved else stored


def decorate(result, state=None):
    """把切换分配与当前配方定义叠加到批次结果；不修改 state。"""
    state = state or {}
    result = deepcopy(result)
    stages = result.get('stages') or {}
    stage6 = stages.get('6') or {}
    intervals = (stages.get('5') or {}).get('rows') or result.get('intervals') or []
    stored = deepcopy(stage6.get('rows') or result.get('acceptance') or [])
    previous = deepcopy(_baseline(stored, state, (result.get('manifest') or {}).get('recipe_version')))

    default = (result.get('manifest') or {}).get('recipe_version') or DEFAULT_RECIPE
    rows, summary, resolved = evaluate(intervals, state.get('assignments'), strict=False, default=default)

    # 配方定义更新：上一次结论的配方指纹与当前定义不一致即重算。
    # 无指纹的历史结论（本功能上线前落库）无法比较，不作“已更新”判断。
    stale = [r for r in previous
             if r.get('配方指纹') and r.get('配方指纹') != digest(r.get('配方版本') or default)]
    recomputed = bool(stale)

    result['acceptance'] = rows
    result['batch_versions'] = summary
    if stage6:
        stage6['rows'] = rows
        metrics = apply({
            'recipe_version': default,
            'intervals': intervals,
            'input_count': result.get('input_count', 0),
            'normalized': result.get('normalized', []),
            'duplicates': result.get('duplicates', 0),
            'quarantine': result.get('quarantine', []),
        }, state.get('assignments'))['metrics']
        stage6['metrics'] = metrics
    result['acceptance_stored'] = previous
    result['acceptance_changed'] = _changed_batches(previous, rows) if recomputed else []
    result['recomputed'] = recomputed
    result['recipe'] = {
        'default': default,
        'versions': sorted({s['配方版本'] for s in summary}),
        'switched': sorted(b for b, d in resolved.items()
                           if d['basis'] in BASIS_SWITCHED and d['version'] != default),
        'rule_updated': recomputed,
        'catalogue': [v for v in ('V1', 'V2')],
    }
    result['acceptance_history'] = deepcopy(state.get('history') or [])
    result['switch_revision'] = int(state.get('revision') or 0)
    return result


def evidence_for(intervals, normalized, raw_rows, batch):
    """已验收炉次的依据：验收结论 + 归一化成分 + 原始来料行（保留行号）。"""
    return {
        'batch': batch,
        'conclusions': [deepcopy(r) for r in intervals if r.get('炉次') == batch],
        'lots': [deepcopy(r) for r in normalized if r.get('batch') == batch],
        'raw_rows': [deepcopy(r) for r in raw_rows if r.get('batch') == batch],
    }
