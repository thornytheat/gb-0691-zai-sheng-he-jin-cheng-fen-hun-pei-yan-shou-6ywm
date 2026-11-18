import io

import pytest

from copy import deepcopy

from app.modules.m6_acceptance import rules as m6


def run_of(client, **kw):
    return client.post('/api/run', json=kw).get_json()


def switch(client, ident, **body):
    return client.post('/api/recipe/switch', json={'id': ident, **body})


def test_each_batch_has_exactly_one_recipe_version(client):
    run = run_of(client)
    versions = {row['炉次']: row['配方版本'] for row in run['batch_versions']}
    assert versions == {'F-1108': 'V1'}
    assert {(r['炉次'], r['配方版本']) for r in run['acceptance']} == {('F-1108', 'V1')}
    assert {r['版本依据'] for r in run['acceptance']} == {m6.BASIS_LOCKED}


def test_switch_rejudges_selected_batches_only(client, raw):
    run = run_of(client)
    ident = run['manifest']['run_id']
    response = switch(client, ident, version='V2', batches=['F-1108'], expected_revision=0)
    assert response.status_code == 200
    body = response.get_json()
    assert body['idempotent'] is False
    assert body['changed'] == ['F-1108']
    state = client.get('/api/runs/%s/recipe' % ident).get_json()
    assert {r['配方版本'] for r in state['acceptance']} == {'V2'}
    assert {r['版本依据'] for r in state['acceptance']} == {m6.BASIS_SWITCH}
    assert {r['原版本'] for r in state['acceptance']} == {'V1'}
    summary = state['batch_versions'][0]
    assert summary['口径'] == '验收版' and summary['改判'] == '是'
    # 复核/历史记录不因切换被改写
    assert client.get('/api/runs/' + ident).get_json()['decision'] == '待复核'


def test_switch_is_idempotent_under_replay(client):
    ident = run_of(client)['manifest']['run_id']
    first = switch(client, ident, version='V2', batches=['F-1108']).get_json()
    again = switch(client, ident, version='V2', batches=['F-1108']).get_json()
    assert first['revision'] == 1 and again['revision'] == 1
    assert again['idempotent'] is True
    # 重放返回首次的改判清单，但不会新增历史、不会再次改判
    assert again['changed'] == first['changed'] == ['F-1108']
    state = client.get('/api/runs/%s/recipe' % ident).get_json()
    assert len(state['history']) == 1


def test_stale_switch_revision_is_rejected(client):
    ident = run_of(client)['manifest']['run_id']
    assert switch(client, ident, version='V2', expected_revision=0).status_code == 200
    assert switch(client, ident, version='V1', expected_revision=0).status_code == 409


def test_unknown_or_duplicate_batch_rejected_without_mutation(client):
    ident = run_of(client)['manifest']['run_id']
    assert switch(client, ident, version='V2', batches=['F-X']).status_code == 422
    assert switch(client, ident, version='V2', batches=['F-1108', 'F-1108']).status_code == 422
    assert switch(client, ident, version='V9').status_code == 422
    state = client.get('/api/runs/%s/recipe' % ident).get_json()
    assert state['revision'] == 0 and state['acceptance'][0]['配方版本'] == 'V1'


def test_one_version_per_batch_in_conflicting_assignment():
    intervals = [{'炉次': 'F1', '元素': 'Cu', '含量下界_pct': '9', '含量上界_pct': '10'}]
    with pytest.raises(Exception):
        m6.evaluate(intervals, [{'batch': 'F1', 'version': 'V1'}, {'batch': 'F1', 'version': 'V2'}])


def test_restore_brings_back_old_version_and_keeps_history(client):
    ident = run_of(client)['manifest']['run_id']
    switch(client, ident, version='V2', batches=['F-1108'])
    response = client.post('/api/recipe/restore', json={'id': ident, 'target_revision': 1})
    assert response.status_code == 200
    state = client.get('/api/runs/%s/recipe' % ident).get_json()
    assert {r['配方版本'] for r in state['acceptance']} == {'V1'}
    assert state['revision'] == 2
    assert [e['action'] for e in state['history']] == ['切换', '恢复']
    # 恢复后旧版结论可追回（切换事件里保存了当时的结论快照）
    assert state['history'][0]['conclusions']


def test_restore_to_unknown_point_is_rejected(client):
    ident = run_of(client)['manifest']['run_id']
    assert client.post('/api/recipe/restore', json={'id': ident, 'target_revision': 9}).status_code == 422


