"""Explicit real-service acceptance runner. No credentials are printed or exported.

Run after start-integrations, from a normal Windows session with Docker access.
It consumes one six-case embedding batch, two embedding queries (one during repair),
and one model repair call.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def configure():
    from dotenv import load_dotenv
    from repolaunch.credentials import load_credentials
    load_dotenv(ROOT/'.env',override=False,interpolate=False)
    data_dir=Path(os.environ.get('REPOLAUNCH_DATA_DIR',str(ROOT/'.repolaunch')))
    saved=load_credentials(data_dir)
    for field,variable in [('embedding_api_key','REPOLAUNCH_EMBEDDING_API_KEY'),('embedding_base_url','REPOLAUNCH_EMBEDDING_BASE_URL'),('embedding_model','REPOLAUNCH_EMBEDDING_MODEL')]:
        if variable not in os.environ and saved.get(field):os.environ[variable]=saved[field]
    settings={field:os.environ.get(variable,saved.get(field,default)) for field,variable,default in [('api_key','REPOLAUNCH_API_KEY',''),('base_url','REPOLAUNCH_BASE_URL','https://api.openai.com/v1'),('model','REPOLAUNCH_MODEL','gpt-4o-mini')]}
    return data_dir,settings


def verify_mysql(data_dir):
    from repolaunch.storage import TaskStore,now
    from sqlalchemy import delete
    url=os.environ.get('DATABASE_URL','')
    if not url.startswith('mysql+pymysql://'):raise ValueError('MySQL is not explicitly configured')
    store=TaskStore(data_dir,url)
    identifier=uuid.uuid4().hex
    created=False
    try:
        assert store.backend=='mysql'
        task={'id':identifier,'name':'ephemeral storage verification','mode':'validation','status':'stopped','created_at':now(),'updated_at':now(),'events':[]}
        store.create(task);created=True
        second=TaskStore(data_dir,url)
        try:assert second.get(identifier)==task
        finally:second.close()
        return {'ok':True,'backend':'mysql','committed_and_read_back':True}
    finally:
        if created:
            with store.engine.begin() as connection:
                connection.execute(delete(store.table).where(store.table.c.id==identifier))
        store.close()


def verify_milvus():
    from repolaunch.knowledge import index_diagnostic_cases,retrieve_diagnostics
    index=index_diagnostic_cases()
    result=retrieve_diagnostics("ModuleNotFoundError: No module named 'requests'",3)
    case_ids=[case['id'] for case in result['cases']]
    if result['backend']!='milvus' or 'missing-module' not in case_ids:raise ValueError('Vector retrieval not verified')
    return {'ok':True,'index':index,'backend':result['backend'],'case_ids':case_ids}


def trace_client():
    import httpx
    base=os.environ.get('LANGFUSE_BASE_URL','').rstrip('/')
    if not base.startswith('http://127.0.0.1:'):raise ValueError('Local Langfuse is not configured')
    return httpx.Client(base_url=base,auth=(os.environ.get('LANGFUSE_PUBLIC_KEY',''),os.environ.get('LANGFUSE_SECRET_KEY','')),timeout=10,trust_env=False,follow_redirects=False)


def verify_langfuse_auth():
    with trace_client() as client:
        health=client.get('/api/public/health?failIfDatabaseUnavailable=true')
        projects=client.get('/api/public/projects')
        if health.status_code!=200 or projects.status_code!=200:raise ValueError('Langfuse health/auth not verified')
        return {'ok':True,'health_status':health.status_code,'authenticated_status':projects.status_code}


def verify_repair(data_dir,settings):
    from repolaunch import engine
    from repolaunch.planner import Planner
    identifier='verify-'+uuid.uuid4().hex[:16]
    folder=data_dir/'acceptance'/identifier
    repo=folder/'repo'
    folder.mkdir(parents=True,exist_ok=False)
    shutil.copytree(ROOT/'repolaunch/fixtures/repair-check',repo)
    files={path.name:path.read_text(encoding='utf-8-sig') for path in repo.iterdir() if path.is_file()}
    analysis={'repo_url':'builtin://controlled-repair-check','commit':'bundled-fixture','repo_dir':str(repo),'files':files,'readme':files['README.md'],'warnings':['Controlled failure injection; not a public repository benchmark.']}
    plan=engine.validate_plan({'python_version':'3.11','install_mode':'none','requirements_file':'','start_kind':'python','entrypoint':'main.py','port':8000,'verify_path':'/health','extra_packages':[],'needs_env':[],'summary':'Controlled acceptance test intentionally omits requests.','warnings':[],'evidence':['This fixture imports requests at module scope.']},repo)
    captured=[]
    def emit(level,message):
        captured.append(str(message))
        if len(captured)>300:del captured[0]
    original_timeout=engine.VERIFY_TIMEOUT
    try:
        engine.VERIFY_TIMEOUT=6
        try:
            failed=engine.run_project(identifier,repo,plan,emit,threading.Event())
        except RuntimeError:
            # An immediately exited container can lose its port binding. Engine
            # emits the actual container traceback before owned-resource cleanup.
            startup_logs='\n'.join(captured)
            if "ModuleNotFoundError: No module named 'requests'" not in startup_logs:raise
            failed={'verification':{'passed':False},'logs':startup_logs}
    finally:engine.VERIFY_TIMEOUT=original_timeout
    logs=failed.get('logs','')
    if failed['verification']['passed'] or "No module named 'requests'" not in logs:raise ValueError('Expected real missing-dependency failure was not observed')
    (folder/'initial-failure.log').write_text(logs,encoding='utf-8')
    (folder/'initial-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    started=time.time()
    trace_name='repolaunch-acceptance-'+uuid.uuid4().hex[:16]
    repaired=Planner(settings,trace_name=trace_name).repair(analysis,plan,logs)
    if not any(package.split('==',1)[0].casefold()=='requests' for package in repaired['extra_packages']):raise ValueError('Model did not propose the evidenced dependency repair')
    result=None
    try:
        result=engine.run_project(identifier,repo,repaired,emit,threading.Event())
        if not result['verification']['passed']:raise ValueError('Repaired container failed HTTP verification')
        import httpx
        with httpx.Client(timeout=5,trust_env=False) as client:
            response=client.get(result['url']+'/health')
            if response.status_code!=200 or response.json().get('service')!='repolaunch-repair-check':raise ValueError('Repaired business probe failed')
        return {'ok':True,'fixture':'builtin://controlled-repair-check','failure_observed':"ModuleNotFoundError: No module named 'requests'",'repaired_packages':repaired['extra_packages'],'model_source':repaired['source'],'model_usage':repaired.get('model_usage'),'retrieval':repaired.get('retrieval'),'tracing':repaired.get('tracing'),'health_status':200,'cleanup_requested':True,'trace_after_unix':int(started)-2,'trace_name':trace_name}
    finally:
        if result and result.get('container_id'):engine.stop_project(result['container_id'])


def verify_trace(after,trace_name):
    from datetime import datetime,timezone
    import hashlib
    timestamp=datetime.fromtimestamp(after,tz=timezone.utc).isoformat()
    deadline=time.monotonic()+90
    with trace_client() as client:
        while time.monotonic()<deadline:
            response=client.get('/api/public/traces',params={'limit':20,'name':trace_name,'fromTimestamp':timestamp})
            if response.status_code==200:
                rows=response.json().get('data',[])
                for row in rows:
                    if row.get('name')==trace_name:
                        return {'ok':True,'server_read_back':True,'trace_id':row['id'],'name':row['name'],'base_url':os.environ.get('LANGFUSE_BASE_URL','').rstrip('/'),'project_key_sha256':hashlib.sha256(os.environ.get('LANGFUSE_PUBLIC_KEY','').encode()).hexdigest()}
            time.sleep(3)
    raise ValueError('Langfuse trace not visible before deadline')


def main():
    data_dir,settings=configure()
    report={'started_at':time.strftime('%Y-%m-%dT%H:%M:%S'),'ok':True,'checks':{}}
    checks=[('mysql',lambda:verify_mysql(data_dir)),('milvus',verify_milvus),('langfuse_auth',verify_langfuse_auth),('controlled_repair',lambda:verify_repair(data_dir,settings))]
    for name,check in checks:
        print('Verifying '+name+'...',flush=True)
        try:report['checks'][name]=check()
        except Exception as error:
            message=str(error)[:600] if isinstance(error,ValueError) else 'Verification failed; inspect local service readiness/configuration.'
            for secret in [settings.get('api_key',''),os.environ.get('REPOLAUNCH_EMBEDDING_API_KEY',''),os.environ.get('LANGFUSE_SECRET_KEY','')]:
                if secret:message=message.replace(secret,'[REDACTED]')
            report['checks'][name]={'ok':False,'error_type':type(error).__name__,'message':message}
            report['ok']=False
        print(json.dumps({name:report['checks'][name]},ensure_ascii=False),flush=True)
    repair=report['checks'].get('controlled_repair',{})
    if repair.get('ok'):
        print('Waiting for Langfuse server trace ingestion...',flush=True)
        try:report['checks']['langfuse_trace']=verify_trace(repair['trace_after_unix'],repair['trace_name'])
        except Exception as error:
            report['checks']['langfuse_trace']={'ok':False,'error_type':type(error).__name__,'message':'Trace was not confirmed on the server.'};report['ok']=False
    else:report['checks']['langfuse_trace']={'ok':False,'message':'No successful real model repair trace to confirm.'};report['ok']=False
    destination=data_dir/'full-stack-verification.json'
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Saved sanitized verification report.',flush=True)
    if not report['ok']:raise SystemExit(1)

if __name__=='__main__':main()
