import asyncio
import json
import time
import numpy as np
import pytest
from app.runtime import Runtime
from app.repositories.sqlite import Repository
from app.services.qq_conversation import QQConversationService
from config.api_config import config
from core import memory_engine as me
from qq_bot import DeliveryError,QQAdapter
from tests.conftest import embedding


def raw(group='111',mid='1'):
    return {'post_type':'message','message_type':'group','group_id':int(group),'self_id':123456,'user_id':555,'message_id':mid,'sender':{'nickname':'用户'},'message':[{'type':'text','data':{'text':'你好，记住这个事情'}}]}

@pytest.fixture
def rt():
    runtime=Runtime(probe_models=False);runtime.repo=Repository();runtime.conversation=QQConversationService(runtime.repo)
    yield runtime
    runtime.repo.close();runtime.executor.shutdown()


def test_durable_raw_dedup_and_recovery(rt):
    jid,new=rt.repo.accept(raw());assert new
    assert rt.repo.accept(raw())==(jid,False)
    assert rt.repo.accept(raw('222'))[1]
    rt.repo.update_job(jid,'processing')
    rt.repo.recover();assert rt.repo.job(jid)['stage']=='queued'
    assert rt.repo.query('SELECT COUNT(*) n FROM messages')[0]['n']==2


def test_input_commit_is_one_checkpoint(rt,monkeypatch):
    import app.services.qq_conversation as conversation
    monkeypatch.setattr(conversation,'decompose_input',lambda _: (['第一件事','第二件事'],'short',{},['事情'],'','none'))
    jid,_=rt.repo.accept(raw());job=rt.repo.job(jid)
    rt.conversation.prepare(job);rt.conversation.commit(rt.repo.job(jid))
    assert rt.repo.job(jid)['stage']=='stored'
    assert me._get_db().execute('SELECT COUNT(*) FROM memories').fetchone()[0]==2
    ids=json.loads(rt.repo.job(jid)['memory_ids']);assert len(ids)==2
    assert all(me.memories[mid]['group_id']=='111' for mid in ids)


async def test_ack_timeout_never_publishes_or_counts(rt,monkeypatch):
    jid,_=rt.repo.accept(raw());rt.repo.update_job(jid,'generated',decision=json.dumps({'say':True,'text':'回复'}))
    out=rt.conversation.create_outbox(rt.repo.job(jid),{'say':True,'text':'回复'})
    async def failed(*args):raise DeliveryError('timeout',unknown=True)
    monkeypatch.setattr(rt.adapter,'send',failed)
    before=me._export_metrics_counters()
    assert await rt.deliver(out) is False
    assert rt.repo.query('SELECT status FROM delivery_outbox')[0]['status']=='unknown'
    assert not rt.repo.query("SELECT * FROM messages WHERE role='bot'")
    assert me._export_metrics_counters()==before
    rt.repo.update_job(jid,'failed')
    with pytest.raises(ValueError):rt.retry(jid)
    rt.repo.recover();assert rt.repo.query('SELECT status FROM delivery_outbox')[0]['status']=='unknown'


async def test_ack_publication_once_and_original_group(rt,monkeypatch):
    jid,_=rt.repo.accept(raw('222'));decision={'say':True,'text':'我来回复'}
    rt.repo.update_job(jid,'generated',decision=json.dumps(decision))
    out=rt.conversation.create_outbox(rt.repo.job(jid),decision)
    targets=[]
    async def send(group,payload,oid):targets.append(group);return {'message_id':44}
    monkeypatch.setattr(rt.adapter,'send',send)
    config['active_group_id']='111'
    assert await rt.deliver(out)
    rt.conversation.delivered(out,{'message_id':44})
    assert targets==['222']
    assert len(rt.repo.query("SELECT * FROM messages WHERE role='bot'"))==1
    assert me._get_db().execute("SELECT group_id FROM memories").fetchone()[0]=='222'


async def test_event_loop_responds_during_model_work(rt):
    started=asyncio.Event()
    ticks=[]
    async def model():await rt.run_core(time.sleep,.2)
    async def heartbeat():
        for _ in range(4):await asyncio.sleep(.02);ticks.append(time.monotonic())
    await asyncio.gather(model(),heartbeat())
    assert len(ticks)==4 and ticks[-1]-ticks[0]<.15


async def test_adapter_waits_for_echo_ack(rt):
    class Socket:
        async def send_text(self,text):
            packet=json.loads(text)
            assert packet['params']['group_id']==222
            asyncio.get_running_loop().call_later(.02,lambda: rt.adapter.pending[packet['echo']].set_result({'status':'ok','retcode':0,'data':{'message_id':99}}))
    rt.adapter.websocket=Socket()
    assert await rt.adapter.send('222','test')=={'message_id':99}
    assert not rt.adapter.pending


