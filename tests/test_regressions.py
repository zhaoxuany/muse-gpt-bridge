"""Regression evidence; fake adapters and streams, never real model calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from types import SimpleNamespace as NS
import time
import pytest
from fastapi.testclient import TestClient
from bridge import server as s
from bridge.adapters import AdapterError, StubAdapter
from bridge.store import Store, now
from bridge.siwc_adapter import SIWCAdapter, validate_reply

BASE='/api/muse-gpt'
AUTH={'Authorization':'Bearer '+s.BRIDGE_TOKEN}

class Counting(StubAdapter):
    calls=0
    seen=None
    fail=False
    def reply(self,**kwargs):
        self.calls+=1
        self.seen=kwargs
        time.sleep(.01)
        if self.fail:raise AdapterError('TEST simulated stream disconnect')
        reply=super().reply(**kwargs)
        reply['reply']='round-'+str(kwargs['round_no'])
        return reply
    def cost(self):
        return dict(model='actual-test-model',requested_model='requested-test-model',prompt_tokens=None,completion_tokens=None,cost_cny=0,funding='test-no-network')

@pytest.fixture
def local(tmp_path,monkeypatch):
    database=Store(tmp_path/'test.sqlite')
    fake=Counting()
    monkeypatch.setattr(s,'store',database)
    monkeypatch.setattr(s,'adapter',fake)
    s._RATE.clear()
    with TestClient(s.app) as client:
        yield client,database,fake
    s.store.close()

def body(key='create'):
    return dict(title='test',original_request='budget test',first_message='first',idempotency_key=key)

def create(client,key='create'):
    response=client.post(BASE+'/discussions',json=body(key),headers=AUTH)
    assert response.status_code==201,response.text
    return response.json()

def append(client,did,key='append',**extra):
    return client.post(BASE+'/discussions/'+did+'/messages',json=dict(role='muse',content='second',idempotency_key=key,**extra),headers=AUTH)

def test_exact_replay_after_later_rounds_and_close(local):
    c,db,fake=local
    first=create(c)
    second=append(c,first['discussion_id']).json()
    append(c,first['discussion_id'],'third')
    c.post(BASE+'/discussions/'+first['discussion_id']+'/close',json={},headers=AUTH)
    replay=c.post(BASE+'/discussions',json=body(),headers=AUTH).json()
    assert replay['gpt_reply']==first['gpt_reply'] and replay['round']==1
    replay=append(c,first['discussion_id']).json()
    assert replay['gpt_reply']==second['gpt_reply'] and replay['round']==2
    assert fake.calls==3

def test_collision_is_rejected_across_discussions_and_payload(local):
    c,db,fake=local
    a=create(c);b=create(c,'other')
    assert append(c,a['discussion_id'],'key').status_code==200
    assert append(c,b['discussion_id'],'key').status_code==409
    changed=body();changed['first_message']='different'
    assert c.post(BASE+'/discussions',json=changed,headers=AUTH).status_code==409
    assert fake.calls==3

def test_concurrent_duplicate_calls_model_only_once(local):
    c,db,fake=local
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:c.post(BASE+'/discussions',json=body(),headers=AUTH),range(4)))
    assert [r.status_code for r in results]==[201]*4
    assert len({r.json()['discussion_id'] for r in results})==1
    assert fake.calls==1

def test_failed_create_is_not_false_success_or_retried(local):
    c,db,fake=local
    fake.fail=True
    failed=c.post(BASE+'/discussions',json=body(),headers=AUTH)
    assert failed.status_code==502
    did=failed.json()['detail']['discussion_id']
    assert db.get_discussion(did)['round']==0 and db.list_messages(did)==[]
    assert c.post(BASE+'/discussions',json=body(),headers=AUTH).status_code==502
    assert fake.calls==1
    fake.fail=False
    assert append(c,did,'recover').status_code==200
    assert db.get_discussion(did)['round']==1

def test_user_answer_and_unknown_usage_survive_restart(local,monkeypatch):
    c,db,fake=local
    did=create(c)['discussion_id']
    assert append(c,did,user_answer='500元，桌宽60cm').status_code==200
    path=db.db_path
    db.close();reopened=Store(path);monkeypatch.setattr(s,'store',reopened)
    messages=reopened.list_messages(did)
    assert '500元，桌宽60cm' in messages[2]['content']
    assert messages[3]['model']=='actual-test-model' and messages[3]['prompt_tokens'] is None
    assert reopened.list_cost_records(did)[1]['completion_tokens'] is None
    append(c,did,'after-restart')
    assert '500元，桌宽60cm' in fake.seen['messages'][2]['content']

def test_inflight_operation_marked_interrupted_on_restart(local,monkeypatch):
    c,db,fake=local
    did=create(c)['discussion_id']
    request=s.AppendMessage(role='muse',content='second',idempotency_key='unfinished')
    db.begin('unfinished',did,s._fingerprint('append:'+did,request))
    path=db.db_path
    db.close();reopened=Store(path);monkeypatch.setattr(s,'store',reopened)
    r=append(c,did,'unfinished')
    assert r.status_code==409 and r.json()['detail']['code']=='interrupted'
    assert fake.calls==1 and reopened.get_discussion(did)['round']==1
    assert append(c,did,'recovered-after-review').status_code==200

def test_atomic_rollback_on_cost_write_failure(local):
    c,db,fake=local
    first=create(c);did=first['discussion_id']
    with db.transaction() as con:
        con.execute("CREATE TRIGGER force_failure BEFORE INSERT ON cost_records BEGIN SELECT RAISE(ABORT,'test'); END")
    assert append(c,did).status_code==502
    assert len(db.list_messages(did))==2
    assert len(db.list_cost_records(did))==1 and db.get_discussion(did)['round']==1
    assert db.get_operation('append')['state']=='failed'

def test_second_store_same_db_is_rejected(local):
    _,db,_=local
    with pytest.raises((OSError,BlockingIOError)):Store(db.db_path)

@pytest.mark.parametrize('field,value',[('max_rounds',0),('max_rounds',7),('budget_cap_cny',-1)])
def test_invalid_limits_rejected(local,field,value):
    c,db,fake=local
    payload=body();payload[field]=value
    assert c.post(BASE+'/discussions',json=payload,headers=AUTH).status_code==422
    assert fake.calls==0

@pytest.mark.parametrize('mutation',[{'needs_user_decision':'true'},{'questions_for_muse':[1]},{'status':'invalid'},{'extra':1}])
def test_structured_types_are_strict(mutation):
    reply=StubAdapter().reply(discussion={},messages=[],new_content='test',round_no=1)
    reply.update(mutation)
    with pytest.raises(AdapterError):validate_reply(reply)

def test_siwc_stream_requires_completion_and_records_actual_model(monkeypatch):
    from bridge import siwc_adapter as module
    events=[NS(type='response.output_text.delta',delta='{}')]
    class Stream:
        def __enter__(self):return iter(events)
        def __exit__(self,*args):pass
    class Client:
        def __init__(self,**kwargs):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def create(self,**kwargs):return Stream()
    monkeypatch.setattr(module,'OpenAI',Client)
    obj=SIWCAdapter.__new__(SIWCAdapter);obj._access_token='fake';obj._record={}
    with pytest.raises(AdapterError,match='without response.completed'):obj._post_responses({'model':'requested'})
    events.append(NS(type='response.completed',response=NS(model='actual',usage=None)))
    text,usage=obj._post_responses({'model':'requested'})
    assert obj._last_actual_model=='actual' and usage['input_tokens'] is None
