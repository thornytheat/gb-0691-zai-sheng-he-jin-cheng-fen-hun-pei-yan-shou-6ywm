import sqlite3, json, os, time, hashlib
from pathlib import Path
from app.errors import DomainError
from app.recipe import normalize_version, normalize_policy, digest, DEFAULT_RECIPE
from app.modules.m6_acceptance.rules import decorate, evaluate, BASIS_SWITCH

SWITCH_TABLE = 'recipe_switches'


def connection():
    path=Path(os.environ.get('APP_DB','data/runs.sqlite3'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS '+SWITCH_TABLE+' (run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0, assignments TEXT NOT NULL DEFAULT "{}", history TEXT NOT NULL DEFAULT "[]", conclusions TEXT NOT NULL DEFAULT "[]", updated_at TEXT)')
    columns={r['name'] for r in db.execute('PRAGMA table_info('+SWITCH_TABLE+')')}
    if 'conclusions' not in columns:
        db.execute('ALTER TABLE '+SWITCH_TABLE+' ADD COLUMN conclusions TEXT NOT NULL DEFAULT "[]"')
    return db


def _row_payload(row): return {**json.loads(row['payload']),'revision':row['revision'],'decision':row['decision']}


def _empty_state():
    return {'revision':0,'assignments':{},'history':[],'conclusions':[],'updated_at':None}


def _state(db, ident):
    row=db.execute('SELECT revision,assignments,history,conclusions,updated_at FROM '+SWITCH_TABLE+' WHERE run_id=?',(ident,)).fetchone()
    if row is None: return _empty_state()
    return {'revision':row['revision'],'assignments':json.loads(row['assignments']),'history':json.loads(row['history']),
            'conclusions':json.loads(row['conclusions'] or '[]'),'updated_at':row['updated_at']}


def _write_state(db, ident, state):
    db.execute('INSERT INTO '+SWITCH_TABLE+'(run_id,revision,assignments,history,conclusions,updated_at) VALUES (?,?,?,?,?,?) '
               'ON CONFLICT(run_id) DO UPDATE SET revision=excluded.revision,assignments=excluded.assignments,history=excluded.history,conclusions=excluded.conclusions,updated_at=excluded.updated_at',
               (ident,state['revision'],json.dumps(state['assignments'],ensure_ascii=False),
                json.dumps(state['history'],ensure_ascii=False),
                json.dumps(state.get('conclusions') or [],ensure_ascii=False),state['updated_at']))


def save(result):
    with connection() as db:
        ident=result['manifest']['run_id'];row=db.execute('SELECT payload,revision,decision FROM runs WHERE id=?',(ident,)).fetchone()
        if row:
            return decorate({**_row_payload(row),'reused':True},_state(db,ident))
        db.execute('INSERT INTO runs(id,payload) VALUES (?,?)',(ident,json.dumps(result,ensure_ascii=False)))
        state=_empty_state()
    return decorate({**result,'revision':1,'decision':'待复核','reused':False},state)


def get_run(ident):
    with connection() as db:
        row=db.execute('SELECT payload,revision,decision FROM runs WHERE id=?',(ident,)).fetchone()
        if row is None: raise DomainError('NOT_FOUND','批次不存在',404)
        return decorate(_row_payload(row),_state(db,ident))


def latest():
    with connection() as db:
        row=db.execute('SELECT id,payload,revision,decision FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
        if row is None: return None
        return decorate(_row_payload(row),_state(db,row['id']))


def history():
    with connection() as db:
        runs=[dict(r) for r in db.execute('SELECT r.id,r.revision,r.decision,s.revision AS switch_revision FROM runs r LEFT JOIN '+SWITCH_TABLE+' s ON s.run_id=r.id ORDER BY r.rowid DESC')]
    return runs


def review(ident, revision, decision):
    if decision not in ('通过','退回'): raise DomainError('DECISION','复核动作无效')
    with connection() as db:
        changed=db.execute('UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?',(decision,ident,revision)).rowcount
        if not changed: raise DomainError('VERSION','版本已变化，请重新载入',409)
    return {'revision':revision+1,'decision':decision}


# ---- 配方版本切换 ----------------------------------------------------

def _batches(payload):
    return [row['炉次'] for row in ((payload.get('stages',{}).get('5') or {}).get('rows') or []) if row.get('炉次')]


def _run_intervals(payload):
    return ((payload.get('stages',{}).get('5') or {}).get('rows') or [])


def _version_of(item):
    if item is None: return None
    return normalize_version(item if isinstance(item,str) else item.get('version'))


def _run_default(payload):
    return (payload.get('manifest') or {}).get('recipe_version') or DEFAULT_RECIPE


def _conclusion_snapshot(payload, assignments):
    """某分配下的 (炉次, 元素) → 验收结论，用于识别改判。"""
    rows,_,_=evaluate(_run_intervals(payload),assignments,default=_run_default(payload))
    return [{'炉次':r['炉次'],'元素':r['元素'],'验收':r['验收'],'配方版本':r['配方版本'],
             '配方指纹':r['配方指纹']} for r in rows]


def _changed(before, after):
    """改判炉次：对照的配方版本发生变化（不只是结论字符串翻转）。"""
    old={r['炉次']:r['配方版本'] for r in before}
    new={r['炉次']:r['配方版本'] for r in after}
    return sorted(b for b in set(old)|set(new) if old.get(b)!=new.get(b))


def _verdict_changed(before, after):
    """结论翻转的 (炉次, 元素)，与改判分开统计，便于看清影响。"""
    old={(r['炉次'],r['元素']):r['验收'] for r in before}
    new={(r['炉次'],r['元素']):r['验收'] for r in after}
    return sorted({b for (b,e) in set(old)|set(new) if old.get((b,e))!=new.get((b,e))})


def _same_assignments(left, right):
    def view(mapping):
        return {b:(v.get('version') if isinstance(v,dict) else v) for b,v in (mapping or {}).items()}
    return view(left)==view(right)


def switch(ident, version=None, batches=None, policy=None, expected_revision=None, idempotency_key=None):
    """把指定炉次切到一个配方版本；失败不写库，成功整批原子生效。

    重放同一次切换不会重复改判：显式幂等键命中或分配未发生变化时直接返回。
    """
    version=normalize_version(version)
    policy=normalize_policy(policy)
    if batches is not None:
        batches=[str(b).strip() for b in batches if str(b).strip()]
        if not batches: raise DomainError('RECIPE_TARGET','请选择要切换的炉次')
        if len(set(batches))!=len(batches):
            raise DomainError('RECIPE_CONFLICT','同一炉次只允许一个配方版本')
    key=idempotency_key or hashlib.sha256(
        ('|'.join([ident,version,policy,','.join(sorted(batches or [])),str(expected_revision)]).encode('utf-8'))).hexdigest()[:16]
    with connection() as db:
        row=db.execute('SELECT payload FROM runs WHERE id=?',(ident,)).fetchone()
        if row is None: raise DomainError('NOT_FOUND','批次不存在',404)
        payload=json.loads(row['payload'])
        state=_state(db,ident)
        for entry in state['history']:
            # 仅当当前分配仍是该次切换的结果时才算重放；否则用户已改过口径，
            # 同参数的再次切换必须真正生效，不能把旧状态复活。
            if (entry.get('action')=='切换' and entry.get('idempotency_key')==key
                    and _same_assignments(state['assignments'],entry.get('after') or {})):
                return {'run_id':ident,'revision':state['revision'],'idempotent':True,
                        'changed':entry.get('changed',[]),'assignments':state['assignments'],
                        'recipe':decorate(payload,state)['recipe']}
        if expected_revision is not None and int(expected_revision)!=state['revision']:
            raise DomainError('VERSION','切换版本已变化，请重新载入',409)
        known=_batches(payload)
        if not known: raise DomainError('BATCH_NOT_FOUND','批次中没有可切换的炉次')
        if batches is None: batches=list(known)
        unknown=sorted(set(batches)-set(known))
        if unknown: raise DomainError('BATCH_NOT_FOUND','炉次不存在：%s' % '、'.join(unknown))
        previous=json.loads(json.dumps(state['assignments'],ensure_ascii=False))
        proposed=json.loads(json.dumps(previous,ensure_ascii=False))
        default=_run_default(payload)
        stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        for batch in batches:
            before=_version_of(previous.get(batch)) if batch in previous else default
            if version==default:
                # 切回投料版即撤销分配，恢复默认锁定，不残留切换标记。
                proposed.pop(batch,None)
                continue
            proposed[batch]={'version':version,'basis':BASIS_SWITCH,
                             'switched_from':before,'switched_at':stamp}
        # 同一炉次只允许一个版本结论：解析失败即整批失败，不落库。
        evaluate(_run_intervals(payload),proposed,strict=True,default=default)
        prior=_conclusion_snapshot(payload,previous)
        changed=_changed(prior,_conclusion_snapshot(payload,proposed))
        verdicts=_verdict_changed(prior,_conclusion_snapshot(payload,proposed))
        if _same_assignments(previous,proposed):
            return {'run_id':ident,'revision':state['revision'],'idempotent':True,
                    'changed':[],'assignments':state['assignments'],
                    'recipe':decorate(payload,state)['recipe']}
        event={'revision':state['revision']+1,'action':'切换','version':version,'policy':policy,
               'batches':batches,'changed':changed,'verdict_changed':verdicts,
               'before':previous,'after':proposed,'at':stamp,
               'idempotency_key':key,'conclusions':_conclusion_snapshot(payload,proposed),
               'previous_conclusions':prior}
        state['assignments']=proposed;state['revision']=event['revision']
        state['conclusions']=_conclusion_snapshot(payload,proposed)
        state['history'].append(event);state['updated_at']=event['at']
        _write_state(db,ident,state)
    result=decorate(payload,state)
    return {'run_id':ident,'revision':state['revision'],'idempotent':False,'changed':changed,
            'verdict_changed':verdicts,'assignments':state['assignments'],
            'recipe':result['recipe'],'event':event}


def restore(ident, target_revision=0, expected_revision=None, idempotency_key=None):
    """恢复到某次切换前的分配快照；旧版验收结论保留，不被覆盖。"""
    with connection() as db:
        row=db.execute('SELECT payload FROM runs WHERE id=?',(ident,)).fetchone()
        if row is None: raise DomainError('NOT_FOUND','批次不存在',404)
        payload=json.loads(row['payload'])
        state=_state(db,ident)
        target_revision=int(target_revision)
        key=idempotency_key or ('restore:%s:%d:%d' % (ident,target_revision,state['revision']))
        for entry in state['history']:
            if (entry.get('action')=='恢复' and entry.get('idempotency_key')==key
                    and _same_assignments(state['assignments'],entry.get('after') or {})):
                return {'run_id':ident,'revision':state['revision'],'idempotent':True,
                        'assignments':state['assignments'],'restored':entry.get('restored')}
        if expected_revision is not None and int(expected_revision)!=state['revision']:
            raise DomainError('VERSION','切换版本已变化，请重新载入',409)
        if target_revision<=0:
            snapshot={}
        else:
            event=next((e for e in state['history'] if e.get('revision')==target_revision and e.get('action')=='切换'),None)
            if event is None: raise DomainError('RECIPE_RESTORE','找不到可恢复的切换点',422)
            snapshot=event.get('before') or {}
        previous=state['assignments']
        default=_run_default(payload)
        evaluate(_run_intervals(payload),snapshot,strict=True,default=default)
        prior=_conclusion_snapshot(payload,previous)
        changed=_changed(prior,_conclusion_snapshot(payload,snapshot))
        verdicts=_verdict_changed(prior,_conclusion_snapshot(payload,snapshot))
        if _same_assignments(previous,snapshot):
            return {'run_id':ident,'revision':state['revision'],'idempotent':True,
                    'assignments':state['assignments'],'restored':target_revision}
        event={'revision':state['revision']+1,'action':'恢复','restored':target_revision,
               'changed':changed,'verdict_changed':verdicts,'at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
               'idempotency_key':key,'before':previous,'after':snapshot,
               'previous_conclusions':prior,'conclusions':_conclusion_snapshot(payload,snapshot)}
        state['assignments']=snapshot;state['revision']=event['revision']
        state['conclusions']=_conclusion_snapshot(payload,snapshot)
        state['history'].append(event);state['updated_at']=event['at']
        _write_state(db,ident,state)
    result=decorate(payload,state)
    return {'run_id':ident,'revision':state['revision'],'idempotent':False,
            'changed':changed,'verdict_changed':verdicts,
            'assignments':state['assignments'],'restored':target_revision,
            'recipe':result['recipe'],'event':event}


def recipe_state(ident):
    with connection() as db:
        row=db.execute('SELECT payload FROM runs WHERE id=?',(ident,)).fetchone()
        if row is None: raise DomainError('NOT_FOUND','批次不存在',404)
        state=_state(db,ident)
        result=decorate(json.loads(row['payload']),state)
    return {'run_id':ident,'revision':state['revision'],'assignments':state['assignments'],
            'history':state['history'],'batch_versions':result['batch_versions'],
            'acceptance':result['acceptance'],'acceptance_stored':result['acceptance_stored'],
            'acceptance_changed':result['acceptance_changed'],'recomputed':result['recomputed'],
            'recipe':result['recipe']}
