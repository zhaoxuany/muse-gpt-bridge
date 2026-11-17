"""True SIWC calls through an ephemeral loopback service; prints no credentials."""
import datetime
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parent
ACTIVE=ROOT if (ROOT/'bridge').is_dir() else ROOT/'active'
OUT=ROOT/'bridge_local_acceptance.json' if ACTIVE==ROOT else ROOT.parent.parent/'outputs'/'bridge_local_acceptance.json'
BASE='http://127.0.0.1:8003/api/muse-gpt'
token=secrets.token_urlsafe(48)
env={**os.environ,'BRIDGE_TOKEN':token,'BRIDGE_DB':str(ROOT/'acceptance.sqlite'),'MUSE_GPT_ADAPTER':'siwc','SIWC_MODEL':'gpt-6.1-sol','PYTHONUTF8':'1'}
report={'phase':'5 Windows local bridge','started_at':datetime.datetime.now().astimezone().isoformat(),'scope':'127.0.0.1 only; Work acts as local test client, not Muse connector','checks':{},'rounds':[],'errors':[],'reasoning_effort':'not_requested','automatic_muse_end_to_end':'not_run_pending_future_authorization','physical_offline_demo':'not_run','refresh_test':'not_forced_in_phase5; already passed in phase4'}
process=None
log=(ROOT/'acceptance-service.log').open('a',encoding='utf-8')

def save():
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

def request(method,path,body=None,auth=True):
    headers={'Content-Type':'application/json'}
    if auth:headers['Authorization']='Bearer '+token
    req=urllib.request.Request(BASE+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=150) as r:return r.status,json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code,json.load(exc)

def start():
    global process
    process=subprocess.Popen([sys.executable,'-X','utf8','-m','uvicorn','bridge.server:app','--host','127.0.0.1','--port','8003','--workers','1','--log-level','warning','--no-access-log'],cwd=ACTIVE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(100):
        if process.poll() is not None:raise RuntimeError('Local service exited; inspect sanitized local service log')
        try:
            code,data=request('GET','/health',auth=False)
            if code==200:return data
        except (OSError,ValueError):pass
        time.sleep(.1)
    raise TimeoutError('Local service did not become ready')

def stop():
    global process
    if process and process.poll() is None:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

try:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',8003))
    initial=start()
    report['checks']['initial_health_does_not_claim_gpt_reachable']=initial['gpt_reachable'] is False
    create={'title':'Windows本地桥真实验收','original_request':'帮我规划耳机和宿舍用品，只讨论思路，不报实时价格或声称已购物。','user_context':'学生住宿舍；记录代号 W5-LOCAL。','project_goal':'验证讨论历史和用户问题转交','first_message':'先给两句规划思路，并问预算、桌面宽度。请将问题放到questions_for_muse，needs_user_decision=true，status=waiting_user。','idempotency_key':'w5-real-create'}
    report['checks']['unauthorized_is_401']=request('POST','/discussions',create,False)[0]==401
    status,first=request('POST','/discussions',create)
    if status!=201:raise RuntimeError(json.dumps({'http_status':status,'response':first},ensure_ascii=False))
    did=first['discussion_id']
    report['discussion_id']=did
    report['rounds'].append(first)
    report['checks']['round1_questions']=first['gpt_reply']['needs_user_decision'] is True and bool(first['gpt_reply']['questions_for_muse'])
    save()
    print('Round 1 completed; actual model='+str(first['cost']['model']),flush=True)
    stop()
    start()
    status,history=request('GET','/discussions/'+did)
    report['checks']['restart_persists_round1']=status==200 and history['round']==1 and len(history['messages'])==2
    payload={'role':'muse','content':'沿用之前的耳机与宿舍用品计划。请简短回复，明确重述新预算和桌宽，再提出一个需用户选择的问题；不要自作主张。','user_answer':'总预算500元，桌宽60cm。','idempotency_key':'w5-real-append'}
    status,second=request('POST','/discussions/'+did+'/messages',payload)
    if status!=200:raise RuntimeError(json.dumps({'http_status':status,'response':second},ensure_ascii=False))
    report['rounds'].append(second)
    reply=second['gpt_reply']
    report['checks']['round2_context_500_and_60']='500' in reply['reply'] and '60' in reply['reply']
    report['checks']['round2_question_for_muse']=reply['needs_user_decision'] is True and bool(reply['questions_for_muse'])
    history=request('GET','/discussions/'+did)[1]
    report['checks']['four_ordered_persisted_messages']=[m['seq_no'] for m in history['messages']]==[1,2,3,4]
    report['checks']['user_answer_persisted']='500' in history['messages'][2]['content'] and '60' in history['messages'][2]['content']
    again=request('POST','/discussions',create)[1]
    report['checks']['create_replay_exact_round1']=again['idempotent_replay'] is True and again['round']==1 and again['gpt_reply']==first['gpt_reply']
    again=request('POST','/discussions/'+did+'/messages',payload)[1]
    report['checks']['append_replay_exact_round2']=again['idempotent_replay'] is True and again['gpt_reply']==second['gpt_reply']
    history=request('GET','/discussions/'+did)[1]
    report['checks']['replays_did_not_add_messages']=len(history['messages'])==4
    report['checks']['close_succeeds']=request('POST','/discussions/'+did+'/close',{'reason':'本地真实验收结束'})[1]['status']=='closed'
    payload['idempotency_key']='w5-after-close'
    report['checks']['append_after_close_409']=request('POST','/discussions/'+did+'/messages',payload)[0]==409
    report['checks']['all_actual_models_recorded']=all(r['cost']['model']=='gpt-6.1-sol' for r in report['rounds'])
    report['checks']['all_usage_provided']=all(isinstance(r['cost'][k],int) for r in report['rounds'] for k in ('prompt_tokens','completion_tokens'))
    report['checks']['funding_is_plan_usage']=all(r['cost']['funding']=='chatgpt-plus-plan-usage' for r in report['rounds'])
    report['true_calls']=2
    report['passed']=all(report['checks'].values())
    print('Round 2 completed; local acceptance passed='+str(report['passed']),flush=True)
except Exception as exc:
    report['passed']=False
    report['errors'].append(str(exc).replace(token,'[REDACTED]'))
    print('Stopped on error; sanitized evidence saved',flush=True)
finally:
    stop();log.close()
    report['service_stopped']=process is None or process.poll() is not None
    report['finished_at']=datetime.datetime.now().astimezone().isoformat()
    save()
sys.exit(0 if report.get('passed') else 1)
