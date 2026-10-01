"""QQ bot control plane at /. Only the internal OneBot endpoint submits messages."""
import asyncio
import json
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from config.api_config import config, DEFAULT_CONFIG, save_config
from utils.paths import DATA_DIR, PROJECT_DIR
from app.auth import Auth, COOKIE
from app.runtime import Runtime
from app.schemas import Login,PasswordChange,MemoryInput,ImportInput,ConfigInput,QQInput,PauseInput,NoteInput,NoteEditInput,MaintenanceInput,LinkInput
from qq_bot import get_manifest

SECRET_KEYS={'primary_api_key','secondary_api_key','napcat_token'}
PAGES={'/','/login','/qq','/history','/memories','/settings','/maintenance','/assets-library','/notes'}

class ManagementLogHandler(logging.Handler):
    def __init__(self):
        super().__init__(); self.pending=__import__('collections').deque(maxlen=500)
    def emit(self, record):
        text=self.format(record)
        from utils.logging import redact
        text=redact(text)
        self.pending.append({'level':record.levelname,'message':text[:6000]})


def validate_settings(values):
    if not set(values)<=set(DEFAULT_CONFIG): raise ValueError('配置包含未支持的字段')
    from zoneinfo import ZoneInfo
    for key,value in values.items():
        default=DEFAULT_CONFIG[key]
        if key in ('sleep_start_hour','sleep_end_hour'):
            if value is not None and (type(value) is not int or not 0<=value<=23): raise ValueError('睡眠小时需要 0–23 或空值')
        elif isinstance(default,bool):
            if type(value) is not bool: raise ValueError(key+' 必须是布尔值')
        elif isinstance(default,(float,int)):
            if type(value) not in (float,int) or not math.isfinite(value) or value<=0: raise ValueError(key+' 必须是正数')
            if isinstance(default,int) and type(value) is not int: raise ValueError(key+' 必须是整数')
        elif not isinstance(value,str) or len(value)>2000:
            raise ValueError(key+' 必须是长度有限的字符串')
        if key.endswith('base_url'):
            url=urlparse(value)
            if url.scheme not in ('http','https') or not url.netloc or url.username or url.password or url.query or url.fragment:
                raise ValueError('模型与内部服务地址必须是有效 HTTP(S) 地址')
        if key=='timezone':
            try: ZoneInfo(value)
            except (KeyError,ValueError): raise ValueError('未知显示时区')
        if key in ('bot_qq','active_group_id') and (not value.isdigit() or len(value)>20): raise ValueError('QQ 号和群号必须为 1–20 位数字')
        if key=='napcat_token' and value and len(value)<16: raise ValueError('NapCat token 至少 16 个字符')
    proposed=dict(config)|values
    if proposed['biorhythm_wake_threshold']>=proposed['biorhythm_onset_threshold'] or proposed['biorhythm_onset_threshold']>1:
        raise ValueError('睡眠阈值须满足 0 < 醒来阈值 < 入睡阈值 ≤ 1')
    limits={'embedding_dimension':8192,'max_queue_size':10000,'max_media_count':20,'max_media_bytes':64*1024*1024,
            'action_page_size':100,'action_max_pages':10,'active_interval_seconds':86400,'model_timeout_seconds':120,'reply_ttl_seconds':86400}
    for key,maximum in limits.items():
        if proposed[key]>maximum: raise ValueError(key+' 超过上限')
    if proposed['biorhythm_rhythm_min_nights']>proposed['biorhythm_rhythm_full_nights']: raise ValueError('作息充分积累夜数不能少于最少积累夜数')
    if proposed['biorhythm_rhythm_weight']>1: raise ValueError('作息惯性权重不能大于 1')
    if proposed['active_interval_seconds']<10: raise ValueError('主动行为间隔至少 10 秒')
    if proposed['active_enabled'] and proposed['active_group_id'] not in get_manifest().whitelist:
        raise ValueError('主动目标群必须在白名单内')
    if (proposed['sleep_start_hour'] is None)!=(proposed['sleep_end_hour'] is None):
        raise ValueError('固定睡眠窗口需同时设置开始和结束小时')
    return proposed


