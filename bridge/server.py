"""Loopback-only bridge: one worker, serialized calls, durable idempotency."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from bridge.adapters import AdapterError,get_adapter
from bridge.siwc_adapter import validate_reply
from bridge.store import Store,now

VERSION='0.1.0-windows-local-r1'
SERVICE='muse-gpt-bridge'
BRIDGE_TOKEN=os.environ.get('BRIDGE_TOKEN','')
if len(BRIDGE_TOKEN)<32:raise SystemExit('BRIDGE_TOKEN must contain at least 32 characters')
store=Store(os.environ.get('BRIDGE_DB',str(Path(__file__).resolve().parent.parent/'data'/'bridge.sqlite')))
adapter=get_adapter()
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
_SERIAL=threading.RLock()
_RATE_LOCK=threading.Lock()
_RATE={}

def _rate_limited(key,limit,window=60):
    with _RATE_LOCK:
        t=time.monotonic()
        hits=[x for x in _RATE.get(key,[]) if t-x<window]
        _RATE[key]=hits
        if len(hits)>=limit:return True
        hits.append(t)
        return False

@app.middleware('http')
async def auth_and_log(request:Request,call_next):
    if request.url.path!='/api/muse-gpt/health':
        auth=request.headers.get('authorization','')
        token=auth[7:] if auth.startswith('Bearer ') else ''
        if not token or not hmac.compare_digest(token,BRIDGE_TOKEN):
            return JSONResponse({'detail':'unauthorized','code':'unauthorized'},status_code=401)
        if _rate_limited('token',30):
            return JSONResponse({'detail':'rate limited','code':'rate_limited'},status_code=429,headers={'Retry-After':'60'})
    start=time.monotonic()
    response=await call_next(request)
    print(f'{request.method} {request.url.path} {response.status_code} {int((time.monotonic()-start)*1000)}ms',flush=True)
    return response

class CreateDiscussion(BaseModel):
    title:str=Field(min_length=1,max_length=500)
    original_request:str=Field(min_length=1,max_length=20000)
    user_context:str=Field(default='',max_length=20000)
    project_goal:str=Field(default='',max_length=20000)
    first_message:str=Field(min_length=1,max_length=20000)
    idempotency_key:str=Field(min_length=1,max_length=200)
    max_rounds:int=Field(default=6,ge=1,le=6)
    budget_cap_cny:float|None=Field(default=2,ge=0,allow_inf_nan=False)

class AppendMessage(BaseModel):
    role:str=Field(pattern='^(muse|user)$')
    content:str=Field(min_length=1,max_length=20000)
    idempotency_key:str=Field(min_length=1,max_length=200)
    user_answer:str|None=Field(default=None,max_length=20000)

class CloseDiscussion(BaseModel):
    reason:str=Field(default='',max_length=20000)
    final_report:str|None=Field(default=None,max_length=50000)

def err(code,detail,status):raise HTTPException(status_code=status,detail={'code':code,'detail':detail})
def _new_id(prefix):return prefix+'-'+secrets.token_hex(8)
def _fingerprint(path,body):return hashlib.sha256(json.dumps([path,body.model_dump()],ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def _replay(key,fingerprint):
    operation=store.get_operation(key)
    if not operation:return None
    if operation['fingerprint']!=fingerprint:err('idempotency_conflict','Key belongs to different request content or discussion',409)
    if operation['state']=='running':err('in_progress','Request already running; do not submit another key',409)
    response=json.loads(operation['response_json'])
    response['idempotent_replay']=True
    return JSONResponse(response,status_code=operation['http_status'])

def _msg_out(m):
    out={k:m[k] for k in ('message_id','seq_no','role','content','created_at')}
    if m['role']=='gpt':
        out.update({k:m[k] for k in ('model','requested_model','prompt_tokens','completion_tokens','cost_cny','funding')})
        out['structured']=json.loads(m['structured_json'])
    return out

def _discussion_out(d):
    result={k:d[k] for k in ('discussion_id','title','status','round','max_rounds','created_at','updated_at','closed_at','final_report','cost_total_cny','budget_cap_cny')}
    messages=store.list_messages(d['discussion_id'])
    last=json.loads(messages[-1]['structured_json']) if messages and messages[-1]['role']=='gpt' else {}
    result.update(messages=[_msg_out(m) for m in messages],pending_questions=last.get('questions_for_muse',[]),needs_user_decision=last.get('needs_user_decision',False))
    return result

def _execute(d,key,content,role,http_status):
    start=time.monotonic()
    try:
        reply=validate_reply(adapter.reply(discussion=d,messages=store.list_messages(d['discussion_id']),new_content=content,user_answer=None,round_no=d['round']+1))
        cost=dict(adapter.cost())
        if not cost.get('model'):raise AdapterError('Actual model was not provided')
        cost['duration_ms']=int((time.monotonic()-start)*1000)
        ts,did,round_no=now(),d['discussion_id'],d['round']+1
        gpt_id=_new_id('msg')
        total=round(d['cost_total_cny']+cost['cost_cny'],6)
        status='waiting_user' if reply['status']=='waiting_user' else ('failed' if reply['status']=='failed' else 'active')
        response={'discussion_id':did,'openai_conversation_id':d['openai_conversation_id'],'status':status,'round':round_no,'message_id':gpt_id,'gpt_reply':reply,'cost':{**cost,'cost_cny_total':total},'created_at':d['created_at'],'updated_at':ts,'idempotent_replay':False}
        seq=len(store.list_messages(did))+1
        user_row=dict(message_id=_new_id('msg'),discussion_id=did,seq_no=seq,role=role,content=content,structured_json='{}',idempotency_key=None,created_at=ts)
        usage={k:cost.get(k) for k in ('model','requested_model','prompt_tokens','completion_tokens','funding')}
        gpt_row=dict(message_id=gpt_id,discussion_id=did,seq_no=seq+1,role='gpt',content=reply['reply'],structured_json=json.dumps(reply,ensure_ascii=False),cost_cny=cost['cost_cny'],idempotency_key=None,created_at=ts,**usage)
        cost_row=dict(call_id=_new_id('call'),discussion_id=did,message_id=gpt_id,cost_cny=cost['cost_cny'],duration_ms=cost['duration_ms'],is_retry=int(cost.get('is_retry',False)),status='committed',created_at=ts,**usage)
        store.commit_round(did,key,user_row,gpt_row,cost_row,dict(round=round_no,status=status,cost_total_cny=total,updated_at=ts),response,http_status)
        return JSONResponse(response,status_code=http_status)
    except Exception as exc:
        detail=str(exc) if isinstance(exc,AdapterError) else 'Local processing or persistence failed: '+type(exc).__name__
        if hasattr(adapter,'redact'):detail=adapter.redact(detail)
        response={'detail':{'code':'gpt_error' if isinstance(exc,AdapterError) else 'local_error','detail':detail,'discussion_id':d['discussion_id'],'automatic_retry':False,'usage':'not provided for unsuccessful request'}}
        store.fail(key,response,502)
        return JSONResponse(response,status_code=502)

@app.get('/api/muse-gpt/health')
def health():
    return dict(ok=True,service=SERVICE,version=VERSION,adapter=adapter.name,db_ok=True,budget_ok=True,credentials_configured=adapter.name=='siwc',gpt_reachable=bool(getattr(adapter,'last_success_at',None)),last_success_at=getattr(adapter,'last_success_at',None))

@app.post('/api/muse-gpt/discussions',status_code=201)
def create_discussion(body:CreateDiscussion):
    with _SERIAL:
        fp=_fingerprint('create',body)
        replay=_replay(body.idempotency_key,fp)
        if replay is not None:return replay
        if body.budget_cap_cny==0:err('budget_exceeded','Budget is zero',409)
        did,ts=_new_id('mg'),now()
        d=dict(discussion_id=did,openai_conversation_id=adapter.create_conversation(did),title=body.title,original_request=body.original_request,user_context=body.user_context,project_goal=body.project_goal,max_rounds=body.max_rounds,budget_cap_cny=body.budget_cap_cny,idempotency_key=body.idempotency_key,created_at=ts,updated_at=ts)
        store.begin(body.idempotency_key,did,fp,d)
        return _execute(store.get_discussion(did),body.idempotency_key,body.first_message,'muse',201)

@app.post('/api/muse-gpt/discussions/{discussion_id}/messages')
def append_message(discussion_id:str,body:AppendMessage):
    with _SERIAL:
        fp=_fingerprint('append:'+discussion_id,body)
        replay=_replay(body.idempotency_key,fp)
        if replay is not None:return replay
        d=store.get_discussion(discussion_id)
        if not d:err('not_found','Discussion not found',404)
        if d['status'] in ('closed','failed'):err('conflict','Discussion is '+d['status'],409)
        if d['round']>=d['max_rounds']:err('conflict','Round limit reached',409)
        if d['budget_cap_cny'] is not None and d['cost_total_cny']>=d['budget_cap_cny']:err('budget_exceeded','Budget reached',409)
        if _rate_limited('disc:'+discussion_id,10):err('rate_limited','Discussion rate limited',429)
        store.begin(body.idempotency_key,discussion_id,fp)
        content=body.content+('\n用户回答: '+body.user_answer if body.user_answer else '')
        return _execute(d,body.idempotency_key,content,body.role,200)

@app.get('/api/muse-gpt/discussions/{discussion_id}')
def get_discussion(discussion_id:str):
    with _SERIAL:
        d=store.get_discussion(discussion_id)
        if not d:err('not_found','Discussion not found',404)
        return _discussion_out(d)

@app.post('/api/muse-gpt/discussions/{discussion_id}/close')
def close_discussion(discussion_id:str,body:CloseDiscussion):
    with _SERIAL:
        d=store.get_discussion(discussion_id)
        if not d:err('not_found','Discussion not found',404)
        if d['status']!='closed':
            store.update_discussion(discussion_id,status='closed',closed_at=now(),final_report=body.final_report,close_reason=body.reason)
            d=store.get_discussion(discussion_id)
        return {k:d[k] for k in ('discussion_id','status','closed_at','round','cost_total_cny')}

def main():
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8003,workers=1,log_level='warning',access_log=False)

if __name__=='__main__':main()
