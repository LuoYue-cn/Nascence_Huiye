import io
import json
import time
import zipfile
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from tests.conftest import TEST_ROOT

@pytest.fixture
def client():
    with TestClient(create_app(probe_models=False)) as client:
        for _ in range(100):
            if client.app.state.runtime.ready:break
            time.sleep(.02)
        assert client.app.state.runtime.ready,client.app.state.runtime.error
        yield client


def login(client):
    response=client.post('/api/v1/auth/login',json={'name':'admin','password':'test-admin-password-12345'})
    assert response.status_code==200,response.text
    client.headers['X-CSRF-Token']=response.json()['csrf']


def test_auth_root_and_removed_channels(client):
    assert client.get('/',follow_redirects=False).headers['location']=='/login'
    assert client.get('/api/v1/memories').status_code==401
    for path in ('/admin','/chat','/api/v1/chat','/api/v1/training','/data/test/memory.db','/api/no-such-route'):
        assert client.get(path).status_code==404
    login(client)
    assert client.get('/').status_code==200
    assert client.post('/api/v1/history',json={'message':'web chat'}).status_code==405
    client.headers['X-CSRF-Token']='wrong'
    assert client.post('/api/v1/qq/pause',json={'paused':True}).status_code==403


def test_origin_and_config_contract(client):
    assert client.post('/api/v1/auth/login',headers={'Origin':'https://evil.example'},json={'password':'test-admin-password-12345'}).status_code==403
    login(client)
    assert client.put('/api/v1/settings',json={'values':{'active_enabled':'false'}}).status_code==422
    assert client.put('/api/v1/settings',json={'values':{'active_interval_seconds':0}}).status_code==422
    assert client.put('/api/v1/settings',json={'values':{'training_enabled':True}}).status_code==422
    assert client.put('/api/v1/settings',json={'values':{'napcat_token':'short'}}).status_code==422
    payload=client.get('/api/v1/settings').json()
    assert 'YOUR_API_KEY_HERE' not in json.dumps(payload)


def test_memory_crud_import_and_same_sqlite(client):
    login(client)
    body={'content':'由管理面板写入','group_id':'111','scope':'group','half_life':100000}
    response=client.post('/api/v1/memories',json=body);assert response.status_code==200,response.text
    mid=response.json()['id']
    result=client.get('/api/v1/memories/'+mid).json();assert result['content']==body['content']
    body['content']='修改后的记忆'
    assert client.put('/api/v1/memories/'+mid,json=body).status_code==200
    records=[body,body]
    preview=client.post('/api/v1/memories/import',json={'records':records}).json()
    assert preview['new']==0 and preview['duplicates']==2
    assert client.delete('/api/v1/memories/'+mid).status_code==200
    assert client.get('/api/v1/memories/'+mid).status_code==404
    assert client.post('/api/v1/memories',json=body|{'half_life':-1}).status_code==422


def test_qq_settings_pause_and_history(client):
    login(client)
    assert client.put('/api/v1/qq',json={'whitelist_groups':['333'],'name_mapping':{}}).status_code==200
    assert client.get('/api/v1/qq').json()['whitelist_groups']==['333']
    assert client.post('/api/v1/qq/pause',json={'paused':True}).status_code==200
    assert client.get('/api/v1/status').json()['paused'] is True
    assert client.get('/health/live').json()=={'alive':True}
    assert client.put('/api/v1/qq',json={'whitelist_groups':['../../'],'name_mapping':{}}).status_code==422


def test_notes_and_upload_are_group_scoped(client):
    from PIL import Image
    login(client)
    body={'group_id':'111','name':'清单','text':'1. 完成任务'}
    assert client.post('/api/v1/notes',json=body).status_code==200
    assert client.get('/api/v1/notes?group_id=222').json()==[]
    result=client.put('/api/v1/notes',json={'group_id':'111','name':'清单','old_text':'完成任务','new_text':''})
    assert result.status_code==200,result.text
    assert result.json()['kept']==0
    assert client.post('/api/v1/notes',json=body|{'name':'../escape'}).status_code==422
    binary=io.BytesIO();Image.new('RGB',(4,4),'red').save(binary,format='PNG')
    result=client.post('/api/v1/assets',data={'group_id':'111','kind':'image','desc':'红图'},files={'file':('red.png',binary.getvalue(),'image/png')})
    assert result.status_code==200,result.text
    rows=client.get('/api/v1/assets?group_id=111').json();assert len(rows)==1
    assert client.get('/api/v1/assets?group_id=222').json()==[]
    assert client.get(f"/api/v1/media/{rows[0]['id']}?group_id=111&kind=image").status_code==200
    assert client.get(f"/api/v1/media/{rows[0]['id']}?group_id=222&kind=image").status_code==404
    assert client.post('/api/v1/assets',data={'group_id':'111','kind':'image','desc':'非法'},files={'file':('evil.png',b'not an image','image/png')}).status_code==422


