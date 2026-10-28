'use strict';
const $ = (id) => document.getElementById(id);
const state = {tasks:[], selected:null, system:null, busy:false, pollBusy:false, planSignature:'', eventSignature:'', toastTimer:null};
const activeStatuses = new Set(['analyzing','running','repairing']);
const statusLabels = {analyzing:'分析中',awaiting_approval:'待确认方案',running:'正在启动',succeeded:'验证通过',failed:'需要处理',cancelled:'已取消',stopped:'已停止',repairing:'分析修复中'};
function element(tag, className, text){ const node=document.createElement(tag); if(className)node.className=className; if(text!==undefined&&text!==null)node.textContent=String(text); return node; }
function show(node, visible){node.hidden=!visible;}
function setText(id, value){$(id).textContent=value===null||value===undefined?'':String(value);}
function readable(value){return typeof value==='string'?value:JSON.stringify(value,null,2);}
function taskStatus(task){return task.mode==='demo'&&task.status==='succeeded'?'演示完成':statusLabels[task.status]||task.status;}
function taskName(task){return task.name || (task.repository_url||'').replace(/\/$/,'').split('/').slice(-2).join('/') || '未命名任务';}
function timeLabel(value){if(!value)return '--:--:--'; const date=new Date(value);return Number.isNaN(date.valueOf())?String(value).slice(0,8):date.toLocaleTimeString('zh-CN',{hour12:false});}
function toast(message){setText('toast',message);show($('toast'),true);clearTimeout(state.toastTimer);state.toastTimer=setTimeout(()=>show($('toast'),false),3500);}
async function api(path, options={}){
  const headers={'X-RepoLaunch':'1',...(options.body?{'Content-Type':'application/json'}:{}),...options.headers};
  const response=await fetch(path,{...options,headers,credentials:'same-origin',cache:'no-store'});
  let body; try{body=await response.json();}catch{body=null;}
  if(!response.ok){const detail=body?.detail??body?.error??body?.message;const message=Array.isArray(detail)?detail.map(x=>x.msg||readable(x)).join('；'):detail?readable(detail):`请求失败（${response.status}）`;throw new Error(message);}
  return body;
}
function renderHistory(){
  setText('task-count',state.tasks.length);
  const container=$('task-list');container.replaceChildren();
  if(!state.tasks.length){const p=element('p','empty-history');p.append('还没有任务。',document.createElement('br'),'从一个想试试的仓库开始。');container.append(p);return;}
  for(const task of state.tasks){
    const button=element('button','task-nav'+(state.selected?.id===task.id?' active':''));
    button.type='button';button.setAttribute('aria-label',`${taskName(task)}，${taskStatus(task)}`);
    const content=element('span','task-nav-content');content.append(element('strong','',taskName(task)),element('small','',`${task.mode==='demo'?'演示 · ':task.mode==='example'?'内置 · ':''}${taskStatus(task)}`));
    button.append(element('span','nav-task-icon','◇'),content);button.addEventListener('click',()=>selectTask(task.id));container.append(button);
  }
}
function updateTask(task){
  const i=state.tasks.findIndex(t=>t.id===task.id);if(i===-1)state.tasks.unshift(task);else state.tasks[i]=task;
  if(!state.selected||state.selected.id===task.id){state.selected=task;renderTask();}renderHistory();
}
async function refreshSystem(){
  try{
    state.system=await api('/api/system');
    const ready=Boolean(state.system.docker?.available);$('env-dot').classList.toggle('ready',ready);
    setText('env-label',ready?'Docker 已就绪':state.system.docker?.reason==='access_denied'?'Docker 访问受限':'Docker 尚未就绪');
    const note=$('home-environment');show(note,!ready);
    note.querySelector('p').textContent=ready?'':(state.system.docker?.message||'Docker 当前不可用。')+' 仍可分析仓库或体验演示。';
    const configured=Boolean(state.system.model?.configured);
    setText('settings-system',`Docker：${ready?'已就绪':state.system.docker?.message||'未检测到'}\n模型：${configured?state.system.model?.model||'已配置':'未配置，可先体验演示'}`);
    const integrations=state.system.integrations||{};
    const db=integrations.database_status;const retrieval=integrations.retrieval_status;const tracing=integrations.langfuse;
    setText('integration-status',`任务数据库：${db?.backend||integrations.database||'未知'} · ${db?.status==='ready'?'读取正常':'待核验'}\n检索：${retrieval?.message||integrations.retrieval||'未知'}\n追踪：${tracing?.message||'使用本地日志'}`);
    const acceptance=integrations.acceptance;
    if(acceptance?.available){const checked=acceptance.checks||{};const summary=[['mysql','MySQL'],['milvus','Milvus'],['controlled_repair','受控修复'],['langfuse_trace','追踪']].map(([key,label])=>label+(checked[key]?' ✓':' 未通过')).join(' · ');$('integration-status').textContent+=`\n最近内置验收 ${acceptance.started_at||''}：${summary}\n记录不代表当前任务结果或服务实时在线。`;}
    const storage=state.system.model?.secure_storage;
    $('remember-credentials').disabled=storage?.supported===false;
    setText('secure-storage-status',storage?.warning||(storage?.saved?'已有当前账户加密保存的配置。仅保存到内存不会删除先前保存的配置。':storage?.supported===false?'此系统不支持 Windows 加密保存，可使用仅存内存的配置。':'加密文件保存在本地工作目录，密钥不会写入浏览器存储。'));
    const connection=state.system.model?.connection;
    const connectionText=!configured?'尚未配置模型。保存配置不会自动发送测试请求。':connection?.status==='connected'?`连接已验证：${connection.message||'请求成功'}${connection.checked_at?' · '+timeLabel(connection.checked_at):''}`:connection?.status==='failed'?`最近请求失败：${connection.message||'请检查模型设置'}`:'已保存配置，尚未验证模型连接。';
    setText('model-connection',connectionText);$('model-check').disabled=!configured;
    if(state.selected)renderTask();
  }catch(error){setText('env-label','环境检查失败');}
}
async function refreshTasks(){const data=await api('/api/tasks');state.tasks=Array.isArray(data)?data:data?.tasks||[];renderHistory();}
async function selectTask(id){
  try{
    const task=await api(`/api/tasks/${encodeURIComponent(id)}`);state.selected=task;state.planSignature='';state.eventSignature='';
    show($('home'),false);show($('task-view'),true);renderTask();renderHistory();
  }catch(error){toast(error.message);}
}
function openHome(){state.selected=null;state.planSignature='';state.eventSignature='';show($('home'),true);show($('task-view'),false);setText('breadcrumb-current','新建任务');renderHistory();$('repository-url').focus();}
function renderStepper(task){
  const stages=[['分析仓库','分析依赖与入口'],['确认方案','执行前审阅'],['构建运行','在容器中执行'],['验证交付','检查响应与导出']];
  let current=task.status==='analyzing'?0:task.status==='awaiting_approval'?1:task.status==='running'||task.status==='repairing'?2:3;
  if(task.status==='failed'&&!task.plan)current=0;
  $('task-stepper').replaceChildren(...stages.map(([title],i)=>{const div=element('div','task-step'+(i<current?' complete':'')+(i===current?' current':''));div.append(element('span','',i<current?'✓':`0${i+1}`),element('strong','',title));return div;}));
}
function fillPlan(task){
  const plan=task.plan;show($('plan-content'),Boolean(plan));
  setText('plan-lock',!plan?'等待分析':(plan.source==='builtin'?'内置验证':plan.source==='llm'?'模型规划':'规则分析')+' · '+(task.status==='awaiting_approval'?'待确认':'已记录'));
  setText('plan-summary',plan?.summary||(!plan&&task.status==='failed'?'分析未能完成，请查看失败原因和执行记录。':'正在读取仓库信息，安装前你会看到完整方案。'));
  const usage=plan?.model_usage;show($('model-usage'),Boolean(usage));
  if(usage)setText('model-usage',`模型 ${usage.model||''} · ${usage.duration_ms!=null?usage.duration_ms+' ms':'耗时未提供'} · Token ${usage.total_tokens??'服务商未提供'} · 请求 ${usage.request_count??1} 次`);
  if(!plan)return;
  const signature=`${task.id}:${task.attempt||0}:${task.status}:${JSON.stringify(plan)}`;
  if(signature!==state.planSignature){
    const fields=$('plan-form').elements;
    for(const key of ['python_version','install_mode','requirements_file','start_kind','entrypoint','port','verify_path'])if(fields[key])fields[key].value=plan[key]??(key==='verify_path'?'/':key==='port'?8000:'');
    fields.extra_packages.value=Array.isArray(plan.extra_packages)?plan.extra_packages.join(', '):plan.extra_packages||'';
    state.planSignature=signature;
  }
  const editable=task.status==='awaiting_approval';for(const field of $('plan-form').elements)field.disabled=!editable||task.mode==='example';
  const warnings=Array.isArray(plan.warnings)?plan.warnings:plan.warnings?[plan.warnings]:[];
  $('plan-warnings').replaceChildren(...warnings.map(w=>element('p','warning-line',readable(w))));
  const needed=Array.isArray(plan.needs_env)?plan.needs_env:[];
  show($('needs-env'),needed.length>0);setText('needs-env',`需要外部配置：${needed.map(readable).join('、')}。首版不向目标容器注入密钥，请选择不依赖这些配置的项目。已检测的必填配置不能通过编辑方案跳过。`);
  const evidence=plan.evidence||task.analysis?.evidence||[];
  setText('plan-evidence',Array.isArray(evidence)?evidence.map(readable).join('\n\n'):readable(evidence));
  show($('approve-button'),editable);show($('cancel-button'),editable||activeStatuses.has(task.status));
  const noDocker=task.mode!=='demo'&&!state.system?.docker?.available;
  const blocker=needed.length?'缺少运行时配置，当前方案不能直接执行。':noDocker?(state.system?.docker?.message||'尚未确认 Docker 状态。')+' 请查看环境设置并刷新状态，再确认方案。':'';
  $('approve-button').disabled=state.busy||Boolean(blocker);$('cancel-button').disabled=state.busy;
  show($('approval-blocker'),editable&&Boolean(blocker));setText('approval-blocker',blocker);
  const button=$('approve-button');button.firstChild.textContent=task.mode==='demo'?'确认并体验演示 ':task.mode==='example'?'确认并真实验证 Docker ':'确认方案并启动 ';
}
function safeAppUrl(raw){try{const url=new URL(raw);return url.protocol==='http:'&&['127.0.0.1','localhost','[::1]'].includes(url.hostname)&&!url.username&&!url.password?url.href:null;}catch{return null;}}
function addMetric(label,value){const card=element('div','result-metric');card.append(element('small','',label),element('strong','',value));$('result-metrics').append(card);}
function renderResult(task){
  const terminal=['succeeded','failed','cancelled','stopped'].includes(task.status);
  show($('result-panel'),terminal||Boolean(task.result));if(!terminal&&!task.result)return;
  const result=task.result||{},verification=result.verification||{};const demo=task.mode==='demo';
  setText('result-title',task.status==='succeeded'?(demo?'演示流程已完成':task.mode==='example'?'Docker 环境验证完成':'项目验证完成'):task.status==='failed'?'任务需要处理':task.status==='stopped'?'容器已停止':'任务已取消');
  setText('verification-tag',demo?'模拟结果':verification.passed?'HTTP 验证通过':'未确认可用');
  setText('result-description',demo?'这是预设演示结果，没有真实下载代码、执行安装或启动服务。可查看记录和导出示例复现包。':task.status==='succeeded'?(task.mode==='example'?'内置样例已完成真实 Docker 构建、启动、端口映射与 HTTP 检查。可打开容器服务查看结果；这不代表已安装任何 GitHub 项目。':'服务已完成当前方案中的 HTTP 检查。请继续打开应用，验证具体业务功能。'):task.status==='stopped'?'运行容器已停止，方案与执行记录仍保留，可下载复现包。':task.status==='cancelled'?'任务已停止执行。已经产生的方案与日志仍保留。':'请查看错误与日志。可以让 Agent 分析失败原因并生成新方案，审阅后再尝试执行。');
  $('result-metrics').replaceChildren();
  addMetric('执行模式',demo?'演示 / 模拟':task.mode==='example'?'内置样例 / 真实 Docker':'GitHub 项目 / Docker');
  addMetric('修复次数',String(task.attempt||0)+' / 2');
  if(verification.status_code)addMetric('HTTP 响应',verification.status_code);
  if(result.duration_seconds!==undefined)addMetric('执行耗时',`${Number(result.duration_seconds).toFixed(1)} s`);
  const url=!demo&&task.status==='succeeded'?safeAppUrl(result.url):null;
  show($('open-app'),Boolean(url));if(url)$('open-app').href=url;else $('open-app').removeAttribute('href');
  $('export-button').href=`/api/tasks/${encodeURIComponent(task.id)}/export`;show($('export-button'),Boolean(task.plan));
  const repair=task.mode==='real'&&task.status==='failed'&&Boolean(task.plan)&&(task.attempt||0)<2;
  show($('repair-button'),repair);show($('repair-note'),repair);$('repair-button').disabled=state.busy;
  show($('stop-button'),!demo&&['succeeded','failed'].includes(task.status)&&Boolean(task.plan));$('stop-button').disabled=state.busy;
}
function renderEvents(task){
  const events=task.events||[];setText('event-count',`${events.length} 条`);const active=activeStatuses.has(task.status);
  $('console-dot').classList.toggle('active',active);setText('console-label',active?'正在接收记录':task.status==='awaiting_approval'?'等待确认':'执行记录已保存');
  const signature=`${task.id}:${events.length}:${events.at(-1)?.id}:${events.at(-1)?.message}`;
  if(signature===state.eventSignature)return;state.eventSignature=signature;
  const console=$('event-console');const nearBottom=console.scrollHeight-console.scrollTop-console.clientHeight<90;
  console.replaceChildren();
  if(!events.length){console.append(element('p','console-empty','等待第一条执行记录…'));return;}
  for(const event of events){const row=element('div','event-row '+(event.level||'info'));row.append(element('span','event-time',timeLabel(event.time||event.timestamp)),element('span','event-message',event.message||''));console.append(row);}
  if(nearBottom||events.length<8)console.scrollTop=console.scrollHeight;
}
function renderTask(){
  const task=state.selected;if(!task)return;
  setText('breadcrumb-current',taskName(task));setText('task-name',taskName(task));setText('task-repository',task.mode==='example'?'来源：RepoLaunch 内置验证样例 · Python 标准库 · 无外部仓库':task.repository_url||'');
  show($('demo-notice'),task.mode==='demo');show($('example-notice'),task.mode==='example');setText('task-status',taskStatus(task));$('task-status').className=`status-badge ${task.status}`;
  show($('task-error'),Boolean(task.error));setText('task-error',task.error?readable(task.error):'');show($('cancel-analysis'),activeStatuses.has(task.status)&&!task.plan);$('cancel-analysis').disabled=state.busy;renderStepper(task);fillPlan(task);renderEvents(task);renderResult(task);
}
async function createTask(mode){
  if(state.busy)return;const input=$('repository-url');const url=mode==='demo'?'https://github.com/repolaunch-demo/hello-web':mode==='example'?'':input.value.trim();
  if(mode==='real'&&!url){setText('create-error','请先填写公开 GitHub 仓库地址。');show($('create-error'),true);input.focus();return;}
  state.busy=true;$('analyze-button').disabled=true;$('demo-button').disabled=true;$('docker-check-button').disabled=true;show($('create-error'),false);
  try{if(mode==='example')await refreshSystem();const task=await api('/api/tasks',{method:'POST',body:JSON.stringify({repository_url:url,mode})});state.selected=task;state.planSignature='';state.eventSignature='';show($('home'),false);show($('task-view'),true);updateTask(task);}
  catch(error){setText('create-error',error.message);show($('create-error'),true);}
  finally{state.busy=false;$('analyze-button').disabled=false;$('demo-button').disabled=false;$('docker-check-button').disabled=false;if(state.selected)renderTask();}
}
async function taskAction(action,body={}){
  if(state.busy||!state.selected)return;const id=state.selected.id;state.busy=true;renderTask();
  try{const task=await api(`/api/tasks/${encodeURIComponent(id)}/${action}`,{method:'POST',body:JSON.stringify(body)});if(state.selected?.id===id)updateTask(task);else refreshTasks().catch(()=>{});}
  catch(error){toast(error.message);}
  finally{state.busy=false;if(state.selected)renderTask();}
}
function collectPlan(){
  if(state.selected?.mode==='example')return {...state.selected.plan};
  const form=$('plan-form');if(!form.reportValidity())return null;
  const values=Object.fromEntries(new FormData(form));const plan={...state.selected.plan,...values};
  plan.port=Number(values.port);plan.extra_packages=String(values.extra_packages||'').split(',').map(x=>x.trim()).filter(Boolean);return plan;
}
async function openSettings(){
  await refreshSystem();const form=$('settings-form');
  if(state.system?.model?.base_url)form.elements.base_url.value=state.system.model.base_url;
  if(state.system?.model?.model)form.elements.model.value=state.system.model.model;
  form.elements.api_key.value='';form.elements.remember.checked=false;form.elements.use_for_embeddings.checked=false;form.elements.embedding_model.value=state.system?.model?.embedding?.model||'BAAI/bge-m3';show($('embedding-model-field'),false);show($('settings-error'),false);$('settings-dialog').showModal();
}
async function poll(){
  if(state.pollBusy)return;const task=state.selected;if(!task||!activeStatuses.has(task.status))return;
  state.pollBusy=true;
  try{const data=await api(`/api/tasks/${encodeURIComponent(task.id)}`);if(state.selected?.id===task.id)updateTask(data);show($('connection-error'),false);}
  catch(error){setText('connection-error',`连接暂时中断，将继续重试：${error.message}`);show($('connection-error'),true);}
  finally{state.pollBusy=false;}
}
$('create-form').addEventListener('submit',event=>{event.preventDefault();createTask('real');});
$('demo-button').addEventListener('click',()=>createTask('demo'));
$('docker-check-button').addEventListener('click',()=>createTask('example'));
$('new-task').addEventListener('click',openHome);
$('settings-open').addEventListener('click',openSettings);$('environment-settings').addEventListener('click',openSettings);
$('settings-close').addEventListener('click',()=>{$('settings-form').elements.api_key.value='';$('settings-dialog').close();});
$('settings-dialog').addEventListener('close',()=>{$('settings-form').elements.api_key.value='';});
$('settings-form').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget;if(!form.reportValidity())return;
  const body=Object.fromEntries(new FormData(form));body.remember=form.elements.remember.checked;body.use_for_embeddings=form.elements.use_for_embeddings.checked;if(!body.api_key)delete body.api_key;
  $('settings-save').disabled=true;show($('settings-error'),false);
  try{const result=await api('/api/settings',{method:'PUT',body:JSON.stringify(body)});form.elements.api_key.value='';form.elements.remember.checked=false;form.elements.use_for_embeddings.checked=false;show($('embedding-model-field'),false);await refreshSystem();toast(result.message||'模型配置已保存；可手动测试连接。');}
  catch(error){setText('settings-error',error.message);show($('settings-error'),true);await refreshSystem();}
  finally{$('settings-save').disabled=false;}
});
$('reuse-embedding').addEventListener('change',event=>show($('embedding-model-field'),event.target.checked));
$('model-check').addEventListener('click',async()=>{
  const form=$('settings-form');
  if(form.elements.api_key.value||form.elements.base_url.value.replace(/\/$/,'')!==state.system?.model?.base_url||form.elements.model.value.trim()!==state.system?.model?.model){setText('settings-error','上方配置有未保存的修改，请先保存再测试。');show($('settings-error'),true);return;}
  $('model-check').disabled=true;$('settings-save').disabled=true;setText('model-connection','正在测试已保存配置，等待模型响应…');show($('settings-error'),false);
  try{const result=await api('/api/settings/check',{method:'POST',body:'{}'});setText('model-connection',result.ok?`连接已验证 · ${result.model||''} · ${result.duration_ms??'—'} ms`:`连接未通过：${result.message||result.error?.message||'请检查服务配置'}`);await refreshSystem();}
  catch(error){setText('settings-error',error.message);show($('settings-error'),true);}
  finally{$('model-check').disabled=!state.system?.model?.configured;$('settings-save').disabled=false;}
});
$('approve-button').addEventListener('click',()=>{const plan=collectPlan();if(plan)taskAction('approve',{plan});});
$('cancel-button').addEventListener('click',()=>taskAction('cancel'));
$('cancel-analysis').addEventListener('click',()=>taskAction('cancel'));
$('repair-button').addEventListener('click',()=>taskAction('repair'));
$('stop-button').addEventListener('click',()=>taskAction('stop'));
$('copy-logs').addEventListener('click',async()=>{const logs=(state.selected?.events||[]).map(e=>`[${timeLabel(e.time||e.timestamp)}] ${e.message}`).join('\n');try{await navigator.clipboard.writeText(logs);toast('执行记录已复制。');}catch{toast('浏览器未允许复制，请直接选择日志文本。');}});
$('plan-form').addEventListener('submit',event=>event.preventDefault());
$('plan-form').elements.install_mode.addEventListener('change',event=>{$('plan-form').elements.requirements_file.disabled=event.target.value!=='requirements';});
document.addEventListener('keydown',event=>{if(event.key.toLowerCase()==='n'&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)&&!$('settings-dialog').open){event.preventDefault();openHome();}});
(async()=>{await refreshSystem();try{await refreshTasks();}catch(error){setText('connection-error',`暂时无法读取工作空间：${error.message}`);show($('connection-error'),true);}})();
setInterval(poll,1500);setInterval(()=>{if(!state.busy)refreshTasks().catch(()=>{});},12000);
