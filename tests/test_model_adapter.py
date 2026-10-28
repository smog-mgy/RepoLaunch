import json
from pathlib import Path
import httpx
from repolaunch.planner import Planner, PLAN_KEYS, heuristic_plan


def test_real_langchain_chat_adapter_with_controlled_http(tmp_path, monkeypatch):
    (tmp_path/'app.py').write_text('from fastapi import FastAPI\napp=FastAPI()\n',encoding='utf-8')
    analysis={'repo_url':'https://github.com/demo/app','commit':'a'*40,'repo_dir':str(tmp_path),'readme':'FastAPI demo','files':{'app.py':(tmp_path/'app.py').read_text()},'warnings':[]}
    proposal={k:v for k,v in heuristic_plan(analysis).items() if k in PLAN_KEYS}
    requests=[]
    def fake_send(self,request,**kwargs):
        requests.append(request)
        body={'id':'chatcmpl-test','object':'chat.completion','created':1,'model':'test-model','choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(proposal)},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':30,'total_tokens':50}}
        return httpx.Response(200,json=body,request=request)
    monkeypatch.setattr(httpx.Client,'send',fake_send)
    planner=Planner({'api_key':'test-secret-only-in-client','base_url':'https://models.example/v1','model':'test-model'})
    result=planner.plan(analysis)
    assert result['source']=='llm'
    assert len(requests)==1
    assert str(requests[0].url)=='https://models.example/v1/chat/completions'
    assert requests[0].headers['Authorization']=='Bearer test-secret-only-in-client'
    assert 'test-secret-only-in-client' not in json.dumps(result)
    assert result['entrypoint']=='app:app'

import pytest

from repolaunch.planner import ModelRequestError, _strict_json, check_model_connection


@pytest.fixture
def model_analysis(tmp_path):
    source = 'from fastapi import FastAPI\napp=FastAPI()\n'
    (tmp_path / 'app.py').write_text(source, encoding='utf-8')
    return {'repo_url':'https://github.com/demo/app','commit':'a'*40,'repo_dir':str(tmp_path),
            'readme':'FastAPI demo','files':{'app.py':source},'warnings':[]}


def controlled_chat(monkeypatch, content, *, status=200, finish='stop', refusal=None):
    requests = []
    def send(self, request, **kwargs):
        requests.append(request)
        if status != 200:
            return httpx.Response(status, json={'error':{'message':'SECRET_PROVIDER_BODY api_key=do-not-leak','type':'test_error','code':'test'}}, request=request)
        message = {'role':'assistant','content':content}
        if refusal:
            message['refusal'] = refusal
        body = {'id':'chatcmpl-test','object':'chat.completion','created':1,'model':'test-model',
                'choices':[{'index':0,'message':message,'finish_reason':finish}],
                'usage':{'prompt_tokens':20,'completion_tokens':30,'total_tokens':50}}
        return httpx.Response(200,json=body,request=request)
    monkeypatch.setattr(httpx.Client, 'send', send)
    return requests


def test_siliconflow_uses_compatible_budget_json_and_no_thinking(model_analysis, monkeypatch):
    proposal = heuristic_plan(model_analysis)
    requests = controlled_chat(monkeypatch, json.dumps(proposal))
    result = Planner({'api_key':'test-only-key','base_url':'https://api.siliconflow.cn/v1',
                      'model':'deepseek-ai/DeepSeek-V4.1-Flash'}).plan(model_analysis)
    body = json.loads(requests[0].content)
    assert len(requests) == 1
    assert body['max_tokens'] == 2500
    assert 'max_completion_tokens' not in body
    assert body['response_format'] == {'type':'json_object'}
    assert body['enable_thinking'] is False
    assert result['model_usage']['total_tokens'] == 50
    assert result['model_usage']['input_tokens'] == 20
    assert result['model_usage']['request_count'] == 1
    assert result['model_usage']['provider_host'] == 'api.siliconflow.cn'
    assert result['model_usage']['duration_ms'] >= 0
    assert 'test-only-key' not in json.dumps(result)


def test_non_siliconflow_host_does_not_get_custom_flags(model_analysis, monkeypatch):
    requests = controlled_chat(monkeypatch, json.dumps(heuristic_plan(model_analysis)))
    Planner({'api_key':'test-only-key','base_url':'https://api.siliconflow.cn.evil.invalid/v1','model':'test-model'}).plan(model_analysis)
    body = json.loads(requests[0].content)
    assert 'enable_thinking' not in body
    assert 'max_completion_tokens' in body
    assert 'max_tokens' not in body


