from copy import deepcopy
import sys
import types

import pytest

from repolaunch.mcp_server import ReadOnlyTaskTools, create_server


class FakeStore:
    def __init__(self):
        self.tasks = [{
            "id": "a" * 16, "repository_url": "https://github.com/example/demo",
            "name": "example/demo", "mode": "real", "status": "succeeded", "attempt": 1,
            "created_at": "2026-10-25T00:00:00Z", "updated_at": "2026-10-25T00:01:00Z",
            "plan": {"entrypoint": "app:app", "python_version": "3.11", "needs_env": ["SERVICE_KEY"],
                     "evidence": ["private-code"], "warnings": ["private-warning"]},
            "analysis": {"repo_dir": "C:/private-location", "files": {"app.py": "private-code"}},
            "events": [{"message": "private-log"}], "error": "private-error",
            "result": {"verification": {"passed": True, "status_code": 200}, "logs": "private-output"},
        }]
    def list(self):
        return deepcopy(self.tasks)
    def get(self, identifier):
        if identifier != self.tasks[0]["id"]:
            raise KeyError(identifier)
        return deepcopy(self.tasks[0])


def test_list_tasks_only_returns_summary_fields():
    store = FakeStore()
    before = deepcopy(store.tasks)
    result = ReadOnlyTaskTools(store).list_tasks()
    assert result[0]["name"] == "example/demo"
    assert "plan" not in result[0]
    assert "private" not in str(result)
    assert store.tasks == before


def test_task_status_excludes_sensitive_payloads():
    status = ReadOnlyTaskTools(FakeStore()).task_status("a" * 16)
    assert status["verification"]["passed"] is True
    assert status["required_environment_names"] == ["SERVICE_KEY"]
    assert status["has_error"] is True
    assert "private" not in str(status)
    assert status["plan"]["entrypoint"] == "app:app"


def test_export_only_returns_route_and_metadata():
    result = ReadOnlyTaskTools(FakeStore()).export_metadata("a" * 16)
    assert result["api_path"] == "/api/tasks/" + "a" * 16 + "/export"
    assert result["format"] == "zip"
    assert "private" not in str(result)
    assert "content" not in result


@pytest.mark.parametrize("value", ["../secret", "a" * 15, "a" * 33, "G" * 16, "https://evil.invalid", None])
def test_invalid_ids_are_rejected(value):
    with pytest.raises(ValueError):
        ReadOnlyTaskTools(FakeStore()).task_status(value)


@pytest.mark.parametrize("value", [0, 51, -1, True, "20"])
def test_list_limits_are_bounded(value):
    with pytest.raises(ValueError):
        ReadOnlyTaskTools(FakeStore()).list_tasks(value)


def test_missing_task_has_clear_error():
    with pytest.raises(ValueError, match="找不到"):
        ReadOnlyTaskTools(FakeStore()).task_status("b" * 16)


def test_registration_only_exposes_three_read_only_tools(monkeypatch):
    class FakeServer:
        def __init__(self, name):
            self.name = name
            self.tools = {}
        def tool(self, **options):
            def register(function):
                self.tools[function.__name__] = (function, options)
                return function
            return register
    class Annotations:
        def __init__(self, **values):
            self.__dict__.update(values)
    fake_server = types.ModuleType("mcp.server.fastmcp")
    fake_server.FastMCP = FakeServer
    fake_types = types.ModuleType("mcp.types")
    fake_types.ToolAnnotations = Annotations
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake_server)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types)
    server = create_server(FakeStore())
    assert set(server.tools) == {"list_tasks", "task_status", "export_metadata"}
    for function, options in server.tools.values():
        annotations = options["annotations"]
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False
    assert server.tools["list_tasks"][0]()[0]["id"] == "a" * 16


def test_mcp_loads_only_database_environment_without_interpolation(tmp_path, monkeypatch):
    from repolaunch.mcp_server import load_data_environment
    path=tmp_path/'.env'
    path.write_text('DATABASE_URL=mysql+pymysql://user:${MODEL_SECRET}@127.0.0.1/tasks\nREPOLAUNCH_DATA_DIR=F:/MCP-data\nREPOLAUNCH_API_KEY=must-not-import\nREPOLAUNCH_EMBEDDING_API_KEY=must-not-import-embedding\nLANGFUSE_SECRET_KEY=must-not-import-tracing\nMODEL_SECRET=must-not-expand\n',encoding='utf-8')
    for name in ['DATABASE_URL','REPOLAUNCH_DATA_DIR','REPOLAUNCH_API_KEY','REPOLAUNCH_EMBEDDING_API_KEY','LANGFUSE_SECRET_KEY','MODEL_SECRET']:
        monkeypatch.delenv(name,raising=False)
    load_data_environment(path)
    import os
    assert os.environ['DATABASE_URL']=='mysql+pymysql://user:${MODEL_SECRET}@127.0.0.1/tasks'
    assert os.environ['REPOLAUNCH_DATA_DIR']=='F:/MCP-data'
    for name in ['REPOLAUNCH_API_KEY','REPOLAUNCH_EMBEDDING_API_KEY','LANGFUSE_SECRET_KEY','MODEL_SECRET']:
        assert name not in os.environ