def test_say_false_has_no_outbox_or_public_history(rt,monkeypatch):
    import app.services.qq_conversation as conversation
    monkeypatch.setattr(conversation,'verbalize',lambda *args,**kwargs:{'say':False,'text':'内部想法'})
    monkeypatch.setattr(conversation.cog,'retrieve_by_concept',lambda *args:([],None,None))
    monkeypatch.setattr(conversation.cog,'retrieve_and_diffuse',lambda *args:[])
    jid,_=rt.repo.accept(raw());rt.repo.update_job(jid,'stored',fragments=json.dumps({'keywords':['hello'],'intent':'none','full':'hello'}))
    decision=rt.conversation.think(rt.repo.job(jid))
    assert not decision['say'] and not rt.repo.query('SELECT * FROM delivery_outbox')
    assert not rt.repo.query("SELECT * FROM messages WHERE role='bot'")
    assert me._get_db().execute('SELECT source FROM memories').fetchone()[0]=='internal'


def test_final_note_line_deletion_still_records_trace(rt,monkeypatch):
    import core.action_layer
    monkeypatch.setattr(core.action_layer,'decide_action',lambda *args:{'action':'edit_txt','note_name':'清单','old_text':'最后一项','new_text':'','deleted':True,'note_text':''})
    jid,_=rt.repo.accept(raw());rt.repo.update_job(jid,'finished',decision=json.dumps({'say':False,'text':''}))
    rt.conversation.action(rt.repo.job(jid),False)
    assert me._get_db().execute('SELECT content FROM memories').fetchone()[0].endswith('划掉了')
    assert '（空）' in rt.conversation.state('111').pending_note


def test_history_after_200_and_group_context(rt):
    from utils.message_history import flush_to_file,get_all
    from utils.session_context import group_context
    for i in range(250):rt.repo.history_add('user',str(i),'QQ','111')
    flush_to_file()
    for i in range(250,270):rt.repo.history_add('user',str(i),'QQ','111')
    rt.repo.history_add('other','OTHER','QQ','222')
    flush_to_file()
    from utils.paths import DATA_DIR
    assert len((DATA_DIR/'message_history.log').read_text().splitlines())==271
    with group_context('111'):
        history=get_all();assert len(history)==200 and all(m['content']!='OTHER' for m in history)
    assert get_all()==[]

async def test_simultaneous_delivery_attempts_send_only_once(rt,monkeypatch):
    jid,_=rt.repo.accept(raw());decision={'say':True,'text':'只发送一次'}
    rt.repo.update_job(jid,'generated',decision=json.dumps(decision))
    out=rt.conversation.create_outbox(rt.repo.job(jid),decision)
    count=[]
    async def send(*args):count.append(1);await asyncio.sleep(.03);return {'message_id':99}
    monkeypatch.setattr(rt.adapter,'send',send)
    assert await asyncio.gather(rt.deliver(out),rt.deliver(out))==[True,True]
    assert count==[1]


async def test_failed_delivery_retries_saved_payload(rt,monkeypatch):
    jid,_=rt.repo.accept(raw());decision={'say':True,'text':'保存的原回复'}
    rt.repo.update_job(jid,'generated',decision=json.dumps(decision))
    out=rt.conversation.create_outbox(rt.repo.job(jid),decision)
    rt.repo.execute("UPDATE delivery_outbox SET status='failed' WHERE id=?",(out['id'],))
    payload=[]
    async def send(group,text,oid):payload.append((group,text));return {'message_id':88}
    monkeypatch.setattr(rt.adapter,'send',send)
    assert await rt.retry_delivery(out['id'])
    assert payload==[('111','保存的原回复')]
    with pytest.raises(ValueError):await rt.retry_delivery(out['id'])


async def test_invalid_onebot_segments_and_sender_are_rejected(rt):
    for payload in [raw()|{'message':[None]},raw()|{'sender':[]},raw()|{'sender':{'card':['invalid']}},raw()|{'message_id':{'bad':'id'}}]:
        with pytest.raises(ValueError):await rt.accept_message(payload)
    assert not rt.repo.query('SELECT * FROM turn_jobs')


def test_note_trace_survives_embedding_failure(rt,monkeypatch):
    import core.action_layer
    monkeypatch.setattr(core.action_layer,'decide_action',lambda *args:{'action':'edit_txt','note_name':'清单','old_text':'最后一项','new_text':'','deleted':True,'note_text':''})
    monkeypatch.setattr(me,'text_to_vector',lambda text:(_ for _ in ()).throw(RuntimeError('provider failed')))
    jid,_=rt.repo.accept(raw());rt.repo.update_job(jid,'finished',decision=json.dumps({'say':False,'text':''}))
    with pytest.raises(RuntimeError):rt.conversation.action(rt.repo.job(jid),False)
    assert rt.repo.query("SELECT * FROM messages WHERE role='internal'")[0]['content'].endswith('划掉了')


async def test_sleeping_stored_job_does_not_block_new_wake_message(rt,monkeypatch):
    from core.biorhythm import BIORHYTHM
    first,_=rt.repo.accept(raw(mid='old'));rt.repo.update_job(first,'stored')
    second,_=rt.repo.accept(raw(mid='new'))
    BIORHYTHM.force_sleep('test')
    monkeypatch.setattr(BIORHYTHM,'tick',lambda:None)
    rt.ready=True;rt.adapter.websocket=object()
    selected=[]
    async def process(job):selected.append(job['id']);rt.stopping=True
    monkeypatch.setattr(rt,'_process',process)
    await asyncio.wait_for(rt._worker(),1)
    assert selected==[second]
