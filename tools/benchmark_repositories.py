"""Explicit, small real-repository benchmark; never reports a general success rate."""
from __future__ import annotations
import argparse,json,time
from pathlib import Path
import httpx

REPOSITORIES=[('render-flask','https://github.com/render-examples/flask-hello-world'),('digitalocean-flask','https://github.com/digitalocean/sample-flask'),('render-fastapi','https://github.com/render-examples/fastapi')]

def wait(client,identifier,states,timeout):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        task=client.get('/api/tasks/'+identifier).raise_for_status().json()
        if task['status'] in states:return task
        time.sleep(1)
    raise TimeoutError('Task did not reach the expected state in time')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8787')
    parser.add_argument('--only',nargs='+',choices=[name for name,_ in REPOSITORIES],help='Select a subset of the fixed benchmark repositories')
    parser.add_argument('--execute',action='store_true',help='Actually call the configured model and run Docker containers')
    parser.add_argument('--output',default=str(Path(__file__).resolve().parents[1]/'.repolaunch/benchmark-results.json'))
    args=parser.parse_args()
    if not args.execute:parser.error('--execute is required for real model and Docker requests')
    from urllib.parse import urlsplit
    parsed=urlsplit(args.url)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.path not in {'','/'} or parsed.query or parsed.fragment or parsed.username:parser.error('Use a plain local RepoLaunch URL')
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    repositories=[item for item in REPOSITORIES if not args.only or item[0] in args.only]
    report={'scope':'Selected small public Python Web repositories, not a general success rate.','results':[]}
    with httpx.Client(base_url=args.url.rstrip('/'),headers={'X-RepoLaunch':'1'},timeout=15,trust_env=False,follow_redirects=False) as client:
        for name,url in repositories:
            started=time.monotonic();identifier=None;task={}
            result={'name':name,'repository':url,'passed':False}
            try:
                print('Testing '+url,flush=True)
                task=client.post('/api/tasks',json={'repository_url':url,'mode':'real'}).raise_for_status().json();identifier=task['id'];result['task_id']=identifier
                task=wait(client,identifier,{'awaiting_approval','failed'},180)
                if task['status']!='awaiting_approval':raise ValueError(task.get('error') or 'Analysis failed')
                plan=task['plan'];result.update(commit=task.get('analysis',{}).get('commit'),model_source=plan.get('source'),retrieval=plan.get('retrieval'),model_usage=plan.get('model_usage'),entrypoint=plan.get('entrypoint'))
                if plan.get('source')!='llm':raise ValueError('Benchmark requires real model planning')
                if plan.get('needs_env'):raise ValueError('Repository requires unsupported external credentials')
                client.post('/api/tasks/'+identifier+'/approve',json={}).raise_for_status()
                task=wait(client,identifier,{'succeeded','failed','cancelled'},720)
                if task['status']!='succeeded':raise ValueError(task.get('error') or 'Container did not start')
                target=task['result']['url']
                with httpx.Client(timeout=8,trust_env=False,follow_redirects=False) as app:
                    response=app.get(target+'/');response.raise_for_status()
                    if name=='render-flask':assert 'Hello, World!' in response.text
                    elif name=='render-fastapi':
                        assert response.json().get('message')=='Hello World'
                        item=app.get(target+'/items/7',params={'q':'test'});item.raise_for_status()
                        assert item.json().get('item_id')==7 and item.json().get('q')=='test'
                    else:assert 'text/html' in response.headers.get('content-type','') and '<html' in response.text.lower()
                exported=client.get('/api/tasks/'+identifier+'/export');exported.raise_for_status()
                (output.parent/(name+'-report.zip')).write_bytes(exported.content)
                result.update(passed=True,http_status=response.status_code,business_probe=True,repair_attempts=task.get('attempt',0))
            except Exception as error:
                result['error_type']=type(error).__name__
                # Task errors are already sanitized by the application; cap report length.
                result['error']=str(error)[:600]
            finally:
                if identifier:
                    try:
                        current=client.get('/api/tasks/'+identifier).raise_for_status().json()
                        if current['status'] in {'analyzing','running','repairing','awaiting_approval'}:
                            client.post('/api/tasks/'+identifier+'/cancel').raise_for_status()
                        elif (current.get('result') or {}).get('container_id'):
                            stopped=client.post('/api/tasks/'+identifier+'/stop').raise_for_status().json()
                            result['stopped']=stopped['status']=='stopped'
                    except Exception as error:result['cleanup_error_type']=type(error).__name__
                result['duration_seconds']=round(time.monotonic()-started,2)
                report['results'].append(result)
                output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(result,ensure_ascii=False),flush=True)
    report['passed_count']=sum(row['passed'] for row in report['results'])
    report['tested_count']=len(report['results'])
    report['all_stopped']=all(row.get('stopped') for row in report['results'] if row['passed'])
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if report['passed_count']!=len(repositories) or not report['all_stopped']:raise SystemExit(1)

if __name__=='__main__':main()
