import io
import json
import time
import zipfile
from fastapi.testclient import TestClient
import pytest
from repolaunch.app import create_app
from repolaunch.storage import TaskStore

HEADERS = {"X-RepoLaunch": "1"}


def wait_task(client, task_id, statuses, limit=5):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in statuses:
            return task
        time.sleep(0.025)
    raise AssertionError(task)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('dotenv.load_dotenv', lambda *args, **kwargs: False)
    for variable in ['REPOLAUNCH_EMBEDDING_API_KEY','REPOLAUNCH_EMBEDDING_BASE_URL','REPOLAUNCH_EMBEDDING_MODEL','REPOLAUNCH_MILVUS_URI','REPOLAUNCH_MILVUS_TOKEN','LANGFUSE_PUBLIC_KEY','LANGFUSE_SECRET_KEY']:
        monkeypatch.setenv(variable, '')
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('REPOLAUNCH_API_KEY', raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def make_demo(client):
    response = client.post('/api/tasks', json={'repository_url':'https://github.com/demo/hello-web','mode':'demo'}, headers=HEADERS)
    assert response.status_code == 201, response.text
    return wait_task(client, response.json()['id'], {'awaiting_approval','failed'})


def test_demo_full_flow_export_and_stop(client):
    task = make_demo(client)
    assert task['status'] == 'awaiting_approval', task
    assert task['plan']['source'] == 'rules'
    response = client.post(f"/api/tasks/{task['id']}/approve", json={}, headers=HEADERS)
    assert response.status_code == 200, response.text
    again = client.post(f"/api/tasks/{task['id']}/approve", json={}, headers=HEADERS)
    assert again.status_code == 400
    task = wait_task(client, task['id'], {'succeeded','failed'})
    assert task['status'] == 'succeeded', task
    assert task['result']['simulated'] is True
    assert task['result']['url'] is None
    response = client.get(f"/api/tasks/{task['id']}/export")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        report = json.loads(z.read('report.json'))
        assert report['mode'] == 'demo'
        assert 'plan.json' in z.namelist()
    assert client.post(f"/api/tasks/{task['id']}/stop", headers=HEADERS).json()['status'] == 'stopped'


def test_static_assets_and_docs(client):
    for url in ['/', '/static/app.js', '/static/styles.css', '/docs', '/openapi.json']:
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.headers['x-content-type-options'] == 'nosniff'


def test_csrf_and_host_guard(client):
    assert client.post('/api/tasks', json={}).status_code == 403
    assert client.post('/api/tasks', json={}, headers={**HEADERS,'Origin':'https://evil.example'}).status_code == 403
    assert client.get('/api/system', headers={'Host':'evil.example'}).status_code == 403


def test_reject_external_url(client):
    response = client.post('/api/tasks', json={'repository_url':'http://169.254.169.254/latest/meta-data','mode':'real'}, headers=HEADERS)
    assert response.status_code == 400
    assert client.get('/api/tasks').json() == []


def test_approval_rejects_command_injection(client):
    task = make_demo(client)
    plan = {**task['plan'], 'entrypoint': 'app:app; curl evil.example'}
    response = client.post(f"/api/tasks/{task['id']}/approve", json={'plan':plan}, headers=HEADERS)
    assert response.status_code == 400
    assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'awaiting_approval'


def test_cancel_before_execution(client):
    task = make_demo(client)
    response = client.post(f"/api/tasks/{task['id']}/cancel", headers=HEADERS)
    assert response.json()['status'] == 'cancelled'
    assert client.post(f"/api/tasks/{task['id']}/approve", json={}, headers=HEADERS).status_code == 400


def test_cancel_during_demo(client):
    task = make_demo(client)
    client.post(f"/api/tasks/{task['id']}/approve", json={}, headers=HEADERS)
    client.post(f"/api/tasks/{task['id']}/cancel", headers=HEADERS)
    assert wait_task(client, task['id'], {'cancelled','failed'})['status'] == 'cancelled'


def test_settings_secret_not_returned_or_persisted(client, tmp_path):
    key = 'sk-test-VERY_PRIVATE_123456'
    response = client.put('/api/settings', json={'base_url':'https://example.com/v1','model':'test-model','api_key':key}, headers=HEADERS)
    assert response.status_code == 200
    assert key not in response.text
    assert key not in client.get('/api/system').text
    assert key not in client.get('/api/tasks').text
    assert client.put('/api/settings', json={'base_url':'http://evil.example','model':'test','api_key':key}, headers=HEADERS).status_code == 400
    for file in tmp_path.rglob('*'):
        if file.is_file():
            assert key.encode() not in file.read_bytes()


def test_secret_preserved_on_empty_settings_update(client):
    client.put('/api/settings', json={'api_key':'secret-value'}, headers=HEADERS)
    assert client.put('/api/settings', json={'api_key':''}, headers=HEADERS).json()['configured']


def test_missing_task(client):
    assert client.get('/api/tasks/no-such-task').status_code == 404


def test_real_analysis_and_missing_docker_preserves_plan(client, monkeypatch):
    from repolaunch import engine
    from pathlib import Path
    fixture = Path(__file__).resolve().parents[1] / 'repolaunch' / 'fixtures'
    monkeypatch.setattr(engine, 'inspect_repository', lambda url, directory, cancel, emit: {'repo_url':url, 'commit':'a'*40, 'repo_dir':str(fixture),'readme':'FastAPI app', 'files':{'app.py':(fixture/'app.py').read_text(encoding='utf-8-sig'),'requirements.txt':'fastapi\nuvicorn'}, 'warnings':[]})
    monkeypatch.setattr(engine, 'docker_status', lambda: {'available':False,'message':'missing'})
    task = client.post('/api/tasks', json={'repository_url':'https://github.com/demo/real-app'}, headers=HEADERS).json()
    task = wait_task(client, task['id'], {'awaiting_approval','failed'})
    assert task['status'] == 'awaiting_approval', task
    assert client.post(f"/api/tasks/{task['id']}/approve", json={}, headers=HEADERS).status_code == 400
    assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'awaiting_approval'


def test_persistence_and_restart_marks_interrupted(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    app = create_app(tmp_path)
    manager = app.state.manager
    manager.store.create({'id':'test','status':'running','created_at':'2026-01-01','events':[]})
    manager.close()
    app2 = create_app(tmp_path)
    assert app2.state.manager.store.get('test')['status'] == 'failed'
    app2.state.manager.close()

def test_cannot_remove_detected_required_environment(client):
    task = make_demo(client)
    manager = client.app.state.manager
    manager.update(task['id'], plan={**task['plan'], 'needs_env':['REQUIRED_TOKEN']})
    response = client.post(f"/api/tasks/{task['id']}/approve", json={'plan':{**task['plan'],'needs_env':[]}}, headers=HEADERS)
    assert response.status_code == 400
    assert '配置' in response.json()['detail'] or '环境变量' in response.json()['detail']


def test_export_before_run_has_pinned_recipe(client):
    task = make_demo(client)
    response = client.get(f"/api/tasks/{task['id']}/export")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        assert {'Dockerfile','.dockerignore','plan.json','REPRODUCE.md'}.issubset(z.namelist())
        assert 'USER 10001:10001' in z.read('Dockerfile').decode()
        assert 'docker build' in z.read('REPRODUCE.md').decode()


def test_cancel_during_analysis_stays_cancelled(client, monkeypatch):
    from repolaunch import engine
    import threading
    entered = threading.Event()
    release = threading.Event()
    fixture = __import__('pathlib').Path(__file__).resolve().parents[1] / 'repolaunch' / 'fixtures'
    def inspect_repo(url, directory, cancel, emit):
        entered.set()
        release.wait(2)
        return {'repo_url':url,'commit':'a'*40,'repo_dir':str(fixture),'readme':'FastAPI','files':{'app.py':(fixture/'app.py').read_text(encoding='utf-8-sig'),'requirements.txt':'fastapi\nuvicorn'},'warnings':[]}
    monkeypatch.setattr(engine, 'inspect_repository', inspect_repo)
    task=client.post('/api/tasks',json={'repository_url':'https://github.com/demo/cancel'},headers=HEADERS).json()
    assert entered.wait(2)
    client.post(f"/api/tasks/{task['id']}/cancel",headers=HEADERS)
    release.set()
    assert wait_task(client,task['id'],{'cancelled','failed'})['status']=='cancelled'


def make_example(client):
    response = client.post('/api/tasks', json={'mode':'example'}, headers=HEADERS)
    assert response.status_code == 201, response.text
    return wait_task(client, response.json()['id'], {'awaiting_approval','failed'})


def test_builtin_example_does_not_fetch_github_or_call_model(client, monkeypatch):
    from pathlib import Path
    from repolaunch import engine
    from repolaunch.planner import Planner
    def forbidden(*args, **kwargs):
        raise AssertionError('Built-in Docker check must not call repository fetch or model planning')
    monkeypatch.setattr(engine, 'inspect_repository', forbidden)
    monkeypatch.setattr(Planner, 'plan', forbidden)
    task = make_example(client)
    assert task['status'] == 'awaiting_approval', task
    assert task['name'] == '内置 Docker 验证'
    assert task['repository_url'] == 'builtin://docker-check'
    assert task['analysis']['origin'] == 'builtin'
    assert task['analysis']['commit'] == 'bundled-example'
    assert task['plan']['source'] == 'builtin'
    assert task['plan']['start_kind'] == 'python'
    assert task['plan']['install_mode'] == 'none'
    assert task['plan']['extra_packages'] == []
    assert task['plan']['needs_env'] == []
    assert task['plan']['verify_path'] == '/health'
    repo = Path(task['analysis']['repo_dir'])
    assert repo.parent.name == task['id']
    assert (repo / 'main.py').is_file()
    assert 'ThreadingHTTPServer' in (repo / 'main.py').read_text(encoding='utf-8')
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/tasks/{task['id']}/export").content)) as z:
        instructions = z.read('REPRODUCE.md').decode()
        assert 'repolaunch/fixtures/docker-check' in instructions
        assert 'codeload.github.com' not in instructions
        assert '非 GitHub 仓库' in instructions


def test_builtin_example_uses_real_runner_and_fixed_plan(client, monkeypatch):
    from repolaunch import engine
    calls = []
    monkeypatch.setattr(engine, 'docker_status', lambda: {'available':True,'message':'test double'})
    def run(task_id, repo, plan, emit, cancel):
        calls.append((task_id, repo, plan))
        emit('info', 'Runner invoked: test double, no real Docker started by this unit test')
        return {'container_id':'repolaunch-'+task_id,'url':'http://127.0.0.1:18080','verification':{'passed':True,'status_code':200,'kind':'http_readiness'},'dockerfile':engine.render_dockerfile(plan),'logs':'test double'}
    monkeypatch.setattr(engine, 'run_project', run)
    task = make_example(client)
    altered = {**task['plan'],'extra_packages':['unexpected-dependency'],'verify_path':'/wrong','python_version':'3.13'}
    response = client.post(f"/api/tasks/{task['id']}/approve", json={'plan':altered}, headers=HEADERS)
    assert response.status_code == 200, response.text
    done = wait_task(client, task['id'], {'succeeded','failed'})
    assert done['status'] == 'succeeded', done
    assert len(calls) == 1
    assert calls[0][2]['extra_packages'] == []
    assert calls[0][2]['verify_path'] == '/health'
    assert calls[0][2]['python_version'] == '3.11'
    assert done['result'].get('simulated') is not True
    assert done['result']['verification']['kind'] == 'http_readiness'
    assert done['result']['url'] == 'http://127.0.0.1:18080'
    assert client.post(f"/api/tasks/{task['id']}/repair", headers=HEADERS).status_code == 400


def test_builtin_example_still_requires_docker_before_execution(client, monkeypatch):
    from repolaunch import engine
    monkeypatch.setattr(engine, 'docker_status', lambda: {'available':False,'message':'not running'})
    task = make_example(client)
    response = client.post(f"/api/tasks/{task['id']}/approve", json={}, headers=HEADERS)
    assert response.status_code == 400
    assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'awaiting_approval'
    assert client.get('/api/system').json()['example_available'] is True


def test_builtin_approval_preserves_docker_access_denied_reason(client, monkeypatch):
    from repolaunch import engine
    monkeypatch.setattr(engine, 'docker_status', lambda: {'available':False,'reason':'access_denied','message':'Docker 引擎访问被拒绝，请从普通 Windows 会话运行；不需要重新安装。'})
    task = make_example(client)
    response = client.post(f"/api/tasks/{task['id']}/approve", json={}, headers=HEADERS)
    assert response.status_code == 400
    assert '访问被拒绝' in response.json()['detail']
    assert '不需要重新安装' in response.json()['detail']
    assert '未安装或未启动' not in response.json()['detail']



def test_model_check_requires_config_and_never_runs_automatically(client, monkeypatch):
    import repolaunch.app as module
    calls = []
    monkeypatch.setattr(module, 'check_model_connection', lambda settings: calls.append(settings) or {'ok':True,'model':settings['model'],'provider_host':'example.com','duration_ms':12,'usage':{'total_tokens':4},'error':None})
    assert client.post('/api/settings/check', headers=HEADERS).status_code == 400
    assert calls == []
    response = client.put('/api/settings', json={'base_url':'https://example.com/v1','model':'org/model-with-slash','api_key':'fake-model-check-key'}, headers=HEADERS)
    assert response.status_code == 200
    assert calls == []
    assert response.json()['connection']['status'] == 'not_tested'
    response = client.post('/api/settings/check', headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['status'] == 'connected'
    assert response.json()['model'] == 'org/model-with-slash'
    assert len(calls) == 1
    assert 'fake-model-check-key' not in response.text
    assert client.get('/api/system').json()['model']['connection']['status'] == 'connected'
    client.put('/api/settings', json={'base_url':'https://example.com/v1','model':'org/other-model'}, headers=HEADERS)
    assert client.get('/api/system').json()['model']['connection']['status'] == 'not_tested'
    assert len(calls) == 1


def test_model_check_failure_is_visible_and_does_not_leak_secret(client, monkeypatch):
    import repolaunch.app as module
    key = 'fake-secret-model-check-123'
    client.put('/api/settings', json={'api_key':key}, headers=HEADERS)
    monkeypatch.setattr(module, 'check_model_connection', lambda settings: {'ok':False,'model':settings['model'],'provider_host':'example.com','duration_ms':2,'usage':None,'error':{'code':'authentication','message':'Rejected '+key,'retryable':False}})
    response = client.post('/api/settings/check', headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['status'] == 'failed'
    assert response.json()['error']['code'] == 'authentication'
    assert key not in response.text
    assert key not in client.get('/api/system').text


def test_validation_errors_do_not_echo_credentials(client):
    secret = 'fake-validation-secret-' + 'x'*1100
    response = client.put('/api/settings', json={'api_key':secret}, headers=HEADERS)
    assert response.status_code == 422
    assert secret not in response.text
    assert 'fake-validation-secret' not in response.text
    assert all('input' not in item and 'ctx' not in item for item in response.json()['detail'])
    assert client.put('/api/settings', json={'model':'   '}, headers=HEADERS).status_code == 400


def test_rotated_credentials_are_scrubbed_from_state_and_every_export_file(client):
    old, new = 'fake-old-private-credential-123', 'fake-new-private-credential-456'
    client.put('/api/settings', json={'api_key':old}, headers=HEADERS)
    client.put('/api/settings', json={'api_key':new}, headers=HEADERS)
    task = make_demo(client)
    manager = client.app.state.manager
    manager.update(task['id'], plan={**task['plan'],'summary':'echo '+old+' '+new,'evidence':[{'api_key':old},old]})
    manager.emit(task['id'], 'error', 'Old request failed '+old+' current '+new)
    response = client.get(f"/api/tasks/{task['id']}")
    assert old not in response.text and new not in response.text
    response = client.get(f"/api/tasks/{task['id']}/export")
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        for name in z.namelist():
            content = z.read(name).decode()
            assert old not in content and new not in content, name
    raw = json.dumps(manager.store.get(task['id']))
    assert old not in raw and new not in raw


def test_real_model_failure_never_approves_or_executes(client, monkeypatch):
    from repolaunch import engine
    from repolaunch.planner import Planner, ModelRequestError
    from pathlib import Path
    fixture = Path(__file__).resolve().parents[1] / 'repolaunch' / 'fixtures'
    monkeypatch.setattr(engine, 'inspect_repository', lambda url, directory, cancel, emit: {'repo_url':url,'commit':'a'*40,'repo_dir':str(fixture),'readme':'FastAPI','files':{},'warnings':[]})
    def failed(self, analysis):
        raise ModelRequestError('authentication', '模型服务认证失败，请检查配置。')
    monkeypatch.setattr(Planner, 'plan', failed)
    calls=[]
    monkeypatch.setattr(engine, 'run_project', lambda *args: calls.append(args))
    client.put('/api/settings', json={'api_key':'fake-error-test-key'}, headers=HEADERS)
    response = client.post('/api/tasks',json={'repository_url':'https://github.com/demo/model-error'},headers=HEADERS)
    task=wait_task(client,response.json()['id'],{'failed'})
    assert task['plan'] is None and task['error_code']=='authentication'
    assert task['error_retryable'] is False
    assert client.post(f"/api/tasks/{task['id']}/approve",json={},headers=HEADERS).status_code == 400
    assert calls == []
    assert client.get('/api/system').json()['model']['connection']['status']=='failed'



def test_remember_and_embedding_reuse_are_explicit_and_encrypted(client, monkeypatch):
    import os
    import repolaunch.credentials as credentials
    saved=[]
    monkeypatch.setattr(credentials,'save_credentials',lambda directory,payload:saved.append(dict(payload)))
    key='fake-encrypted-remember-key'
    response=client.put('/api/settings',json={'api_key':key,'base_url':'https://example.com/v1','model':'org/chat'},headers=HEADERS)
    assert response.status_code==200 and saved==[]
    assert not os.environ.get('REPOLAUNCH_EMBEDDING_API_KEY')
    response=client.put('/api/settings',json={'api_key':key,'base_url':'https://example.com/v1','model':'org/chat','remember':True,'use_for_embeddings':True,'embedding_model':'BAAI/bge-m3'},headers=HEADERS)
    assert response.status_code==200,response.text
    assert len(saved)==1
    assert saved[0]['api_key']==key and saved[0]['embedding_api_key']==key
    assert saved[0]['embedding_model']=='BAAI/bge-m3'
    assert os.environ['REPOLAUNCH_EMBEDDING_API_KEY']==key
    assert response.json()['secure_storage']['saved'] is True
    assert key not in response.text and key not in client.get('/api/system').text


def test_secure_save_failure_keeps_memory_without_plaintext_fallback(client, monkeypatch, tmp_path):
    import repolaunch.credentials as credentials
    def failed(*args):raise credentials.CredentialError('DPAPI unavailable')
    monkeypatch.setattr(credentials,'save_credentials',failed)
    key='fake-no-plaintext-fallback-key'
    response=client.put('/api/settings',json={'api_key':key,'remember':True},headers=HEADERS)
    assert response.status_code==400
    assert '当前进程生效' in response.json()['detail']
    assert client.get('/api/system').json()['model']['configured'] is True
    for file in tmp_path.rglob('*'):
        if file.is_file():assert key.encode() not in file.read_bytes()


def test_sync_saved_credentials_and_explicit_env_priority(tmp_path, monkeypatch):
    from repolaunch.app import TaskManager
    import repolaunch.credentials as credentials
    saved={'api_key':'fake-saved-model-key','base_url':'https://example.com/v1','model':'org/saved-chat','embedding_api_key':'fake-saved-embedding-key','embedding_base_url':'https://example.com/v1','embedding_model':'BAAI/bge-m3'}
    monkeypatch.setattr(credentials,'load_credentials',lambda directory:dict(saved))
    for variable in ['REPOLAUNCH_API_KEY','REPOLAUNCH_BASE_URL','REPOLAUNCH_MODEL','REPOLAUNCH_EMBEDDING_API_KEY','REPOLAUNCH_EMBEDDING_BASE_URL','REPOLAUNCH_EMBEDDING_MODEL']:
        monkeypatch.delenv(variable,raising=False)
    manager=TaskManager(tmp_path/'saved')
    try:
        assert manager.settings['api_key']==saved['api_key']
        assert manager.embedding_settings['api_key']==saved['embedding_api_key']
        assert manager.secure_storage['loaded'] is True
        assert saved['api_key'] not in json.dumps(manager.scrub({'echo':saved['api_key']}))
    finally:manager.close()
    monkeypatch.setenv('REPOLAUNCH_API_KEY','fake-explicit-env-key')
    manager=TaskManager(tmp_path/'env')
    try:assert manager.settings['api_key']=='fake-explicit-env-key'
    finally:manager.close()


def test_sync_memory_redaction_probe_and_integration_metadata(tmp_path, monkeypatch):
    from repolaunch.app import TaskManager
    import repolaunch.app as module
    import repolaunch.credentials as credentials
    monkeypatch.setattr(credentials,'load_credentials',lambda directory:{})
    for variable in ['REPOLAUNCH_API_KEY','REPOLAUNCH_EMBEDDING_API_KEY','REPOLAUNCH_EMBEDDING_BASE_URL','REPOLAUNCH_MILVUS_URI','LANGFUSE_PUBLIC_KEY','LANGFUSE_SECRET_KEY']:
        monkeypatch.setenv(variable,'')
    manager=TaskManager(tmp_path)
    try:
        old,new='fake-sync-old-model-key','fake-sync-new-model-key'
        manager.configure_model('https://example.com/v1','org/model',old)
        manager.configure_model('https://example.com/v1','org/model',new)
        assert old not in manager.redact(old+' '+new) and new not in manager.redact(old+' '+new)
        calls=[]
        monkeypatch.setattr(module,'check_model_connection',lambda settings:calls.append(settings) or {'ok':True,'model':settings['model'],'provider_host':'example.com','duration_ms':1,'usage':{'total_tokens':2},'error':None})
        assert manager.model_connection['status']=='not_tested' and calls==[]
        result=manager.check_model()
        assert result['status']=='connected' and len(calls)==1
        assert new not in json.dumps(result)
        status=manager.integration_status()
        assert status['database_status']['status']=='ready'
        assert status['retrieval']=='lexical'
        assert status['langfuse']['delivery_verified'] is False
        assert len(calls)==1
    finally:manager.close()



def test_sync_explicit_app_data_directory_never_loads_project_dotenv(tmp_path, monkeypatch):
    import dotenv
    from repolaunch.app import create_app
    import repolaunch.credentials as credentials
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('REPOLAUNCH_API_KEY',raising=False)
    monkeypatch.setattr(credentials,'load_credentials',lambda directory:{})
    def forbidden(*args,**kwargs):raise AssertionError('An isolated app must not read user .env')
    monkeypatch.setattr(dotenv,'load_dotenv',forbidden)
    app=create_app(tmp_path)
    try:assert app.state.manager.store.backend=='sqlite'
    finally:app.state.manager.close()


def test_sync_app_project_dotenv_requires_explicit_optin_for_data_directory(tmp_path, monkeypatch):
    import dotenv
    from repolaunch.app import create_app
    import repolaunch.credentials as credentials
    calls=[]
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setattr(credentials,'load_credentials',lambda directory:{})
    monkeypatch.setattr(dotenv,'load_dotenv',lambda *args,**kwargs:calls.append((args,kwargs)))
    app=create_app(tmp_path,load_environment=True)
    try:assert len(calls)==1 and calls[0][1]['override'] is False
    finally:app.state.manager.close()


def acceptance_test_manager(tmp_path, monkeypatch):
    from repolaunch.app import TaskManager
    import repolaunch.credentials as credentials
    import repolaunch.app as module
    import repolaunch.knowledge as knowledge
    monkeypatch.setattr(credentials,'load_credentials',lambda directory:{})
    for variable in ['REPOLAUNCH_API_KEY','REPOLAUNCH_MILVUS_URI','REPOLAUNCH_EMBEDDING_API_KEY','REPOLAUNCH_EMBEDDING_BASE_URL']:
        monkeypatch.setenv(variable,'')
    monkeypatch.setenv('LANGFUSE_BASE_URL','http://127.0.0.1:3301')
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY','fake-acceptance-public-key')
    monkeypatch.setenv('LANGFUSE_SECRET_KEY','fake-acceptance-secret-key')
    monkeypatch.setattr(module,'check_model_connection',lambda *args,**kwargs:pytest.fail('Cached status must not call a model'))
    monkeypatch.setattr(knowledge,'retrieval_status',lambda probe=False: {'configured':False,'status':'disabled','backend':'lexical','checked_at':None})
    return TaskManager(tmp_path)


def acceptance_report():
    import hashlib
    return {'started_at':'2026-10-26T02:05:00','ok':True,'checks':{
        'mysql':{'ok':True,'backend':'mysql','committed_and_read_back':True},
        'milvus':{'ok':True,'private_debug_payload':'never-return-this'},
        'controlled_repair':{'ok':True,'model_source':'llm','health_status':200},
        'langfuse_trace':{'ok':True,'server_read_back':True,'base_url':'http://127.0.0.1:3301','project_key_sha256':hashlib.sha256(b'fake-acceptance-public-key').hexdigest(),'trace_id':'private-trace-id','name':'repolaunch-acceptance-test'}}}


def test_sync_cached_acceptance_only_marks_matching_builtin_trace_as_confirmed(tmp_path, monkeypatch):
    manager=acceptance_test_manager(tmp_path,monkeypatch)
    try:
        (tmp_path/'full-stack-verification.json').write_text(json.dumps(acceptance_report()),encoding='utf-8')
        result=manager.integration_status()
        assert result['acceptance']['available'] is True
        assert result['acceptance']['checks']=={'mysql':True,'milvus':True,'controlled_repair':True,'langfuse_trace':True}
        assert result['langfuse']['delivery_verified'] is True
        assert result['langfuse']['delivery_scope']=='latest_builtin_acceptance'
        assert result['langfuse']['delivery_verified_at']=='2026-10-26T02:05:00'
        assert '非所有任务' in result['langfuse']['message']
        serialized=json.dumps(result)
        for value in ['never-return-this','private-trace-id','fake-acceptance-public-key','fake-acceptance-secret-key','project_key_sha256']:
            assert value not in serialized
    finally:manager.close()


@pytest.mark.parametrize('patch',[
    {'base_url':'http://127.0.0.1:9999'},
    {'project_key_sha256':'0'*64},
    {'ok':'true'},
    {'server_read_back':False},
    {'server_read_back':'true'},
])
def test_sync_cached_trace_mismatch_never_claims_delivery(tmp_path, monkeypatch, patch):
    manager=acceptance_test_manager(tmp_path,monkeypatch)
    try:
        report=acceptance_report();report['checks']['langfuse_trace'].update(patch)
        (tmp_path/'full-stack-verification.json').write_text(json.dumps(report),encoding='utf-8')
        result=manager.integration_status()
        assert result['langfuse']['delivery_verified'] is False
        assert result['acceptance']['trace_matches_current_config'] is False
        assert '已服务端确认' not in result['langfuse']['message']
    finally:manager.close()


@pytest.mark.parametrize('content',['{not json','[]',json.dumps({'started_at':'sensitive-untrusted-label','checks':{}}),'x'*131073],ids=['invalid_json','wrong_root','bad_timestamp','oversized'])
def test_sync_corrupt_or_oversized_acceptance_is_ignored(tmp_path, monkeypatch, content):
    manager=acceptance_test_manager(tmp_path,monkeypatch)
    try:
        (tmp_path/'full-stack-verification.json').write_text(content,encoding='utf-8')
        result=manager.integration_status()
        assert result['acceptance']['available'] is False
        assert result['langfuse']['delivery_verified'] is False
        assert result['acceptance']['started_at'] is None
        assert all(value is False for value in result['acceptance']['checks'].values())
    finally:manager.close()
