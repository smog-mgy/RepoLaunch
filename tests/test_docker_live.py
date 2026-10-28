"""Opt-in real Docker check. Core tests never silently substitute for it."""
import json
import os
import threading
from urllib.request import urlopen
import pytest
from repolaunch import engine

pytestmark = pytest.mark.skipif(os.getenv('REPOLAUNCH_RUN_DOCKER_TESTS') != '1', reason='Set REPOLAUNCH_RUN_DOCKER_TESTS=1 on a machine with Docker to run the real integration test.')


def test_real_docker_build_run_and_stop(tmp_path):
    assert engine.docker_status()['available'], 'Docker engine must be ready for this opt-in test.'
    repo=tmp_path/'repo'
    repo.mkdir()
    (repo/'main.py').write_text('''import os,json
from http.server import HTTPServer,BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"status":"ok"}')
HTTPServer(('0.0.0.0',int(os.getenv('PORT','8000'))),Handler).serve_forever()
''',encoding='utf-8')
    plan={'python_version':'3.11','install_mode':'none','requirements_file':'','start_kind':'python','entrypoint':'main.py','port':8000,'verify_path':'/health','extra_packages':[],'needs_env':[]}
    task_id='live-'+__import__('uuid').uuid4().hex[:12]
    name='repolaunch-'+task_id
    try:
        result=engine.run_project(task_id,repo,plan,lambda level,message:None,threading.Event())
        assert result['verification']['passed'], result
        with urlopen(result['url']+'/health',timeout=5) as response:
            assert json.load(response)=={'status':'ok'}
    finally:
        engine.stop_project(name)
