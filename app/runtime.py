"""Process-owned QQ runtime; browser and NapCat connections never own the core."""
import asyncio
import contextvars
import functools
import json
import logging
import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from config.api_config import config
from utils.paths import DATA_DIR
from app.repositories.sqlite import Repository
from qq_bot import QQAdapter, DeliveryError, get_manifest, get_bot_qq, get_active_group_id

log=logging.getLogger(__name__)

class InstanceLock:
    def __init__(self, path): self.path=path; self.file=None
    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=open(self.path,'a+b')
        try:
            if os.name=='nt':
                import msvcrt
                self.file.seek(0); self.file.write(b'0'); self.file.flush(); self.file.seek(0)
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,BlockingIOError):
            self.file.close(); self.file=None
            raise RuntimeError('This data directory is already owned by another bot process')
    def release(self):
        if self.file:
            if os.name=='nt':
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
            self.file.close(); self.file=None

class Runtime:
    def __init__(self, probe_models=True):
        self.lock=InstanceLock(DATA_DIR/'.instance.lock')
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='huiye-core')
        self.repo=None; self.conversation=None; self.memory=None
        self.adapter=QQAdapter(self)
        self.ready=False; self.error='Initializing'; self.stopping=False
        self.paused=False; self.probe_models=probe_models
        self.worker=None; self.initializer=None
        self.last_active=time.monotonic(); self.last_save=time.monotonic()
        self.maintenance_jobs={}
        self._initialization_lock=asyncio.Lock()
        self._delivery_lock=asyncio.Lock()
        self._initializers=set()

    async def run_core(self, fn, *args, **kwargs):
        context=contextvars.copy_context()
        future=asyncio.get_running_loop().run_in_executor(self.executor,context.run,functools.partial(fn,*args,**kwargs))
        # Keep the executor operation alive and tracked if its HTTP requester disconnects.
        return await asyncio.shield(future)

    async def start(self):
        self.lock.acquire()
        try:
            self.repo=Repository()
            from app.services.memory import MemoryService
            from app.services.qq_conversation import QQConversationService
            self.memory=MemoryService(); self.conversation=QQConversationService(self.repo)
            settings=self.repo.query("SELECT value FROM settings WHERE key='qq_paused'")
            self.paused=bool(json.loads(settings[0]['value'])) if settings else False
            await self.run_core(self.repo.recover)
            self.worker=asyncio.create_task(self._worker(),name='qq-coordinator')
            self.initializer=asyncio.create_task(self.initialize(),name='core-initialization')
        except BaseException:
            self.lock.release(); raise

    def _initialize_core(self):
        from core import memory_engine as me
        from utils.persistence import load_all_data,load_state
        if (DATA_DIR/'offline_operation.json').exists(): raise RuntimeError('Interrupted offline migration: inspect offline_operation.json and restore its backup before starting')
        db=me._get_db()
        count=db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        stamp=db.execute("SELECT value FROM core_metadata WHERE key='time_schema'").fetchone()
        if not stamp and (count or (DATA_DIR/'memory.json').exists() or (DATA_DIR/'clock_state.txt').exists()):
            raise RuntimeError('Legacy data needs offline migration: tools/migrate_legacy.py --preview')
        if not stamp:
            db.execute("INSERT INTO core_metadata VALUES('time_schema','\"unix_v1\"')"); db.commit()
        load_all_data(); load_state()
        from core.llm_interface import refresh_clients
        refresh_clients()
        model=db.execute("SELECT value FROM core_metadata WHERE key='embedding_model'").fetchone()
        if model and json.loads(model[0]) != config['ollama_embed_model']:
            raise RuntimeError('Embedding model changed; run a controlled re-embedding migration')
        if self.probe_models:
            me.validate_vector(me.text_to_vector('服务启动自检'))
            if config.get('primary_api_key') in ('','YOUR_API_KEY_HERE',None):
                raise RuntimeError('Primary model credentials have not been configured')
        db.execute('INSERT OR REPLACE INTO core_metadata VALUES(?,?)',('embedding_model',json.dumps(config['ollama_embed_model'])))
        db.commit()
        return {'memories':count}

    async def initialize(self):
        task=asyncio.current_task()
        self._initializers.add(task)
        try:
            await self._initialize_locked()
        finally: self._initializers.discard(task)

    async def _initialize_locked(self):
        async with self._initialization_lock:
            self.ready=False
            try:
                result=await self.run_core(self._initialize_core)
                self.error=None; self.ready=True
                self.repo.event('runtime',{'ready':True,**result})
            except Exception as exc:
                self.error=str(exc)
                log.warning('Core not ready: %s',exc)
                self.repo.event('runtime',{'ready':False,'error':self.error})

    async def accept_message(self, data):
        if self.stopping or self.paused: return
        group=str(data.get('group_id','')); bot=str(data.get('self_id',''))
        if group not in get_manifest().whitelist or bot!=get_bot_qq(): return
        if str(data.get('user_id',''))==get_bot_qq(): return
        if not str(data.get('message_id','')) or not str(data.get('user_id','')).isdigit():
            raise ValueError('QQ message lacks a stable message ID or sender')
        sender=data.get('sender')
        if sender is None: sender={}
        if not isinstance(sender,dict) or any(value is not None and (not isinstance(value,str) or len(value)>200) for key,value in sender.items() if key in ('card','nickname')):
            raise ValueError('QQ sender metadata is invalid')
        message=data.get('message',data.get('raw_message',''))
        if isinstance(message,list):
            if len(message)>100 or any(not isinstance(segment,dict) or not isinstance(segment.get('data'),dict) or not isinstance(segment.get('type'),str) for segment in message):
                raise ValueError('QQ message segments are invalid')
            text=''.join(str(segment['data'].get('text','')) for segment in message if segment['type']=='text')
        elif isinstance(message,str): text=message
        else: raise ValueError('QQ message must be text or OneBot segments')
        if len(text)>20000: raise ValueError('QQ message text is too long')
        if type(data.get('message_id')) not in (int,str) or len(str(data['message_id']))>100: raise ValueError('QQ external message ID is invalid')
        jid,new=await asyncio.to_thread(self.repo.accept,data,int(config.get('max_queue_size',100)))
        if new: self.repo.event('job',{'id':jid,'stage':'queued'})

    def status(self):
        counts={r['stage']:r['n'] for r in self.repo.query('SELECT stage,COUNT(*) n FROM turn_jobs GROUP BY stage')}
        memories=self.repo.query("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name='memories'")
        count=self.repo.query('SELECT COUNT(*) n FROM memories')[0]['n'] if memories[0]['n'] else 0
        return {'ready':self.ready,'error':self.error,'qq_connected':self.adapter.connected,'paused':self.paused,
                'stopping':self.stopping,'jobs':counts,'memories':count,'time':time.time(),
                'timezone':config.get('timezone','Asia/Taipei'),'active_enabled':config.get('active_enabled',False),
                'maintenance':{k:{a:b for a,b in v.items() if a!='_task'} for k,v in self.maintenance_jobs.items()}}

    def pause(self, paused):
        self.paused=paused
        self.repo.execute("INSERT OR REPLACE INTO settings VALUES('qq_paused',?)",(json.dumps(paused),))
        self.repo.audit('qq.pause',{'paused':paused}); self.repo.event('runtime',{'paused':paused})

    async def _worker(self):
        while not self.stopping:
            try:
                from core.biorhythm import BIORHYTHM
                from core.virtual_clock import clock
                was_sleeping=BIORHYTHM.is_asleep()
                BIORHYTHM.tick()
                if clock.in_sleep_window() and not BIORHYTHM.is_asleep() and time.time()>=BIORHYTHM.awake_until:
                    BIORHYTHM.force_sleep('configured sleep window')
                if BIORHYTHM.is_asleep() and not was_sleeping and self.ready:
                    from utils.persistence import sleep_cleanup
                    await self.run_core(sleep_cleanup)
                if self.ready and not self.paused and self.adapter.connected:
                    cutoff=time.time()-float(config['reply_ttl_seconds'])
                    expired=self.repo.query("SELECT id FROM turn_jobs WHERE stage IN ('queued','prepared','stored','generated') AND created_at<?",(cutoff,))
                    for row in expired: self.repo.update_job(row['id'],'expired')
                    stages="('queued','prepared')" if BIORHYTHM.is_asleep() else "('queued','prepared','stored','generated')"
                    rows=self.repo.query(f'SELECT * FROM turn_jobs WHERE stage IN {stages} ORDER BY created_at LIMIT 1')
                    if rows:
                        await self._process(rows[0])
                        continue
                    # Recover durable pending deliveries after a clean reconnection.
                    pending=self.repo.query("SELECT * FROM delivery_outbox WHERE status='pending' ORDER BY created_at LIMIT 1")
                    if pending and not BIORHYTHM.is_asleep():
                        await self.deliver(pending[0]); continue
                    interval=float(config.get('active_interval_seconds',60))
                    if not BIORHYTHM.is_asleep() and config.get('active_enabled') and time.monotonic()-self.last_active>=interval:
                        self.last_active=time.monotonic()
                        group=get_active_group_id()
                        if group in get_manifest().whitelist:
                            jid='active-'+uuid.uuid4().hex
                            from core import memory_engine as me
                            await self.run_core(lambda: setattr(me,'_count_active_attempt',me._count_active_attempt+1))
                            self.repo.execute('''INSERT INTO turn_jobs(id,group_id,stage,created_at,updated_at)
                                VALUES(?,?,'stored',?,?)''',(jid,group,time.time(),time.time()))
                            await self._process(self.repo.job(jid))
                            continue
                if time.monotonic()-self.last_save>60 and self.ready:
                    from utils.persistence import save_all_data,save_state
                    await self.run_core(save_all_data); await self.run_core(save_state)
                    self.last_save=time.monotonic()
                await asyncio.sleep(.25)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception('QQ coordinator operation failed')
                self.repo.event('error',{'message':str(exc)})
                await asyncio.sleep(1)

    async def _process(self, job):
        jid=job['id']; temporary=[]
        from utils.session_context import current_deadline
        token=current_deadline.set(time.monotonic()+max(0,float(config['reply_ttl_seconds'])-(time.time()-job['created_at'])))
        try:
            if time.time()-job['created_at']>float(config.get('reply_ttl_seconds',300)):
                self.repo.update_job(jid,'expired'); return
            if job['stage']=='queued':
                self.repo.update_job(jid,'processing',attempts=job['attempts']+1)
                raw=json.loads(job['raw']); quote=None
                segments=raw.get('message') if isinstance(raw.get('message'),list) else []
                reply=next((s.get('data',{}).get('id') for s in segments if isinstance(s,dict) and s.get('type')=='reply'),None)
                if reply: quote=await self.adapter.quote(reply,job['group_id'])
                media,temporary=await self.adapter.describe_media(raw)
                await self.run_core(self.conversation.prepare,job,quote,media)
                job=self.repo.job(jid)
                if job['stage']=='ignored': return
            if job['stage']=='prepared':
                await self.run_core(self.conversation.commit,job); job=self.repo.job(jid)
            if job['stage']=='stored':
                from core.biorhythm import BIORHYTHM
                if BIORHYTHM.is_asleep():
                    await asyncio.sleep(.25)
                    return
                await self.run_core(self.conversation.think,job); job=self.repo.job(jid)
            result=json.loads(job['decision'] or '{}')
            sent=False
            if result.get('say') is True and result.get('text'):
                outbox=await self.run_core(self.conversation.create_outbox,job,result)
                sent=await self.deliver(outbox)
            self.repo.update_job(jid,'finished')
            if not self.stopping and (sent or job.get('raw')) and config.get('action_enabled'):
                media=await self.run_core(self.conversation.action,self.repo.job(jid),sent)
                if media: await self.deliver(media)
        except Exception as exc:
            self.repo.update_job(jid,'failed',error=str(exc))
            log.exception('QQ job %s failed',jid)
        finally:
            current_deadline.reset(token)
            for path in temporary: Path(path).unlink(missing_ok=True)

    async def deliver(self, outbox):
        async with self._delivery_lock:
            fresh=self.repo.query('SELECT * FROM delivery_outbox WHERE id=?',(outbox['id'],))
            if not fresh: raise KeyError(outbox['id'])
            return await self._deliver_locked(fresh[0])

    async def retry_delivery(self, oid):
        async with self._delivery_lock:
            rows=self.repo.query('SELECT * FROM delivery_outbox WHERE id=?',(oid,))
            if not rows: raise KeyError(oid)
            row=rows[0]
            if row['status']!='failed': raise ValueError('Only an explicitly rejected delivery may be retried; uncertain results are never replayed')
            if time.time()-row['created_at']>float(config['reply_ttl_seconds']): raise ValueError('Delivery has expired')
            self.repo.audit('delivery.retry',{'id':oid,'group':row['group_id']})
            return await self._deliver_locked(row)

    async def _deliver_locked(self, outbox):
        if outbox['status'] in ('delivered','unknown','expired'): return outbox['status']=='delivered'
        if time.time()-outbox['created_at']>float(config.get('reply_ttl_seconds',300)):
            self.repo.execute("UPDATE delivery_outbox SET status='expired' WHERE id=?",(outbox['id'],)); return False
        try:
            ack=await self.adapter.send(outbox['group_id'],json.loads(outbox['payload']),outbox['id'])
            # Persist ACK and public memory atomically before reporting success.
            await self.run_core(self.conversation.delivered,outbox,ack)
            return True
        except DeliveryError as exc:
            status='unknown' if exc.unknown else 'failed'
            self.repo.execute('UPDATE delivery_outbox SET status=?,error=?,updated_at=? WHERE id=?',
                              (status,str(exc),time.time(),outbox['id']))
            self.repo.event('delivery',{'id':outbox['id'],'status':status,'error':str(exc)})
            return False
        except Exception as exc:
            # The channel ACK was received, but recording failed. Do not resend.
            self.repo.execute("UPDATE delivery_outbox SET status='unknown',error=? WHERE id=?",
                              ('ACK received; persistence failed: '+str(exc),outbox['id']))
            raise

    def retry(self, jid):
        job=self.repo.job(jid)
        if not job or job['stage']!='failed': raise ValueError('Only failed jobs can be retried')
        if time.time()-job['created_at']>float(config.get('reply_ttl_seconds',300)):
            raise ValueError('Job has expired; retry would send an outdated response')
        outbox=self.repo.query('SELECT status FROM delivery_outbox WHERE job_id=?',(jid,))
        if any(r['status'] in ('unknown','delivered','sending') for r in outbox):
            raise ValueError('Job delivery is accepted or uncertain; automatic replay is disabled')
        stage='generated' if job['decision'] else 'stored' if job['memory_ids'] else 'prepared' if job['fragments'] else 'queued'
        self.repo.update_job(jid,stage,error=None)
        self.repo.audit('job.retry',{'id':jid})

    def maintenance(self, kind, callback):
        operation_id=uuid.uuid4().hex
        self.maintenance_jobs[operation_id]={'kind':kind,'status':'queued'}
        async def run():
            self.maintenance_jobs[operation_id]['status']='running'
            try:
                value=await self.run_core(callback)
                self.maintenance_jobs[operation_id].update(status='completed',result=value)
            except Exception as exc:
                self.maintenance_jobs[operation_id].update(status='failed',error=str(exc))
            self.repo.event('maintenance',{'id':operation_id,**{k:v for k,v in self.maintenance_jobs[operation_id].items() if k!='_task'}})
        task=asyncio.create_task(run(),name='maintenance-'+operation_id)
        self.maintenance_jobs[operation_id]['_task']=task
        return operation_id

    async def stop(self):
        self.stopping=True
        # Do not cancel a synchronous core writer while it can still mutate state.
        tasks=list(self._initializers)
        if self.initializer and self.initializer not in tasks: tasks.append(self.initializer)
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        if self.worker: await self.worker
        tasks=[v['_task'] for v in self.maintenance_jobs.values() if '_task' in v]
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        try:
            if self.ready:
                from utils.persistence import save_all_data,save_state
                await self.run_core(save_all_data,force=True); await self.run_core(save_state)
        finally:
            try:
                await self.adapter.close()
                await self.run_core(self._close_core)
            finally:
                self.executor.shutdown(wait=True,cancel_futures=False)
                self.repo.close(); self.lock.release()

    def _close_core(self):
        from core import memory_engine as me,concept_store
        if me._db_conn: me._db_conn.close(); me._db_conn=None
        concept_store._db_conn=None
        from app.repositories import sqlite as histories
        if histories._history_repository:
            histories._history_repository.close(); histories._history_repository=None
        from core.llm_interface import client,client_
        client.close(); client_.close()