def create_app(probe_models=True):
    @asynccontextmanager
    async def lifespan(app):
        runtime=Runtime(probe_models)
        app.state.runtime=runtime
        handler=ManagementLogHandler()
        logging.getLogger().addHandler(handler)
        try:
            await runtime.start()
            app.state.auth=Auth(runtime.repo)
            async def flush_logs():
                while True:
                    await asyncio.sleep(.5)
                    for _ in range(min(50,len(handler.pending))):
                        runtime.repo.event('log',handler.pending.popleft())
            logs=asyncio.create_task(flush_logs())
            yield
        finally:
            logging.getLogger().removeHandler(handler)
            if 'logs' in locals():
                logs.cancel(); await asyncio.gather(logs,return_exceptions=True)
            if runtime.repo: await runtime.stop()

    app=FastAPI(title='Nascence 辉夜 QQ 管理',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    def authorized(request:Request):
        return request.app.state.auth.session(request,request.method not in ('GET','HEAD','OPTIONS'))
    def runtime(request:Request): return request.app.state.runtime
    def core_ready(rt):
        if not rt.ready: raise HTTPException(503,rt.error or 'QQ 核心尚未就绪')

    @app.middleware('http')
    async def headers(request,call_next):
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['X-Frame-Options']='DENY'
        if request.url.path.startswith('/api/') or request.url.path=='/login': response.headers['Cache-Control']='no-store'
        return response

    @app.exception_handler(ValueError)
    async def invalid(request,exc): return JSONResponse({'detail':str(exc)},status_code=422)
    @app.exception_handler(KeyError)
    async def missing(request,exc): return JSONResponse({'detail':'记录不存在'},status_code=404)

    @app.post('/api/v1/auth/login')
    async def login(body:Login,request:Request):
        token,csrf=await request.app.state.auth.login(request,body.name,body.password)
        response=JSONResponse({'name':body.name,'csrf':csrf})
        response.set_cookie(COOKIE,token,httponly=True,secure=request.url.scheme=='https',samesite='strict',max_age=43200,path='/')
        return response

    @app.get('/api/v1/auth/session')
    async def session(request:Request,user=Depends(authorized)):
        return {'name':user['name'],'csrf':user['csrf']}

    @app.post('/api/v1/auth/logout')
    async def logout(request:Request,user=Depends(authorized)):
        request.app.state.runtime.repo.execute('DELETE FROM admin_sessions WHERE token_hash=?',(user['token_hash'],))
        response=JSONResponse({'ok':True}); response.delete_cookie(COOKIE,path='/'); return response

    @app.put('/api/v1/auth/password')
    async def password(body:PasswordChange,request:Request,user=Depends(authorized)):
        await request.app.state.auth.change_password(request,body.old_password,body.new_password)
        response=JSONResponse({'ok':True}); response.delete_cookie(COOKIE,path='/'); return response

    @app.get('/api/v1/status')
    async def status(rt=Depends(runtime),user=Depends(authorized)): return rt.status()

    @app.get('/health/live')
    async def live(): return {'alive':True}
    @app.get('/health/ready')
    async def ready(rt=Depends(runtime)):
        return JSONResponse({'ready':rt.ready},status_code=200 if rt.ready else 503)

    @app.websocket('/internal/onebot/ws')
    async def onebot(websocket:WebSocket): await websocket.app.state.runtime.adapter.handle(websocket)

    @app.get('/api/v1/qq')
    async def qq(rt=Depends(runtime),user=Depends(authorized)):
        m=get_manifest(); return {'whitelist_groups':sorted(m.whitelist),'name_mapping':m.name_map,
                                  'connected':rt.adapter.connected,'paused':rt.paused}
    @app.put('/api/v1/qq')
    async def update_qq(body:QQInput,rt=Depends(runtime),user=Depends(authorized)):
        if config.get('active_enabled') and str(config['active_group_id']) not in body.whitelist_groups:
            raise ValueError('先关闭主动发言或修改主动群，再移除该群的白名单')
        await rt.run_core(get_manifest().save,body.whitelist_groups,body.name_mapping)
        rt.repo.audit('qq.manifest',{'groups':body.whitelist_groups}); return {'ok':True}
    @app.post('/api/v1/qq/pause')
    async def pause(body:PauseInput,rt=Depends(runtime),user=Depends(authorized)):
        rt.pause(body.paused); return {'paused':rt.paused}

    @app.get('/api/v1/history')
    async def history(group_id:str|None=None,before:int|None=None,limit:int=100,rt=Depends(runtime),user=Depends(authorized)):
        return rt.repo.history(group_id,before,limit,include_internal=True)
    @app.get('/api/v1/history/jobs')
    async def jobs(rt=Depends(runtime),user=Depends(authorized)):
        return rt.repo.query('SELECT id,group_id,stage,error,attempts,created_at,updated_at FROM turn_jobs ORDER BY created_at DESC LIMIT 100')
    @app.get('/api/v1/history/deliveries')
    async def deliveries(rt=Depends(runtime),user=Depends(authorized)):
        return rt.repo.query('SELECT id,job_id,group_id,status,error,channel_message_id,created_at FROM delivery_outbox ORDER BY created_at DESC LIMIT 100')
    @app.post('/api/v1/history/jobs/{jid}/retry')
    async def retry(jid:str,rt=Depends(runtime),user=Depends(authorized)):
        rt.retry(jid); return {'ok':True}

    @app.post('/api/v1/deliveries/{oid}/retry')
    async def retry_delivery(oid:str,rt=Depends(runtime),user=Depends(authorized)):
        return {'accepted':await rt.retry_delivery(oid)}

    @app.get('/api/v1/memories')
    async def memories(q:str='',group_id:str|None=None,before:int|None=None,limit:int=50,rt=Depends(runtime),user=Depends(authorized)):
        return await rt.run_core(rt.memory.list,q,group_id,before,min(max(limit,1),200))
    @app.get('/api/v1/memories/export')
    async def export(rt=Depends(runtime),user=Depends(authorized)):
        rows=await rt.run_core(rt.memory.list,'',None,None,100000)
        return JSONResponse(rows,headers={'Content-Disposition':'attachment; filename="memories.json"'})
    @app.post('/api/v1/memories/import')
    async def import_memories(body:ImportInput,rt=Depends(runtime),user=Depends(authorized)):
        core_ready(rt)
        records=[r.model_dump() for r in body.records]
        result=await rt.run_core(rt.memory.import_records,records,body.apply)
        rt.repo.audit('memory.import',{'apply':body.apply,'count':len(records)}); return result
    @app.get('/api/v1/memories/{mid}')
    async def memory(mid:str,rt=Depends(runtime),user=Depends(authorized)):
        return await rt.run_core(rt.memory.detail,mid)
    @app.post('/api/v1/memories')
    async def add_memory(body:MemoryInput,rt=Depends(runtime),user=Depends(authorized)):
        core_ready(rt)
        mid=await rt.run_core(rt.memory.create,**body.model_dump())
        rt.repo.audit('memory.create',{'id':mid}); return {'id':mid}
    @app.put('/api/v1/memories/{mid}')
    async def edit_memory(mid:str,body:MemoryInput,rt=Depends(runtime),user=Depends(authorized)):
        core_ready(rt)
        result=await rt.run_core(rt.memory.edit,mid,**body.model_dump())
        rt.repo.audit('memory.edit',{'id':mid}); return result
    @app.delete('/api/v1/memories/{mid}')
    async def delete_memory(mid:str,rt=Depends(runtime),user=Depends(authorized)):
        await rt.run_core(rt.memory.delete,mid); rt.repo.audit('memory.delete',{'id':mid}); return {'ok':True}
    @app.get('/api/v1/concepts')
    async def concepts(rt=Depends(runtime),user=Depends(authorized)):
        return await rt.run_core(lambda: [dict(zip(('id','name','scope','group_id','member_count'),r)) for r in __import__('core.concept_store',fromlist=['_get_db'])._get_db().execute('SELECT id,name,scope,group_id,member_count FROM concepts ORDER BY last_accessed DESC LIMIT 200')])
    @app.post('/api/v1/links')
    async def links(body:LinkInput,rt=Depends(runtime),user=Depends(authorized)):
        from core import memory_engine as me
        def add():
            for mid in (body.src,body.tgt): rt.memory.detail(mid)
            with me.atomic_memories(): me.add_link(body.src,body.tgt,body.weight,body.type)
        await rt.run_core(add); rt.repo.audit('link.create',body.model_dump()); return {'ok':True}

    @app.get('/api/v1/settings')
    async def settings(rt=Depends(runtime),user=Depends(authorized)):
        return {'values':{k:v for k,v in config.items() if k not in SECRET_KEYS},
                'secrets_configured':{k:bool(config.get(k) and config[k]!='YOUR_API_KEY_HERE') for k in SECRET_KEYS}}
    @app.put('/api/v1/settings')
    async def update_settings(body:ConfigInput,rt=Depends(runtime),user=Depends(authorized)):
        def update():
            from core import memory_engine as me,concept_store
            fresh=validate_settings(body.values)
            count=me._get_db().execute('SELECT COUNT(*) FROM memories').fetchone()[0]
            count+=concept_store._get_db().execute('SELECT COUNT(*) FROM concepts').fetchone()[0]
            if count and any(fresh[k]!=config[k] for k in ('ollama_embed_model','embedding_dimension')):
                raise ValueError('已有记忆时需先使用 tools/reembed.py 进行受控向量迁移')
            from core.biorhythm import BIORHYTHM
            BIORHYTHM.tick()
            save_config(fresh)
            BIORHYTHM.configure(); BIORHYTHM.save()
            if not count:
                me._get_db().execute('INSERT OR REPLACE INTO core_metadata VALUES(?,?)',('embedding_model',json.dumps(fresh['ollama_embed_model'])))
                me._get_db().commit()
            me.OLLAMA_BASE_URL=fresh['ollama_base_url']; me.OLLAMA_EMBED_MODEL=fresh['ollama_embed_model']
            me.EMBED_DIM=concept_store.EMBED_DIM=fresh['embedding_dimension']
            from core.llm_interface import refresh_clients
            refresh_clients()
            return fresh
        await rt.run_core(update)
        rt.repo.audit('settings.update',{'keys':sorted(body.values)})
        rt.initializer=asyncio.create_task(rt.initialize())
        return {'ok':True,'reinitializing':True}

    @app.get('/api/v1/assets')
    async def asset_list(group_id:str,rt=Depends(runtime),user=Depends(authorized)):
        from core import asset_library as assets
        def read():
            with assets.group_storage(group_id):
                return assets.ordered_pool('image')+assets.ordered_pool('sticker')
        result=await rt.run_core(read)
        return [{k:v for k,v in row.items() if k!='path'} for row in result]
    @app.post('/api/v1/assets')
    async def upload_asset(group_id:str=Form(...),kind:str=Form(...),desc:str=Form(...),file:UploadFile=File(...),rt=Depends(runtime),user=Depends(authorized)):
        import tempfile
        from PIL import Image
        from core import asset_library as assets
        if not group_id.isdigit() or kind not in ('image','sticker') or not desc.strip() or len(desc)>500:
            raise ValueError('群号、素材类型或描述无效')
        staging=DATA_DIR/'upload_staging'; staging.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(dir=staging,suffix=Path(file.filename or '').suffix[:10])
        path=Path(name); total=0
        try:
            with __import__('os').fdopen(fd,'wb') as stream:
                while chunk:=await file.read(65536):
                    total+=len(chunk)
                    if total>min(assets.MAX_FILE_BYTES,int(config['max_media_bytes'])):
                        raise HTTPException(413,'素材超过大小限制')
                    stream.write(chunk)
            def store():
                try:
                    with Image.open(path) as image:
                        if image.width*image.height>25000000: raise ValueError('图片尺寸过大')
                        image.verify()
                except (OSError,Image.DecompressionBombError) as exc: raise ValueError('素材不是有效图片') from exc
                with assets.group_storage(group_id):
                    result=assets.add_asset(kind,str(path),desc)
                    if not result: raise ValueError('素材保存失败')
                    return {k:v for k,v in result.items() if k!='path'}
            result=await rt.run_core(store)
            rt.repo.audit('asset.upload',{'group':group_id,'kind':kind,'desc':desc})
            return result
        finally:
            await file.close(); path.unlink(missing_ok=True)

    @app.get('/api/v1/media/{asset_id}')
    async def media(asset_id:str,group_id:str,kind:str,rt=Depends(runtime),user=Depends(authorized)):
        from core import asset_library as assets
        if kind not in ('image','sticker'): raise ValueError('无效素材类型')
        def read():
            with assets.group_storage(group_id): return assets.resolve_token(kind,asset_id)
        entry=await rt.run_core(read)
        if not entry: raise KeyError(asset_id)
        path=Path(entry['path']).resolve(); root=(DATA_DIR/'groups'/group_id/'assets').resolve()
        if not path.is_relative_to(root): raise HTTPException(403,'素材路径无效')
        return FileResponse(path)
    @app.delete('/api/v1/assets/{asset_id}')
    async def delete_asset(asset_id:str,group_id:str,kind:str,rt=Depends(runtime),user=Depends(authorized)):
        from core import asset_library as assets
        def remove():
            with assets.group_storage(group_id):
                entry=assets.resolve_token(kind,asset_id)
                if not entry: raise KeyError(asset_id)
                with assets._lock:
                    assets._load()[kind].pop(entry['desc'],None); assets._save()
                    path=Path(entry['path']).resolve()
                    if not path.is_relative_to((DATA_DIR/'groups'/group_id/'assets').resolve()): raise ValueError('素材路径无效')
                    path.unlink(missing_ok=True)
        if kind not in ('image','sticker'): raise ValueError('无效素材类型')
        await rt.run_core(remove); rt.repo.audit('asset.delete',{'id':asset_id,'group':group_id}); return {'ok':True}

    @app.get('/api/v1/notes')
    async def notes(group_id:str,rt=Depends(runtime),user=Depends(authorized)):
        from core import asset_library as assets
        def read():
            with assets.group_storage(group_id):
                return [{k:v for k,v in (assets.read_note(n) or {'name':n,'text':''}).items() if k!='path'} for n in assets.list_notes()]
        return await rt.run_core(read)
    @app.post('/api/v1/notes')
    async def note(body:NoteInput,rt=Depends(runtime),user=Depends(authorized)):
        from core import asset_library as assets
        def write():
            with assets.group_storage(body.group_id):
                path=assets.write_note(body.name,body.text)
                if not path: raise ValueError('笔记名称或内容无效')
        await rt.run_core(write); rt.repo.audit('note.append',{'group':body.group_id,'name':body.name}); return {'ok':True}
    @app.put('/api/v1/notes')
    async def edit_note(body:NoteEditInput,rt=Depends(runtime),user=Depends(authorized)):
        from core import asset_library as assets
        def edit():
            with assets.group_storage(body.group_id):
                result=assets.edit_note(body.name,body.old_text,body.new_text)
                if not result: raise ValueError('笔记条目不存在或修改失败')
                return {k:v for k,v in result.items() if k!='path'}
        result=await rt.run_core(edit)
        rt.repo.audit('note.edit',{'group':body.group_id,'name':body.name,'changed':result['changed']})
        return result

    @app.delete('/api/v1/notes/{name}')
    async def delete_note(name:str,group_id:str,rt=Depends(runtime),user=Depends(authorized)):
        from core import asset_library as assets
        def remove():
            with assets.group_storage(group_id):
                base=assets._safe_note_base(name)
                if not base: raise ValueError('笔记名称无效')
                (Path(assets.NOTE_DIR)/(base+'.txt')).unlink(missing_ok=True)
        await rt.run_core(remove); rt.repo.audit('note.delete',{'group':group_id,'name':name}); return {'ok':True}

    @app.get('/api/v1/metrics')
    async def metrics(rt=Depends(runtime),user=Depends(authorized)):
        from core.memory_engine import _export_metrics_counters
        from core.biorhythm import BIORHYTHM
        return {'counters':await rt.run_core(_export_metrics_counters),'energy':BIORHYTHM.energy,'sleeping':BIORHYTHM.is_asleep()}
    @app.get('/api/v1/audit')
    async def audit(rt=Depends(runtime),user=Depends(authorized)):
        return rt.repo.query('SELECT * FROM audit_events ORDER BY seq DESC LIMIT 100')
    @app.post('/api/v1/maintenance')
    async def maintenance(body:MaintenanceInput,rt=Depends(runtime),user=Depends(authorized)):
        from app.services.maintenance import backup, self_test, rebuild_indexes
        from utils.persistence import sleep_cleanup
        if body.kind=='initialize':
            rt.initializer=asyncio.create_task(rt.initialize()); return {'id':'initialization'}
        callback={'backup':backup,'self_test':self_test,'rebuild_indexes':rebuild_indexes,'sleep_cleanup':sleep_cleanup}[body.kind]
        if body.kind=='sleep_cleanup': core_ready(rt)
        oid=rt.maintenance(body.kind,callback); rt.repo.audit('maintenance.start',{'kind':body.kind,'id':oid}); return {'id':oid}
    @app.get('/api/v1/maintenance/backups')
    async def backups(user=Depends(authorized)):
        return [{'name':p.name,'bytes':p.stat().st_size} for p in sorted((DATA_DIR/'backups').glob('*.zip'),reverse=True)]
    @app.get('/api/v1/maintenance/backups/{name}')
    async def download_backup(name:str,user=Depends(authorized)):
        if not re.fullmatch(r'backup-[0-9a-f]+\.zip',name): raise ValueError('备份名称无效')
        path=DATA_DIR/'backups'/name
        if not path.is_file(): raise KeyError(name)
        return FileResponse(path,filename=name)

    @app.get('/api/v1/events')
    async def events(request:Request,after:int=0,rt=Depends(runtime),user=Depends(authorized)):
        last=request.headers.get('last-event-id')
        if last and last.isdigit(): after=int(last)
        async def stream():
            cursor=max(0,after)
            bounds=rt.repo.query('SELECT MIN(seq) lo, MAX(seq) hi FROM management_events')[0]
            if bounds['lo'] and cursor and cursor<bounds['lo']-1:
                cursor=bounds['hi']
                yield f'id: {cursor}\nevent: reset\ndata: {{"resync":true}}\n\n'
            started=time.monotonic()
            # A finite stream lets the server drain connections on shutdown;
            # EventSource reconnects with Last-Event-ID during normal operation.
            while not rt.stopping and time.monotonic()-started<30 and not await request.is_disconnected():
                try: request.app.state.auth.session(request)
                except HTTPException: break
                rows=rt.repo.events(cursor)
                for row in rows:
                    cursor=row['seq']
                    yield f"id: {cursor}\nevent: {row['kind']}\ndata: {row['payload']}\n\n"
                if not rows: yield ': heartbeat\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    dist=PROJECT_DIR/'frontend/dist'
    if (dist/'assets').is_dir(): app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
    for page in PAGES:
        async def render(request:Request):
            if request.url.path!='/login':
                try: request.app.state.auth.session(request)
                except HTTPException: return RedirectResponse('/login',status_code=303)
            if not (dist/'index.html').exists(): return JSONResponse({'detail':'请先构建管理前端：cd frontend && npm ci && npm run build'},status_code=503)
            return FileResponse(dist/'index.html',headers={'Cache-Control':'no-store'})
        app.add_api_route(page,render,methods=['GET'],include_in_schema=False)
    return app

app=create_app()