def test_old_conclusions_preserved_when_recipe_rules_update(client, monkeypatch):
    import app.recipe as recipe
    ident = run_of(client)['manifest']['run_id']
    switch(client, ident, version='V2', batches=['F-1108'])
    before = client.get('/api/runs/%s/recipe' % ident).get_json()['acceptance']
    tightened = deepcopy(recipe.RECIPES['V2']['targets'])
    tightened['Cu'] = ('0', '5')
    monkeypatch.setitem(recipe.RECIPES['V2'], 'targets', tightened)
    state = client.get('/api/runs/%s/recipe' % ident).get_json()
    assert state['recomputed'] is True and state['acceptance_changed'] == ['F-1108']
    assert next(r for r in state['acceptance'] if r['元素'] == 'Cu')['验收'] == '不合格'
    # 旧结论原样保留，不因重算丢失
    assert [(r['元素'], r['验收']) for r in state['acceptance_stored']] == \
           [(r['元素'], r['验收']) for r in before]
    assert state['recipe']['rule_updated'] is True


def test_evidence_traces_back_to_raw_rows(client):
    ident = run_of(client)['manifest']['run_id']
    evidence = client.get('/api/runs/%s/evidence?batch=F-1108' % ident).get_json()
    assert evidence['batch'] == 'F-1108'
    assert len(evidence['raw_rows']) == 50
    assert all('行号' in row for row in evidence['raw_rows'])
    assert evidence['conclusions'] and all(r['配方版本'] == 'V1' for r in evidence['conclusions'])
    assert len(evidence['lots']) == 46
    assert client.get('/api/runs/%s/evidence' % ident).status_code == 422


def test_recipe_catalogue_exposes_both_versions(client):
    body = client.get('/api/recipe').get_json()
    assert [v['version'] for v in body['versions']] == ['V1', 'V2']
    assert body['default'] == 'V1'
    assert all(v['digest'] for v in body['versions'])


def test_switch_rejects_unknown_policy(client):
    ident = run_of(client)['manifest']['run_id']
    assert switch(client, ident, version='V2', policy='随便').status_code == 422


def test_acceptance_page_shows_version_and_rejudged(client):
    ident = run_of(client)['manifest']['run_id']
    switch(client, ident, version='V2', batches=['F-1108'])
    html = client.get('/work/6?run_id=' + ident).data.decode()
    assert '配方版本' in html and '版本依据' in html and '切换重判' in html
    assert '原版本' in html and '>V1<' in html and '>V2<' in html
    assert '已验收依据与原始来料' in html and '配方版本切换' in html


def test_switch_back_to_locked_version_clears_assignment(client):
    ident = run_of(client)['manifest']['run_id']
    switch(client, ident, version='V2')
    back = switch(client, ident, version='V1').get_json()
    assert back['changed'] == ['F-1108']
    state = client.get('/api/runs/%s/recipe' % ident).get_json()
    assert state['assignments'] == {}
    assert {(r['配方版本'], r['版本依据']) for r in state['acceptance']} == {('V1', m6.BASIS_LOCKED)}


def test_evidence_csv_carries_recipe_columns(client):
    ident = run_of(client)['manifest']['run_id']
    switch(client, ident, version='V2')
    head = client.get('/api/evidence.csv?run_id=' + ident).data.decode('utf-8-sig').splitlines()[0]
    assert '配方版本' in head and '版本依据' in head and '配方指纹' in head


def test_failed_switch_is_atomic(client):
    ident = run_of(client)['manifest']['run_id']
    response = switch(client, ident, version='V2', batches=['F-1108', 'F-NOPE'])
    assert response.status_code == 422
    state = client.get('/api/runs/%s/recipe' % ident).get_json()
    assert state['revision'] == 0 and state['assignments'] == {}
    assert state['history'] == []


def test_replaying_old_switch_after_change_takes_effect(client):
    ident = run_of(client)['manifest']['run_id']
    switch(client, ident, version='V2')          # r1: V1 -> V2
    switch(client, ident, version='V1')          # r2: back to locked
    replay = switch(client, ident, version='V2').get_json()
    assert replay['idempotent'] is False
    assert replay['revision'] == 3
    state = client.get('/api/runs/%s/recipe' % ident).get_json()
    assert {r['配方版本'] for r in state['acceptance']} == {'V2'}
