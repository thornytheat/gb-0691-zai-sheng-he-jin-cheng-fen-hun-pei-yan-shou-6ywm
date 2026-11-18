from flask import Flask, request, jsonify, Response
import csv,io
from app.errors import DomainError
from app.reader import decode
from app.pipeline import calculate
from app.store import save, latest, history, review, get_run, switch, restore, recipe_state
from app.web import bp as web
from app.recipe import catalogue, policy_version
from app.modules.m6_acceptance.rules import evidence_for

from app.modules.m1_receiving.handler import bp as bp1
from app.modules.m2_assay.handler import bp as bp2
from app.modules.m3_recipe.handler import bp as bp3
from app.modules.m4_blend.handler import bp as bp4
from app.modules.m5_uncertainty.handler import bp as bp5
from app.modules.m6_acceptance.handler import bp as bp6
from app.modules.m7_reblend.handler import bp as bp7
app=Flask(__name__)
app.config['MAX_CONTENT_LENGTH']=5*1024*1024
app.json.ensure_ascii=False
@app.template_filter('display')
def display(value):
    import re
    from decimal import Decimal
    text=str(value)
    if re.fullmatch(r'-?[0-9]+\.[0-9]+',text):
        return format(Decimal(text),'.4f').rstrip('0').rstrip('.')
    return text
app.register_blueprint(web)
app.register_blueprint(bp1)
app.register_blueprint(bp2)
app.register_blueprint(bp3)
app.register_blueprint(bp4)
app.register_blueprint(bp5)
app.register_blueprint(bp6)
app.register_blueprint(bp7)
@app.before_request
def validate_request_object():
    if request.method!='POST': return None
    if request.files:
        if request.path!='/api/run' or 'file' not in request.files:
            raise DomainError('REQUEST','上传必须使用file字段',400)
        return None
    if not request.is_json:
        raise DomainError('REQUEST','请求须为JSON对象或CSV文件',400)
    if not isinstance(request.get_json(silent=True),dict):
        raise DomainError('REQUEST','请求内容必须为JSON对象',400)
@app.errorhandler(DomainError)
def domain_error(error): return jsonify({'code':error.code,'message':error.message}),error.status
@app.errorhandler(413)
def too_big(error): return jsonify({'code':'LIMIT','message':'文件超过5MB'}),413
@app.route('/health')
def health(): return {'status':'ok'}
@app.route('/api/run',methods=['POST'])
def run():
    if request.files:
        raw=request.files['file'].read();text=decode(raw)
    else:
        payload=request.get_json(silent=True) or {};text=payload.get('csv')
    return jsonify(save(calculate(text)))
@app.route('/api/runs/<ident>')
def run_detail(ident): return jsonify(get_run(ident))
@app.route('/api/history')
def runs(): return jsonify(history())
@app.route('/api/review',methods=['POST'])
def decide():
    body=request.get_json(silent=True) or {}
    try: version=int(body.get('revision',0))
    except (ValueError,TypeError): raise DomainError('VERSION','版本必须为整数')
    return jsonify(review(body.get('id'),version,body.get('decision')))
@app.route('/api/recipe')
def recipe_catalogue():
    from app.config import RULE_VERSION
    return jsonify({'rule_version':RULE_VERSION,'default':'V1','versions':catalogue()})


def _switch_body():
    body=request.get_json(silent=True)
    ident=request.args.get('run_id') or (body or {}).get('id') or (body or {}).get('run_id')
    if not ident: raise DomainError('REQUEST','缺少批次id')
    return ident,body or {}


@app.route('/api/recipe/switch',methods=['POST'])
def recipe_switch():
    ident,body=_switch_body()
    batches=body.get('batches') or body.get('炉次')
    if isinstance(batches,str): batches=[x for x in batches.replace('，',',').split(',') if x.strip()]
    expected=body.get('expected_revision',body.get('switch_revision'))
    policy=body.get('policy') or body.get('口径')
    version=body.get('version') or body.get('配方版本') or (policy_version(policy) if policy else None)
    return jsonify(switch(ident,version=version,batches=batches,
                          policy=policy,expected_revision=expected,
                          idempotency_key=body.get('idempotency_key')))


@app.route('/api/recipe/restore',methods=['POST'])
def recipe_restore():
    ident,body=_switch_body()
    expected=body.get('expected_revision',body.get('switch_revision'))
    return jsonify(restore(ident,target_revision=body.get('target_revision',body.get('恢复点',0)),
                           expected_revision=expected,idempotency_key=body.get('idempotency_key')))


@app.route('/api/recipe/state')
@app.route('/api/runs/<ident>/recipe')
def recipe_run_state(ident=None):
    return jsonify(recipe_state(ident or request.args['run_id']))


@app.route('/api/runs/<ident>/evidence')
def run_evidence(ident):
    batch=request.args.get('batch')
    if not batch: raise DomainError('REQUEST','缺少炉次')
    result=get_run(ident)
    return jsonify(evidence_for(result.get('acceptance',[]),result.get('normalized',[]),
                                ((result.get('stages') or {}).get('1') or {}).get('rows') or [],batch))


@app.route('/api/evidence.csv')
def evidence():
    result=get_run(request.args['run_id']) if request.args.get('run_id') else (latest() or save(calculate()));out=io.StringIO(newline='')
    rows=[]
    for index,stage in result['stages'].items():
        rows.extend({'阶段':index,**r} for r in stage['rows'])
    rows.extend({'阶段':'隔离',**r} for r in result['quarantine'])
    fields=list(dict.fromkeys(['阶段','run_id','规则版本']+[k for r in rows for k in r]))
    writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader()
    for row in rows: writer.writerow({'run_id':result['manifest']['run_id'],'规则版本':result['manifest']['rule_version'],**row})
    return Response('\ufeff'+out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=evidence.csv'})
if __name__=='__main__': app.run(host='0.0.0.0',port=8000)
