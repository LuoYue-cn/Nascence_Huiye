<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
type Row = Record<string, any>
const confirm = (message:string) => window.confirm(message)
const page=ref(location.pathname), logged=ref(false), csrf=ref(''), busy=ref(false), error=ref(''), notice=ref('')
const loginForm=reactive({name:'admin',password:''}), passwordForm=reactive({old_password:'',new_password:''})
const status=ref<Row>({jobs:{},maintenance:{}}), qq=ref<Row>({whitelist_groups:[],name_mapping:{}})
const settings=ref<Row>({}), secretStatus=ref<Row>({}), secrets=reactive<Row>({primary_api_key:'',secondary_api_key:'',napcat_token:''})
const group=ref(''), groupsText=ref(''), namesText=ref('{}'), query=ref(''), rows=ref<Row[]>([]), jobs=ref<Row[]>([]), deliveries=ref<Row[]>([])
const memories=ref<Row[]>([]), detail=ref<Row|null>(null), concepts=ref<Row[]>([]), logs=ref<Row[]>([]), audits=ref<Row[]>([]), backups=ref<Row[]>([]), metrics=ref<Row>({counters:{}})
const assets=ref<Row[]>([]), notes=ref<Row[]>([]), noteForm=reactive({name:'',text:''}), importText=ref('[]'), importPreview=ref<Row|null>(null), importRecords=ref<Row[]>([])
const memoryForm=reactive<Row>({id:'',content:'',half_life:172800,scope:'group',group_id:''})
const linkForm=reactive({src:'',tgt:'',weight:0.8,type:'semantic'})
const assetForm=reactive({kind:'image',desc:''}), assetFile=ref<File|null>(null)
const editNote=reactive({name:'',old_text:'',new_text:''})
let events:EventSource|null=null, poll:number|undefined
const navigation=[['/','运行总览','◈'],['/qq','QQ 设置','◎'],['/history','消息与任务','≡'],['/memories','记忆与概念','◇'],['/assets-library','素材收藏','▧'],['/notes','笔记管理','▤'],['/settings','模型与行为','⚙'],['/maintenance','日志与维护','↻']]
const title=computed(()=>navigation.find(x=>x[0]===page.value)?.[1] || '管理登录')
const pending=computed(()=>['queued','processing','prepared','stored','generated'].reduce((n,k)=>n+(status.value.jobs?.[k]||0),0))
const fields=[
['primary_base_url','主模型服务地址'],['primary_model','主模型名称'],['secondary_base_url','多模态服务地址'],['secondary_model','多模态模型'],
['ollama_base_url','Ollama 地址'],['ollama_embed_model','向量模型'],['embedding_dimension','向量维度'],['bot_qq','机器人 QQ 号'],['active_group_id','主动发言群号'],
['active_enabled','允许主动行为'],['active_interval_seconds','主动行为间隔（秒）'],['timezone','显示与节律时区'],['sleep_start_hour','固定睡眠开始小时（可留空）'],['sleep_end_hour','固定睡眠结束小时（可留空）'],
['max_queue_size','消息队列上限'],['reply_ttl_seconds','回复有效期（秒）'],['model_timeout_seconds','模型请求超时（秒）'],['max_media_bytes','QQ 附件上限（字节）'],['max_media_count','单条附件数量上限'],
['action_enabled','允许素材和笔记动作'],['action_note_enabled','允许笔记动作'],['action_max_pages','素材翻页上限'],['action_page_size','每页素材数量'],
['biorhythm_wake_seconds','清醒压力时间常数（秒）'],['biorhythm_sleep_seconds','睡眠恢复时间常数（秒）'],['biorhythm_onset_threshold','入睡阈值'],['biorhythm_wake_threshold','醒来阈值'],
['biorhythm_rhythm_weight','作息惯性权重'],['biorhythm_idle_to_sleep','作息入睡安静时长（秒）'],['biorhythm_rhythm_min_nights','作息最少积累夜数'],['biorhythm_rhythm_full_nights','作息充分积累夜数'],['napcat_http_base_url','NapCat 内部 HTTP 地址']]
const stageLabels:Row={queued:'排队',processing:'处理输入',prepared:'提取完成',stored:'记忆已提交',generated:'回复已生成',finished:'完成',failed:'失败',unknown:'结果未知',delivered:'渠道已接受',expired:'已过期',ignored:'已忽略',pending:'待投递',sending:'等待确认',running:'进行中',completed:'已完成'}
function date(t:number|undefined){return t?new Date(t*1000).toLocaleString('zh-CN',{timeZone:status.value.timezone||'Asia/Taipei'}):'—'}
function label(s:string){return stageLabels[s]||s}
async function api(path:string,method='GET',data?:any){
  const headers:Record<string,string>={'X-CSRF-Token':csrf.value}
  let body:BodyInit|undefined
  if(data instanceof FormData) body=data
  else if(data!==undefined){headers['Content-Type']='application/json';body=JSON.stringify(data)}
  const response=await fetch('/api/v1'+path,{method,headers,body,credentials:'same-origin'})
  if(!response.ok){
    const problem=await response.json().catch(()=>({detail:'请求失败'}))
    if(response.status===401 && path!='/auth/login'){logged.value=false;events?.close();page.value='/login';history.replaceState({},'','/login')}
    throw new Error(typeof problem.detail==='string'?problem.detail:JSON.stringify(problem.detail))
  }
  return response.json()
}
async function perform(fn:()=>Promise<any>, success=''){
  if(busy.value)return
  busy.value=true;error.value='';notice.value=''
  try{await fn();if(success)notice.value=success}catch(e:any){error.value=e.message}finally{busy.value=false}
}
async function signIn(){await perform(async()=>{const data=await api('/auth/login','POST',loginForm);csrf.value=data.csrf;logged.value=true;loginForm.password='';page.value='/';history.replaceState({},'','/');await load();subscribe()},'登录成功')}
async function signOut(){await perform(async()=>{await api('/auth/logout','POST');logged.value=false;events?.close();page.value='/login';history.replaceState({},'','/login')})}
async function navigate(path:string){page.value=path;history.pushState({},'',path);error.value='';notice.value='';await perform(load)}
async function refreshStatus(){status.value=await api('/status')}
async function load(){
  await refreshStatus(); qq.value=await api('/qq')
  if(!group.value && qq.value.whitelist_groups.length)group.value=qq.value.whitelist_groups[0]
  if(page.value==='/qq'){groupsText.value=qq.value.whitelist_groups.join('\n');namesText.value=JSON.stringify(qq.value.name_mapping,null,2)}
  if(page.value==='/history')await loadHistory()
  if(page.value==='/memories'){await loadMemories();concepts.value=await api('/concepts')}
  if(page.value==='/settings'){const data=await api('/settings');settings.value=data.values;secretStatus.value=data.secrets_configured}
  if(page.value==='/maintenance' || page.value==='/'){
    metrics.value=await api('/metrics');backups.value=await api('/maintenance/backups');audits.value=await api('/audit')
  }
  if(page.value==='/assets-library'&&group.value)assets.value=await api('/assets?group_id='+encodeURIComponent(group.value))
  if(page.value==='/notes'&&group.value)notes.value=await api('/notes?group_id='+encodeURIComponent(group.value))
}
async function loadHistory(before?:number){
  const messages=await api('/history?limit=100'+(group.value?'&group_id='+encodeURIComponent(group.value):'')+(before?'&before='+before:''))
  rows.value=before?[...messages,...rows.value]:messages
  ;[jobs.value,deliveries.value]=await Promise.all([api('/history/jobs'),api('/history/deliveries')])
}
async function loadMemories(before?:number){
  const data=await api('/memories?limit=50&q='+encodeURIComponent(query.value)+(group.value?'&group_id='+encodeURIComponent(group.value):'')+(before?'&before='+before:''))
  memories.value=before?[...memories.value,...data]:data
}
async function saveQQ(){await perform(async()=>{await api('/qq','PUT',{whitelist_groups:groupsText.value.split(/[\s,，]+/).filter(Boolean),name_mapping:JSON.parse(namesText.value)});await load()},'QQ 配置已保存')}
async function togglePause(){await perform(async()=>{await api('/qq/pause','POST',{paused:!status.value.paused});await refreshStatus()},'运行状态已更新')}
async function saveSettings(){await perform(async()=>{
  const values:Row={...settings.value}
  for(const k of ['sleep_start_hour','sleep_end_hour'])if(values[k]==='')values[k]=null
  for(const [key,value]of Object.entries(secrets))if(value)values[key]=value
  await api('/settings','PUT',{values});for(const key in secrets)secrets[key]='';await load()
},'配置已保存，核心正在重新初始化')}
function clearMemory(){Object.assign(memoryForm,{id:'',content:'',half_life:172800,scope:'group',group_id:group.value});detail.value=null}
async function inspectMemory(row:Row){await perform(async()=>{detail.value=await api('/memories/'+row.id);Object.assign(memoryForm,detail.value)})}
async function saveMemory(){await perform(async()=>{
  const data={content:memoryForm.content,half_life:Number(memoryForm.half_life),scope:memoryForm.scope,group_id:memoryForm.scope==='group'?memoryForm.group_id:null}
  await api('/memories'+(memoryForm.id?'/'+memoryForm.id:''),memoryForm.id?'PUT':'POST',data);clearMemory();await loadMemories()
},'记忆已保存并同步索引')}
async function removeMemory(row:Row){if(!confirm('删除这条记忆及其关联引用？'))return;await perform(async()=>{await api('/memories/'+row.id,'DELETE');clearMemory();await loadMemories()},'记忆已删除')}
async function previewImport(){await perform(async()=>{
  const parsed=JSON.parse(importText.value);if(!Array.isArray(parsed))throw new Error('导入文件应为 JSON 数组')
  importRecords.value=parsed.map(r=>({content:r.content,half_life:r.half_life||172800,scope:r.scope,group_id:r.group_id||null}))
  importPreview.value=await api('/memories/import','POST',{records:importRecords.value,apply:false})
})}
async function applyImport(){await perform(async()=>{await api('/memories/import','POST',{records:importRecords.value,apply:true});importPreview.value=null;await loadMemories()},'资料已导入')}
async function startMaintenance(kind:string){await perform(async()=>{await api('/maintenance','POST',{kind});await refreshStatus();backups.value=await api('/maintenance/backups')},'维护任务已提交，可在下方查看结果')}
async function appendNote(){await perform(async()=>{if(!group.value)throw new Error('先选择群');await api('/notes','POST',{group_id:group.value,...noteForm});noteForm.text='';await load()},'笔记已保存')}
async function modifyNote(){await perform(async()=>{await api('/notes','PUT',{group_id:group.value,...editNote});await load()},'笔记条目已修改')}
async function removeNote(name:string){if(!confirm('删除整份笔记？'))return;await perform(async()=>{await api('/notes/'+encodeURIComponent(name)+'?group_id='+group.value,'DELETE');await load()},'笔记已删除')}
async function uploadAsset(){await perform(async()=>{
  if(!group.value||!assetFile.value)throw new Error('选择群和素材文件')
  const data=new FormData();data.append('file',assetFile.value);data.append('group_id',group.value);data.append('kind',assetForm.kind);data.append('desc',assetForm.desc)
  await api('/assets','POST',data);assetForm.desc='';await load()
},'素材已加入该群收藏')}
async function removeAsset(row:Row){if(!confirm('删除这份素材？'))return;await perform(async()=>{await api('/assets/'+row.id+'?group_id='+group.value+'&kind='+row.kind,'DELETE');await load()},'素材已删除')}
async function changePassword(){await perform(async()=>{await api('/auth/password','PUT',passwordForm);logged.value=false;events?.close();page.value='/login';history.replaceState({},'','/login');passwordForm.old_password='';passwordForm.new_password=''},'密码已修改，请重新登录')}
function subscribe(){
  events?.close();events=new EventSource('/api/v1/events')
  events.addEventListener('log',(event:any)=>{logs.value.push(JSON.parse(event.data));if(logs.value.length>150)logs.value.shift()})
  for(const name of ['runtime','qq','maintenance','delivery','job','reset'])events.addEventListener(name,()=>refreshStatus().catch(()=>{}))
  events.addEventListener('maintenance',()=>api('/maintenance/backups').then(value=>{backups.value=value}).catch(()=>{}))
  events.addEventListener('error',(event:any)=>{if(event.data){try{error.value=JSON.parse(event.data).message}catch{}}})
}
onMounted(async()=>{
  try{const user=await api('/auth/session');csrf.value=user.csrf;logged.value=true;if(page.value==='/login'){page.value='/';history.replaceState({},'','/')}await load();subscribe()}catch{}
  poll=window.setInterval(()=>{if(logged.value)refreshStatus().catch(()=>{})},5000)
  window.addEventListener('popstate',()=>{page.value=location.pathname;perform(load)})
})
onUnmounted(()=>{events?.close();if(poll)clearInterval(poll)})
</script>

