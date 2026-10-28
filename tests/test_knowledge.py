import json
from contextlib import contextmanager

import httpx
import pytest

from repolaunch import knowledge


@pytest.fixture(autouse=True)
def isolate_integration_configuration(monkeypatch):
    for key in knowledge._ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with knowledge._STATUS_LOCK:
        knowledge._LAST_STATUS.clear()


def configure(monkeypatch):
    monkeypatch.setenv('REPOLAUNCH_MILVUS_URI', 'http://127.0.0.1:19530')
    monkeypatch.setenv('REPOLAUNCH_EMBEDDING_BASE_URL', 'https://api.siliconflow.cn/v1')
    monkeypatch.setenv('REPOLAUNCH_EMBEDDING_API_KEY', 'dummy-dedicated-embedding-key')


def test_siliconflow_has_supported_default_without_borrowing_planner_key(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv('REPOLAUNCH_API_KEY', 'different-planner-key')
    config = knowledge._vector_configuration()
    assert config['model'] == 'BAAI/bge-m3'
    assert config['api_key'] == 'dummy-dedicated-embedding-key'
    monkeypatch.delenv('REPOLAUNCH_EMBEDDING_API_KEY')
    with pytest.raises(knowledge.RetrievalError, match='单独配置'):
        knowledge._vector_configuration()


def test_real_langchain_embedding_adapter_uses_strings_float_and_no_dimensions(monkeypatch):
    configure(monkeypatch)
    requests = []
    class Client:
        closed = False
        def close(self):
            self.closed = True
    milvus = Client()
    monkeypatch.setattr(knowledge, '_milvus_client', lambda config: milvus)
    def send(self, request, **kwargs):
        requests.append(request)
        body = json.loads(request.content)
        data = [{'object':'embedding','index':index,'embedding':[0.1] * 1024} for index, text in enumerate(body['input'])]
        return httpx.Response(200, json={'object':'list','model':'BAAI/bge-m3','data':data,
                                       'usage':{'prompt_tokens':6,'total_tokens':6}}, request=request)
    monkeypatch.setattr(httpx.Client, 'send', send)
    with knowledge._vector_resources(knowledge._vector_configuration()) as (client, embeddings):
        vectors = embeddings.embed_documents(['Python 模块缺失', '依赖冲突'])
    assert milvus.closed
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body['model'] == 'BAAI/bge-m3'
    assert body['encoding_format'] == 'float'
    assert body['input'] == ['Python 模块缺失', '依赖冲突']
    assert 'dimensions' not in body
    assert str(requests[0].url) == 'https://api.siliconflow.cn/v1/embeddings'
    assert knowledge._validate_vectors(vectors, 2, 'BAAI/bge-m3') == 1024


@pytest.mark.parametrize('vectors', [[[float('nan')]], [[float('inf')]], [[True]], [[0.0]], [[0.1],[0.1,0.2]]])
def test_invalid_vectors_rejected(vectors):
    with pytest.raises(knowledge.RetrievalError):
        knowledge._validate_vectors(vectors, len(vectors), 'test-model')


def test_bge_fixed_dimension_verified():
    with pytest.raises(knowledge.RetrievalError, match='1024'):
        knowledge._validate_vectors([[0.1, 0.2]], 1, 'BAAI/bge-m3')


def test_status_default_makes_no_network_or_embedding_calls(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(knowledge, '_milvus_client', lambda config: pytest.fail('must not connect'))
    status = knowledge.retrieval_status()
    assert status['status'] == 'not_checked'
    assert status['configured'] is True
    assert status['connected'] is None
    assert 'dummy-dedicated-embedding-key' not in json.dumps(status)


def test_read_only_probe_checks_collection_without_embedding(monkeypatch):
    configure(monkeypatch)
    observed = {}
    class Client:
        def has_collection(self, **kwargs):
            return True
        def describe_collection(self, **kwargs):
            return {'fields':[{'name':'vector','params':{'dim':1024}}]}
        def query(self, **kwargs):
            observed['query'] = kwargs
            return [{'case_id':case['id']} for case in knowledge.CASES]
        def close(self):
            observed['closed'] = True
    monkeypatch.setattr(knowledge, '_milvus_client', lambda config: Client())
    monkeypatch.setattr(knowledge, '_vector_resources', lambda config: pytest.fail('must not embed'))
    status = knowledge.retrieval_status(probe=True)
    assert status['status'] == 'indexed'
    assert status['connected'] is True
    assert status['indexed_count'] == 6
    assert status['backend'] == 'lexical'  # Availability is not an executed vector query.
    assert observed['closed']
    assert observed['query']['consistency_level'] == 'Strong'


def test_index_refuses_existing_dimension_mismatch(monkeypatch):
    configure(monkeypatch)
    class Client:
        def has_collection(self, **kwargs):
            return True
        def describe_collection(self, **kwargs):
            return {'fields':[{'name':'vector','params':{'dim':1536}}]}
        def upsert(self, **kwargs):
            pytest.fail('must not write mismatched vectors')
    class Embeddings:
        def embed_documents(self, texts):
            return [[0.1]*1024 for text in texts]
    @contextmanager
    def resources(config):
        yield Client(), Embeddings()
    monkeypatch.setattr(knowledge, '_vector_resources', resources)
    with pytest.raises(knowledge.RetrievalError, match='原集合'):
        knowledge.index_diagnostic_cases()


def test_index_requires_readback_before_claiming_success(monkeypatch):
    configure(monkeypatch)
    class Client:
        def has_collection(self, **kwargs): return False
        def create_collection(self, **kwargs): pass
        def upsert(self, **kwargs): pass
        def flush(self, **kwargs): pass
        def load_collection(self, **kwargs): pass
        def query(self, **kwargs): return []
    class Embeddings:
        def embed_documents(self, texts): return [[0.1]*1024 for text in texts]
    @contextmanager
    def resources(config): yield Client(), Embeddings()
    monkeypatch.setattr(knowledge, '_vector_resources', resources)
    with pytest.raises(knowledge.RetrievalError, match='不能宣称索引成功'):
        knowledge.index_diagnostic_cases()


def test_env_loader_reads_only_explicit_integration_keys_without_overwrite(tmp_path, monkeypatch, capsys):
    envfile = tmp_path/'settings.env'
    envfile.write_text('REPOLAUNCH_API_KEY=do-not-load\nREPOLAUNCH_EMBEDDING_API_KEY="dedicated-secret"\nREPOLAUNCH_MILVUS_URI=http://127.0.0.1:19530\nPATH=do-not-overwrite\n',encoding='utf-8')
    monkeypatch.delenv('REPOLAUNCH_API_KEY', raising=False)
    monkeypatch.setenv('REPOLAUNCH_MILVUS_URI', 'http://localhost:19530')
    assert knowledge.load_integration_env(envfile) == 1
    assert knowledge.os.environ['REPOLAUNCH_EMBEDDING_API_KEY'] == 'dedicated-secret'
    assert 'REPOLAUNCH_API_KEY' not in knowledge.os.environ
    assert knowledge.os.environ['REPOLAUNCH_MILVUS_URI'] == 'http://localhost:19530'
    assert capsys.readouterr().out == ''


def test_saved_credentials_only_restore_explicit_embedding_fields(monkeypatch, tmp_path, capsys):
    from repolaunch import credentials
    observed = {}
    def load(directory):
        observed['directory'] = directory
        return {'api_key':'must-not-use','base_url':'https://planner.example/v1','model':'planner',
                'embedding_api_key':'separately-authorized-key','embedding_base_url':'https://api.siliconflow.cn/v1',
                'embedding_model':'BAAI/bge-m3'}
    monkeypatch.setattr(credentials,'load_credentials',load)
    monkeypatch.setenv('REPOLAUNCH_DATA_DIR',str(tmp_path))
    assert knowledge.load_saved_embedding_configuration() == 3
    assert observed['directory'] == tmp_path
    assert knowledge.os.environ['REPOLAUNCH_EMBEDDING_API_KEY'] == 'separately-authorized-key'
    assert 'REPOLAUNCH_API_KEY' not in knowledge.os.environ or knowledge.os.environ['REPOLAUNCH_API_KEY'] != 'must-not-use'
    assert capsys.readouterr().out == ''


def test_saved_generic_key_is_never_implicitly_borrowed(monkeypatch):
    from repolaunch import credentials
    monkeypatch.setattr(credentials,'load_credentials',lambda directory: {'api_key':'planner-only','base_url':'https://planner.example/v1','model':'chat-model'})
    assert knowledge.load_saved_embedding_configuration() == 0
    assert 'REPOLAUNCH_EMBEDDING_API_KEY' not in knowledge.os.environ


def test_corrupt_saved_credentials_have_generic_error(monkeypatch):
    from repolaunch import credentials
    def fail(directory):
        raise RuntimeError('sensitive-debug secret-key-value')
    monkeypatch.setattr(credentials,'load_credentials',fail)
    with pytest.raises(knowledge.RetrievalError) as caught:
        knowledge.load_saved_embedding_configuration()
    assert caught.value.code == 'credential_unavailable'
    assert 'sensitive-debug' not in str(caught.value)


def test_saved_embedding_config_does_not_replace_environment(monkeypatch):
    from repolaunch import credentials
    monkeypatch.setenv('REPOLAUNCH_EMBEDDING_API_KEY','environment-wins')
    monkeypatch.setattr(credentials,'load_credentials',lambda directory: {'embedding_api_key':'saved-key'})
    assert knowledge.load_saved_embedding_configuration() == 0
    assert knowledge.os.environ['REPOLAUNCH_EMBEDDING_API_KEY'] == 'environment-wins'