def test_mcp_explicit_database_environment_wins(tmp_path, monkeypatch):
    from repolaunch.mcp_server import load_data_environment
    path=tmp_path/'.env'
    path.write_text('DATABASE_URL=mysql+pymysql://from-file\nREPOLAUNCH_DATA_DIR=F:/from-file\n',encoding='utf-8')
    monkeypatch.setenv('DATABASE_URL','sqlite:///explicit.sqlite')
    monkeypatch.setenv('REPOLAUNCH_DATA_DIR','F:/explicit')
    load_data_environment(path)
    import os
    assert os.environ['DATABASE_URL']=='sqlite:///explicit.sqlite'
    assert os.environ['REPOLAUNCH_DATA_DIR']=='F:/explicit'


def test_mcp_main_disables_database_ddl(tmp_path, monkeypatch):
    import repolaunch.mcp_server as module
    events=[]
    monkeypatch.setattr(module,'load_data_environment',lambda:events.append('load'))
    monkeypatch.setenv('DATABASE_URL','sqlite:///only-this-test.sqlite')
    monkeypatch.setenv('REPOLAUNCH_DATA_DIR',str(tmp_path))
    class Store:
        backend='sqlite'
        def __init__(self,path,url,*,initialize=True):
            events.append(('store',path,url,initialize))
        def close(self):events.append('close')
    class Server:
        def run(self,**kwargs):events.append(('run',kwargs))
    monkeypatch.setattr(module,'TaskStore',Store)
    monkeypatch.setattr(module,'create_server',lambda store:Server())
    module.main()
    assert events==['load',('store',tmp_path,'sqlite:///only-this-test.sqlite',False),('run',{'transport':'stdio'}),'close']


def test_readonly_store_construction_emits_no_create_table(tmp_path):
    from repolaunch.storage import TaskStore
    from sqlalchemy import event
    store=TaskStore(tmp_path,initialize=False)
    statements=[]
    event.listen(store.engine,'before_cursor_execute',lambda conn,cursor,statement,parameters,context,executemany:statements.append(statement))
    try:
        assert statements==[]
        from sqlalchemy.exc import SQLAlchemyError
        with pytest.raises(SQLAlchemyError):store.list()
        assert all('CREATE' not in text.upper() and 'INSERT' not in text.upper() for text in statements)
    finally:store.close()


def test_mcp_database_errors_hide_driver_details():
    from sqlalchemy.exc import OperationalError
    class Store:
        def list(self):raise OperationalError('secret-connection-url',{},Exception('private-db-password'))
        def get(self,task_id):raise OperationalError('secret-connection-url',{},Exception('private-db-password'))
    tools=ReadOnlyTaskTools(Store())
    for action in [lambda:tools.list_tasks(),lambda:tools.task_status('a'*16)]:
        with pytest.raises(ValueError) as error:action()
        assert 'private' not in str(error.value) and 'secret' not in str(error.value)


def test_verifier_child_environment_excludes_model_credentials(monkeypatch):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[1]/'tools/verify_mcp.py'
    spec=importlib.util.spec_from_file_location('repolaunch_verify_mcp_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('REPOLAUNCH_API_KEY','private-planner-key')
    monkeypatch.setenv('REPOLAUNCH_EMBEDDING_API_KEY','private-embedding-key')
    monkeypatch.setenv('LANGFUSE_SECRET_KEY','private-tracing-key')
    monkeypatch.setenv('DATABASE_URL','sqlite:///allowed.sqlite')
    values=module.minimal_environment()
    assert values['DATABASE_URL']=='sqlite:///allowed.sqlite'
    assert values['PYTHONUTF8']=='1'
    assert not {'REPOLAUNCH_API_KEY','REPOLAUNCH_EMBEDDING_API_KEY','LANGFUSE_SECRET_KEY'} & values.keys()