def test_backup_task_and_private_download(client):
    login(client)
    response=client.post('/api/v1/maintenance',json={'kind':'backup'});assert response.status_code==200,response.text
    oid=response.json()['id']
    for _ in range(100):
        job=client.get('/api/v1/status').json()['maintenance'][oid]
        if job['status'] not in ('queued','running'):break
        time.sleep(.02)
    assert job['status']=='completed',job
    name=job['result']['name']
    content=client.get('/api/v1/maintenance/backups/'+name).content
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert 'memory.db' in archive.namelist() and 'manifest.json' in archive.namelist()
        assert 'initial_admin_password.txt' not in archive.namelist()
    client.cookies.clear()
    assert client.get('/api/v1/maintenance/backups/'+name).status_code==401


def test_instance_lock_prevents_second_runtime(client):
    from app.runtime import InstanceLock
    from utils.paths import DATA_DIR
    lock=InstanceLock(DATA_DIR/'.instance.lock')
    with pytest.raises(RuntimeError):lock.acquire()


def test_websocket_auth_and_ack_during_slow_model(client,monkeypatch):
    import asyncio
    import threading
    import app.services.qq_conversation as conversation
    from tests.test_qq import raw
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/internal/onebot/ws'):pass
    started=threading.Event()
    original=client.app.state.runtime.conversation.prepare
    def slow_prepare(*args):started.set();time.sleep(.25);return original(*args)
    monkeypatch.setattr(client.app.state.runtime.conversation,'prepare',slow_prepare)
    monkeypatch.setattr(conversation,'decompose_input',lambda _: (['消息内容'],'普通',{},['消息'],'','none'))
    monkeypatch.setattr(conversation,'verbalize',lambda *args,**kwargs:{'say':False,'text':''})
    monkeypatch.setattr(conversation.cog,'retrieve_by_concept',lambda *args:([],None,None))
    monkeypatch.setattr(conversation.cog,'retrieve_and_diffuse',lambda *args:[])
    with client.websocket_connect('/internal/onebot/ws',headers={'Authorization':'Bearer test-napcat-token-123456'}) as socket:
        socket.send_json(raw())
        assert started.wait(2)
        started_at=time.monotonic()
        future=client.portal.start_task_soon(client.app.state.runtime.adapter.action,'get_status',{},1)
        packet=socket.receive_json();assert packet['action']=='get_status'
        socket.send_json({'echo':packet['echo'],'status':'ok','retcode':0,'data':{'online':True}})
        assert future.result(1)=={'online':True}
        assert time.monotonic()-started_at<.2
    assert client.get('/health/live').status_code==200
    assert client.app.state.runtime.stopping is False


def test_legacy_gate_keeps_management_available(monkeypatch):
    from core import memory_engine as me
    me.create_memory('旧数据迁移前',vector=__import__('tests.conftest',fromlist=['embedding']).embedding('old'))
    with TestClient(create_app(probe_models=False)) as client:
        for _ in range(100):
            if 'offline migration' in str(client.app.state.runtime.error):break
            time.sleep(.02)
        assert not client.app.state.runtime.ready
        login(client)
        assert client.get('/api/v1/settings').status_code==200
        assert client.get('/health/ready').status_code==503


def test_invalid_name_mapping_is_rejected(client):
    login(client)
    assert client.put('/api/v1/qq',json={'whitelist_groups':['111'],'name_mapping':{'111':['bad']}}).status_code==422
    assert client.put('/api/v1/qq',json={'whitelist_groups':['111'],'name_mapping':{'111':{'123':['bad']}}}).status_code==422


def test_embedding_model_can_change_only_when_store_is_empty(client):
    login(client)
    response=client.put('/api/v1/settings',json={'values':{'ollama_embed_model':'test-empty-model'}})
    assert response.status_code==200,response.text
    for _ in range(100):
        if client.app.state.runtime.ready:break
        time.sleep(.02)
    assert client.app.state.runtime.ready,client.app.state.runtime.error


def test_biorhythm_settings_take_effect_without_process_restart(client):
    import core.biorhythm as bio
    login(client)
    response=client.put('/api/v1/settings',json={'values':{'biorhythm_wake_seconds':12345}})
    assert response.status_code==200,response.text
    assert bio.T_WAKE==12345