<template>
  <div v-if="!logged" class="login-layout">
    <div class="login-brand"><span class="brand-mark">辉</span><h1>辉夜</h1><p>QQ bot 管理面板</p><small>连接、记忆与运行状态，集中管理。</small></div>
    <form class="login-card" @submit.prevent="signIn"><span class="eyebrow">管理入口</span><h2>登录管理面板</h2><p class="muted">使用此部署的管理员凭据。</p>
      <label>用户名<input v-model="loginForm.name" autocomplete="username" required></label>
      <label>密码<input v-model="loginForm.password" type="password" autocomplete="current-password" required></label>
      <p v-if="error" class="error" role="alert">{{error}}</p><p v-if="notice" class="notice">{{notice}}</p>
      <button class="primary" :disabled="busy">{{busy?'正在验证…':'登录'}}</button>
    </form>
  </div>
  <div v-else class="shell">
    <aside><a class="brand" href="/" @click.prevent="navigate('/')"><span class="brand-mark">辉</span><div><strong>辉夜</strong><small>QQ BOT · 管理面板</small></div></a>
      <nav><a v-for="[path,name,icon] in navigation" :key="path" :href="path" :aria-label="name" :title="name" :class="{selected:page===path}" @click.prevent="navigate(path)"><span>{{icon}}</span>{{name}}</a></nav>
      <div class="sidebar-footer"><span class="dot" :class="{online:status.qq_connected}"></span>{{status.qq_connected?'NapCat 已连接':'NapCat 未连接'}}<small>管理员 · admin</small><button class="link" @click="signOut">退出登录</button></div>
    </aside>
    <main>
      <header><div><span class="eyebrow">NASCENCE HUIYE</span><h1>{{title}}</h1></div><div class="header-actions"><span class="badge">实时 1×</span><button @click="perform(load)" :disabled="busy">刷新</button></div></header>
      <p v-if="error" class="error" role="alert">{{error}}</p><p v-if="notice" class="notice" role="status">{{notice}}</p>
      <div v-if="status.error" class="warning"><strong>核心尚未就绪</strong><p>{{status.error}}</p><a href="/settings" @click.prevent="navigate('/settings')">检查配置 →</a></div>
      <template v-if="page==='/'">
        <div class="cards"><section class="stat"><small>QQ 连接</small><strong>{{status.qq_connected?'已连接':'等待连接'}}</strong><span>{{status.paused?'QQ 处理已暂停':'群消息接入'}}</span></section><section class="stat"><small>核心服务</small><strong>{{status.ready?'已就绪':'待配置'}}</strong><span>唯一认知协调器</span></section><section class="stat"><small>长期记忆</small><strong>{{status.memories||0}}</strong><span>数据库中的记忆</span></section><section class="stat"><small>待处理任务</small><strong>{{pending}}</strong><span>失败 {{status.jobs.failed||0}} · 完成 {{status.jobs.finished||0}}</span></section></div>
        <div class="two-columns"><section class="panel"><h2>运行控制</h2><p class="muted">暂停 QQ 处理后，管理与维护仍然可用。</p><div class="actions"><button @click="togglePause" :disabled="busy">{{status.paused?'恢复 QQ 处理':'暂停 QQ 处理'}}</button><button @click="startMaintenance('initialize')" :disabled="busy">重新初始化核心</button></div><dl><dt>主动行为</dt><dd>{{status.active_enabled?'已开启':'已关闭'}}</dd><dt>当前时间</dt><dd>{{date(status.time)}}</dd><dt>睡眠状态</dt><dd>{{metrics.sleeping?'睡眠中':'清醒'}}</dd><dt>精力</dt><dd>{{Math.round((metrics.energy||0)*100)}}%</dd></dl></section>
        <section class="panel"><h2>最近管理操作</h2><ul class="event-list"><li v-for="row in audits.slice(0,7)" :key="row.seq"><strong>{{row.action}}</strong><small>{{date(row.created_at)}}</small></li></ul><p v-if="!audits.length" class="empty">尚无管理操作记录</p></section></div>
      </template>
      <section v-if="page==='/qq'" class="panel"><h2>群接入与路由</h2><p class="muted">入站与投递都会检查白名单。群号每行一个。</p><form @submit.prevent="saveQQ"><div class="two-columns"><label>群白名单<textarea v-model="groupsText" rows="8" placeholder="123456789"></textarea></label><label>姓名映射（JSON）<textarea v-model="namesText" rows="8" spellcheck="false"></textarea></label></div><div class="actions"><button class="primary" :disabled="busy">保存 QQ 配置</button><button type="button" @click="togglePause" :disabled="busy">{{status.paused?'恢复 QQ':'暂停 QQ'}}</button></div></form><p class="muted">机器人账号、主动目标群和连接 token 在“模型与行为”中设置。</p></section>
      <template v-if="page==='/history'">
        <section class="panel"><div class="section-heading"><h2>QQ 消息记录</h2><select v-model="group" @change="perform(()=>loadHistory())"><option value="">全部群</option><option v-for="g in qq.whitelist_groups" :value="g">{{g}}</option></select></div><p class="muted">只读查看 QQ 中已发生的记录。</p><button v-if="rows.length" @click="perform(()=>loadHistory(rows[0].seq))" :disabled="busy">加载更早记录</button><div class="history-list"><article v-for="row in rows" :key="row.seq"><div><strong>{{row.sender||'未知'}}</strong><span class="badge">{{row.group_id||'归属未确认'}}</span><small>{{date(row.received_at)}} · {{row.role==='internal'?'内部行为':row.role==='bot'?'机器人':'群消息'}}</small></div><p>{{row.content}}</p><blockquote v-if="row.quote">引用 {{row.quote.sender}}：{{row.quote.text}}</blockquote></article><p v-if="!rows.length" class="empty">此群暂无消息记录</p></div></section>
        <section class="panel"><h2>处理任务</h2><div class="table-scroll"><table><thead><tr><th>时间</th><th>群</th><th>状态</th><th>说明</th><th></th></tr></thead><tbody><tr v-for="job in jobs" :key="job.id"><td>{{date(job.created_at)}}</td><td>{{job.group_id}}</td><td><span class="badge">{{label(job.stage)}}</span></td><td>{{job.error||'—'}}</td><td><button v-if="job.stage==='failed'" @click="perform(async()=>{await api('/history/jobs/'+job.id+'/retry','POST');await loadHistory()},'已提交重试')">重试</button></td></tr></tbody></table></div><p v-if="!jobs.length" class="empty">暂无处理任务</p></section>
        <section class="panel"><h2>投递确认</h2><div class="table-scroll"><table><thead><tr><th>时间</th><th>群</th><th>状态</th><th>说明</th></tr></thead><tbody><tr v-for="row in deliveries" :key="row.id"><td>{{date(row.created_at)}}</td><td>{{row.group_id}}</td><td>{{label(row.status)}}</td><td>{{row.error||'—'}} <button v-if="row.status==='failed'" :disabled="busy" @click="perform(async()=>{await api('/deliveries/'+row.id+'/retry','POST');await loadHistory()},'投递重试已处理')">重试投递</button></td></tr></tbody></table></div><p v-if="!deliveries.length" class="empty">暂无投递记录</p></section>
      </template>
      <template v-if="page==='/memories'">
        <section class="panel"><div class="section-heading"><h2>记忆库</h2><button @click="clearMemory">添加记忆</button></div><div class="filters"><input v-model="query" placeholder="搜索记忆内容" @keyup.enter="perform(()=>loadMemories())"><select v-model="group"><option value="">全部范围</option><option v-for="g in qq.whitelist_groups" :value="g">{{g}}</option></select><button @click="perform(()=>loadMemories())">查询</button><a class="button" href="/api/v1/memories/export">导出</a></div><div class="table-scroll"><table><thead><tr><th>内容</th><th>群 / 范围</th><th>来源</th><th>时间</th><th>操作</th></tr></thead><tbody><tr v-for="row in memories" :key="row.id"><td class="content-cell">{{row.content}}</td><td>{{row.group_id||row.scope}}</td><td>{{row.source}}</td><td>{{row.time_basis==='unix_utc'?date(row.creation_time):'时间未确认'}}</td><td><button class="link" @click="inspectMemory(row)">详情 / 编辑</button><button class="link danger" @click="removeMemory(row)">删除</button></td></tr></tbody></table></div><p v-if="!memories.length" class="empty">暂无匹配记忆</p><button v-if="memories.length" @click="perform(()=>loadMemories(memories[memories.length-1]._cursor))">加载更多</button></section>
        <section class="panel"><h2>{{memoryForm.id?'编辑记忆':'添加记忆'}}</h2><form @submit.prevent="saveMemory"><label>内容<textarea v-model="memoryForm.content" rows="3" required maxlength="20000"></textarea></label><div class="three-columns"><label>可见范围<select v-model="memoryForm.scope"><option value="group">仅指定群</option><option value="persona_shared">角色共享</option></select></label><label v-if="memoryForm.scope==='group'">群号<input v-model="memoryForm.group_id" required pattern="[0-9]+"></label><label>半衰期（秒）<input v-model.number="memoryForm.half_life" type="number" min="1" required></label></div><div class="actions"><button class="primary" :disabled="busy">保存记忆</button><button type="button" @click="clearMemory">清空表单</button></div></form><div v-if="detail"><p class="muted">ID：{{detail.id}}</p><h3>关联</h3><pre>{{JSON.stringify(detail.links,null,2)}}</pre><h3>所属概念</h3><pre>{{JSON.stringify(detail.concepts,null,2)}}</pre></div></section>
        <section class="panel"><h2>建立记忆关联</h2><form class="filters" @submit.prevent="perform(async()=>{await api('/links','POST',linkForm)},'关联已保存')"><input v-model="linkForm.src" placeholder="起点记忆 ID" required><input v-model="linkForm.tgt" placeholder="终点记忆 ID" required><input v-model.number="linkForm.weight" type="number" min="0.01" max="1" step="0.01"><select v-model="linkForm.type"><option value="semantic">语义</option><option value="causal">因果</option><option value="temporal">时序</option></select><button :disabled="busy">保存关联</button></form></section>
        <section class="panel"><h2>资料导入</h2><p class="muted">JSON 数组，每条包含 content、half_life、scope、group_id。先预览，再提交；每批最多 100 条。</p><textarea v-model="importText" rows="5" @input="importPreview=null" spellcheck="false"></textarea><div class="actions"><button @click="previewImport" :disabled="busy">预览导入</button><button v-if="importPreview" class="primary" @click="applyImport" :disabled="busy">确认导入 {{importPreview.new}} 条</button></div><p v-if="importPreview">总数 {{importPreview.total}} · 新增 {{importPreview.new}} · 重复 {{importPreview.duplicates}}</p></section>
        <section class="panel"><h2>概念索引</h2><div class="table-scroll"><table><thead><tr><th>概念</th><th>范围</th><th>记忆成员</th></tr></thead><tbody><tr v-for="row in concepts" :key="row.id"><td>{{row.name}}</td><td>{{row.group_id||row.scope}}</td><td>{{row.member_count}}</td></tr></tbody></table></div><p v-if="!concepts.length" class="empty">暂无概念</p></section>
      </template>
      <template v-if="page==='/settings'">
        <section class="panel"><h2>模型、QQ 与行为配置</h2><form @submit.prevent="saveSettings"><div class="settings-grid"><label v-for="[key,name] in fields" :key="key">{{name}}<input v-if="typeof settings[key]==='boolean'" v-model="settings[key]" type="checkbox"><input v-else-if="typeof settings[key]==='number'||key.startsWith('sleep_')" v-model.number="settings[key]" type="number" step="any"><input v-else v-model="settings[key]"></label><label v-for="key in Object.keys(secrets)" :key="key">{{key==='primary_api_key'?'主模型密钥':key==='secondary_api_key'?'多模态密钥':'NapCat token'}}<input v-model="secrets[key]" type="password" autocomplete="new-password" :placeholder="secretStatus[key]?'已配置，留空保持':'尚未配置'"></label></div><button class="primary" :disabled="busy">保存配置</button></form><p class="muted">有记忆时更换向量模型需先执行受控迁移。模型请求超时、主动频率和队列上限均有验证。</p></section>
        <section class="panel"><h2>修改管理员密码</h2><form @submit.prevent="changePassword"><div class="two-columns"><label>原密码<input v-model="passwordForm.old_password" type="password" autocomplete="current-password" required></label><label>新密码（至少 12 字符）<input v-model="passwordForm.new_password" type="password" autocomplete="new-password" minlength="12" required></label></div><button :disabled="busy">修改密码并重新登录</button></form></section>
      </template>
      <template v-if="page==='/assets-library'">
        <section class="panel"><div class="section-heading"><h2>群素材收藏</h2><select v-model="group" @change="perform(load)"><option v-for="g in qq.whitelist_groups" :value="g">{{g}}</option></select></div><div class="asset-grid"><article v-for="row in assets" :key="row.id"><img :src="'/api/v1/media/'+row.id+'?group_id='+group+'&kind='+row.kind" :alt="row.desc" loading="lazy"><strong>{{row.desc}}</strong><small>{{row.kind==='sticker'?'表情包':'图片'}} · {{row.id}}</small><button @click="removeAsset(row)">删除</button></article></div><p v-if="!assets.length" class="empty">此群尚无收藏</p></section>
        <section class="panel"><h2>添加素材</h2><form @submit.prevent="uploadAsset"><div class="three-columns"><label>类型<select v-model="assetForm.kind"><option value="image">图片</option><option value="sticker">表情包</option></select></label><label>内容描述<input v-model="assetForm.desc" required maxlength="500"></label><label>文件<input type="file" accept="image/*" @change="assetFile=($event.target as HTMLInputElement).files?.[0]||null" required></label></div><button class="primary" :disabled="busy">加入收藏</button></form></section>
      </template>
      <template v-if="page==='/notes'">
        <section class="panel"><div class="section-heading"><h2>群笔记</h2><select v-model="group" @change="perform(load)"><option v-for="g in qq.whitelist_groups" :value="g">{{g}}</option></select></div><article v-for="row in notes" :key="row.name" class="note"><h3>{{row.name}}</h3><pre>{{row.text}}</pre><button class="danger" @click="removeNote(row.name)">删除笔记</button></article><p v-if="!notes.length" class="empty">此群暂无笔记</p></section>
        <section class="panel"><h2>追加笔记</h2><form @submit.prevent="appendNote"><label>名称<input v-model="noteForm.name" required maxlength="32"></label><label>内容<textarea v-model="noteForm.text" rows="4" maxlength="1500" required></textarea></label><button class="primary" :disabled="busy">追加保存</button></form></section>
        <section class="panel"><h2>修改笔记条目</h2><form @submit.prevent="modifyNote"><label>名称<input v-model="editNote.name" required></label><label>原条目<input v-model="editNote.old_text" required></label><label>新条目（留空划掉）<input v-model="editNote.new_text"></label><button :disabled="busy">保存修改</button></form></section>
      </template>
      <template v-if="page==='/maintenance'">
        <section class="panel"><h2>维护操作</h2><div class="actions"><button @click="startMaintenance('backup')" :disabled="busy">创建完整备份</button><button @click="startMaintenance('rebuild_indexes')" :disabled="busy">重建索引</button><button @click="startMaintenance('self_test')" :disabled="busy">向量与数据库自检</button><button @click="confirm('执行记忆衰减清理？请先备份。')&&startMaintenance('sleep_cleanup')" :disabled="busy">执行维护清理</button></div><div v-for="(row,id) in status.maintenance" :key="id" class="operation"><strong>{{row.kind}}</strong><span class="badge">{{row.status}}</span><p>{{row.error||JSON.stringify(row.result||{})}}</p></div></section>
        <section class="panel"><h2>数据库与资料备份</h2><p class="muted">包含数据库、群素材、笔记与配置，下载后妥善保管。恢复需停服。</p><ul class="event-list"><li v-for="row in backups" :key="row.name"><a :href="'/api/v1/maintenance/backups/'+row.name">{{row.name}}</a><small>{{Math.round(row.bytes/1024)}} KB</small></li></ul><p v-if="!backups.length" class="empty">尚无备份；创建后刷新列表</p></section>
        <section class="panel"><h2>运行指标</h2><div class="metrics"><div v-for="(value,key) in metrics.counters" :key="key"><small>{{key}}</small><strong>{{value}}</strong></div></div></section>
        <section class="panel"><h2>实时日志</h2><div class="log-view"><div v-for="(row,i) in logs" :key="i"><span>{{row.level}}</span> {{row.message}}</div><p v-if="!logs.length">等待新的运行事件…</p></div></section>
        <section class="panel"><h2>管理审计</h2><ul class="event-list"><li v-for="row in audits" :key="row.seq"><strong>{{row.action}}</strong><small>{{date(row.created_at)}}</small><p>{{row.details}}</p></li></ul></section>
      </template>
      <footer>辉夜 · QQ bot 管理面板<span>{{status.timezone||'Asia/Taipei'}}</span></footer>
    </main>
  </div>
</template>