@pytest.mark.parametrize(('status','code'), [(401,'authentication'),(403,'permission'),(404,'model_not_found'),(402,'quota'),(429,'rate_limited'),(500,'provider_unavailable'),(400,'invalid_request')])
def test_provider_errors_are_typed_sanitized_and_not_retried(model_analysis, monkeypatch, status, code):
    requests = controlled_chat(monkeypatch, '', status=status)
    with pytest.raises(ModelRequestError) as caught:
        Planner({'api_key':'do-not-leak','base_url':'https://models.example/v1','model':'test-model'}).plan(model_analysis)
    assert caught.value.code == code
    assert len(requests) == 1
    assert 'do-not-leak' not in str(caught.value)
    assert 'SECRET_PROVIDER_BODY' not in str(caught.value)


def test_timeout_is_safe_without_retry(model_analysis, monkeypatch):
    requests = []
    def send(self, request, **kwargs):
        requests.append(request)
        raise httpx.ReadTimeout('SECRET_REQUEST_BODY token=hidden', request=request)
    monkeypatch.setattr(httpx.Client, 'send', send)
    with pytest.raises(ModelRequestError) as caught:
        Planner({'api_key':'test-only-key','base_url':'https://models.example/v1','model':'test-model'}).plan(model_analysis)
    assert caught.value.code == 'timeout'
    assert caught.value.retryable is True
    assert len(requests) == 1
    assert 'SECRET' not in str(caught.value)


def test_valid_json_still_rejected_when_provider_says_truncated(model_analysis, monkeypatch):
    controlled_chat(monkeypatch, json.dumps(heuristic_plan(model_analysis)), finish='length')
    with pytest.raises(ModelRequestError) as caught:
        Planner({'api_key':'test-only-key','base_url':'https://models.example/v1','model':'test-model'}).plan(model_analysis)
    assert caught.value.code == 'truncated_response'


def test_missing_plan_fields_are_not_defaulted_into_execution(model_analysis, monkeypatch):
    controlled_chat(monkeypatch, '{"entrypoint":"app:app","start_kind":"uvicorn"}')
    with pytest.raises(ValueError, match='缺少必要字段'):
        Planner({'api_key':'test-only-key','base_url':'https://models.example/v1','model':'test-model'}).plan(model_analysis)


@pytest.mark.parametrize('text', ['{"port":8000,"port":9000}', '{"port":NaN}', '{"port":Infinity}', '[1,2]', 'prefix {"ok":true} suffix', '{"ok":true} {"ok":false}'])
def test_json_ambiguity_and_nonstandard_numbers_rejected(text):
    with pytest.raises(ModelRequestError):
        _strict_json(text)


def test_json_fence_bom_and_final_text_blocks_supported():
    assert _strict_json('\ufeff```json\n{"ok":true}\n```') == {'ok':True}
    blocks = [{'type':'reasoning','text':'do not parse me'}, {'type':'text','text':'{"ok":true}'}]
    assert _strict_json(blocks) == {'ok':True}


def test_connection_check_is_one_tiny_request_without_repository_content(monkeypatch):
    requests = controlled_chat(monkeypatch, '{"ok":true}')
    result = check_model_connection({'api_key':'test-only-key','base_url':'https://api.siliconflow.cn/v1',
                                     'model':'deepseek-ai/DeepSeek-V4.1-Flash'})
    assert result['ok'] is True
    assert result['usage']['total_tokens'] == 50
    body = json.loads(requests[0].content)
    assert body['max_tokens'] == 64
    assert len(body['messages']) == 2
    assert len(json.dumps(body['messages'])) < 300
    assert len(requests) == 1
    assert 'test-only-key' not in json.dumps(result)


def test_connection_check_has_safe_structured_error(monkeypatch):
    requests = controlled_chat(monkeypatch, '', status=401)
    result = check_model_connection({'api_key':'test-only-key','base_url':'https://models.example/v1','model':'test-model'})
    assert result['ok'] is False
    assert result['error']['code'] == 'authentication'
    assert 'SECRET_PROVIDER_BODY' not in json.dumps(result)
    assert len(requests) == 1


def test_connection_check_without_key_does_not_send(monkeypatch):
    def send(*args, **kwargs):
        raise AssertionError('must not send without a configured key')
    monkeypatch.setattr(httpx.Client, 'send', send)
    assert check_model_connection({'base_url':'https://models.example/v1','model':'test-model'})['error']['code'] == 'not_configured'


def repair_patch(**updates):
    patch = {'python_version':None,'add_packages':['requests'],
             'summary':'根据实际缺失模块日志补充 requests；保留已批准入口。',
             'warnings':[], 'evidence':["ModuleNotFoundError: No module named 'requests'"]}
    patch.update(updates)
    return patch


def test_real_http_repair_uses_patch_prompt_and_preserves_approved_fields(model_analysis, monkeypatch):
    old = heuristic_plan(model_analysis)
    old.update(start_kind='python', entrypoint='app.py', port=8123, verify_path='/health')
    old['source'] = 'llm'
    old['model_usage'] = {'total_tokens':999}
    requests = controlled_chat(monkeypatch, json.dumps(repair_patch(), ensure_ascii=False))
    result = Planner({'api_key':'test-only-key','base_url':'https://api.siliconflow.cn/v1',
                      'model':'deepseek-ai/DeepSeek-V4.1-Flash'}).repair(
        model_analysis, old, "Traceback:\nModuleNotFoundError: No module named 'requests'")
    immutable = {'install_mode','requirements_file','start_kind','entrypoint','port','verify_path','needs_env'}
    for field in immutable:
        assert result[field] == old[field]
    assert result['extra_packages'] == [*old['extra_packages'], 'requests']
    assert result['repair_count'] == 1
    assert result['source'] == 'llm'
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    prompt = body['messages'][0]['content']
    assert 'SMALL REPAIR PATCH' in prompt
    assert 'add_packages' in prompt
    assert 'Do NOT return a complete plan' in prompt
    payload = json.loads(body['messages'][1]['content'])
    assert set(payload['old_plan']) == PLAN_KEYS
    assert 'model_usage' not in payload['old_plan']


def test_real_http_repair_rejects_unknown_patch_field(model_analysis, monkeypatch):
    old = heuristic_plan(model_analysis)
    controlled_chat(monkeypatch, json.dumps(repair_patch(entrypoint='other.py')))
    with pytest.raises(ValueError, match='受控补丁字段'):
        Planner({'api_key':'test-only-key','base_url':'https://models.example/v1','model':'test-model'}).repair(
            model_analysis, old, "ModuleNotFoundError: No module named 'requests'")


def test_real_http_repair_rejects_full_plan_instead_of_silently_merging(model_analysis, monkeypatch):
    old = heuristic_plan(model_analysis)
    controlled_chat(monkeypatch, json.dumps(old))
    with pytest.raises(ValueError, match='不能重写完整运行方案'):
        Planner({'api_key':'test-only-key','base_url':'https://models.example/v1','model':'test-model'}).repair(
            model_analysis, old, "ModuleNotFoundError: No module named 'requests'")


def test_patch_merge_still_rejects_unsafe_packages(model_analysis, monkeypatch):
    old = heuristic_plan(model_analysis)
    controlled_chat(monkeypatch, json.dumps(repair_patch(add_packages=['https://evil.invalid/package.whl'])))
    with pytest.raises(ValueError):
        Planner({'api_key':'test-only-key','base_url':'https://models.example/v1','model':'test-model'}).repair(
            model_analysis, old, "ModuleNotFoundError: No module named 'requests'")


def test_patch_merge_still_requires_evidenced_module(model_analysis, monkeypatch):
    old = heuristic_plan(model_analysis)
    controlled_chat(monkeypatch, json.dumps(repair_patch(add_packages=['flask'])))
    with pytest.raises(ValueError, match='模块报错依据'):
        Planner({'api_key':'test-only-key','base_url':'https://models.example/v1','model':'test-model'}).repair(
            model_analysis, old, "ModuleNotFoundError: No module named 'requests'")


@pytest.mark.parametrize('patch', [repair_patch(add_packages='requests'), repair_patch(python_version=True),
                                   repair_patch(warnings={}), repair_patch(evidence=['x']*51)])
def test_patch_types_are_strict(model_analysis, monkeypatch, patch):
    old = heuristic_plan(model_analysis)
    controlled_chat(monkeypatch, json.dumps(patch))
    with pytest.raises(ValueError):
        Planner({'api_key':'test-only-key','base_url':'https://models.example/v1','model':'test-model'}).repair(
            model_analysis, old, "ModuleNotFoundError: No module named 'requests'")
