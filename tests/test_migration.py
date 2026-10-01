import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import numpy as np
import pytest
from tests.conftest import embedding

PROJECT=Path(__file__).resolve().parents[1]


def legacy(root,invalid=False):
    data=root/'data';cfg=root/'config';data.mkdir();cfg.mkdir()
    db=sqlite3.connect(data/'memory.db')
    db.executescript('''CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT,vector BLOB,half_life REAL,last_accessed REAL,creation_time REAL,last_strengthen_time REAL,concept_tag_ids TEXT);
        CREATE TABLE links(src TEXT,tgt TEXT,weight REAL,type TEXT,last_accessed REAL,creation_time REAL,PRIMARY KEY(src,tgt));
        CREATE TABLE concepts(id TEXT PRIMARY KEY,name TEXT,vector BLOB,event_ids TEXT,occurrences TEXT,total_count INTEGER,member_count INTEGER,creation_time REAL,last_accessed REAL);
        CREATE TABLE concept_events(id TEXT PRIMARY KEY,concept_id TEXT,member_ids TEXT,start_time REAL,end_time REAL);''')
    vec=np.zeros(3,dtype='float32') if invalid else embedding('legacy')
    db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?)',('old','旧 QQ 记忆',vec.tobytes(),172800,12,10,11,'["concept"]'))
    db.execute('INSERT INTO concepts VALUES(?,?,?,?,?,?,?,?,?)',('concept','主题',embedding('concept').tobytes(),'["event"]','[1700000000]',1,1,1700000000,1700000001))
    db.execute('INSERT INTO concept_events VALUES(?,?,?,?,?)',('event','concept','["old"]',10,10));db.commit();db.close()
    (data/'memory.json').write_text(json.dumps({'memories':{'jsononly':{'content':'JSON 独有','vector':embedding('jsononly').tolist(),'half_life':100000,'creation_time':22,'last_accessed':23,'last_strengthen_time':23}},'links':{}}))
    (data/'message_state.json').write_text(json.dumps([{'sender':'用户','content':'原历史','time':1700000004}]))
    return data,cfg


def cli(data,cfg,*args):
    env=os.environ.copy();env['HUIYE_DATA_DIR']=str(data);env['HUIYE_CONFIG_DIR']=str(cfg)
    return subprocess.run([sys.executable,str(PROJECT/'tools/migrate_legacy.py'),'--data-dir',str(data),'--config-dir',str(cfg),*args],cwd='/tmp',env=env,capture_output=True,text=True,timeout=20)


def test_preview_does_not_mutate(tmp_path):
    data,cfg=legacy(tmp_path);before=(data/'memory.db').read_bytes()
    result=cli(data,cfg,'--preview');assert result.returncode==0,result.stderr
    assert (data/'memory.db').read_bytes()==before
    assert not (data/'backups').exists()


def test_field_mapping_preserves_real_concepts_and_is_idempotent(tmp_path):
    data,cfg=legacy(tmp_path)
    mapping=tmp_path/'times.json';mapping.write_text(json.dumps({'memories':{'old':{'basis':'qq_virtual','offset':1700000000}}}))
    result=cli(data,cfg,'--apply','--group','111','--time-map',str(mapping));assert result.returncode==0,result.stderr
    db=sqlite3.connect(data/'memory.db')
    assert db.execute('SELECT creation_time,time_basis,group_id FROM memories WHERE id="old"').fetchone()==(1700000010,'unix_utc','111')
    assert db.execute('SELECT creation_time,time_basis FROM memories WHERE id="jsononly"').fetchone()==(22,'legacy_unknown')
    assert db.execute('SELECT creation_time FROM concepts').fetchone()[0]==1700000000
    assert db.execute('SELECT start_time,time_basis FROM concept_events').fetchone()==(1700000010,'unix_utc')
    assert db.execute('SELECT received_at FROM messages').fetchone()[0]==1700000004
    assert db.execute('SELECT COUNT(*) FROM legacy_time_originals').fetchone()[0]==2
    db.close()
    again=cli(data,cfg,'--apply','--time-map',str(mapping));assert again.returncode==0,again.stderr
    assert 'already_migrated' in again.stdout
    db=sqlite3.connect(data/'memory.db');assert db.execute('SELECT creation_time FROM memories WHERE id="old"').fetchone()[0]==1700000010;db.close()
    assert len(list((data/'backups').glob('offline-*')))==1


def test_invalid_legacy_vector_leaves_original_safe(tmp_path):
    data,cfg=legacy(tmp_path,invalid=True);before=(data/'memory.db').read_bytes()
    result=cli(data,cfg,'--apply');assert result.returncode!=0
    assert (data/'memory.db').read_bytes()==before
    assert list((data/'backups').glob('offline-*/memory.db'))


def test_unassigned_and_unknown_migration_never_guesses(tmp_path):
    data,cfg=legacy(tmp_path)
    result=cli(data,cfg,'--apply');assert result.returncode==0,result.stderr
    db=sqlite3.connect(data/'memory.db')
    assert db.execute('SELECT creation_time,time_basis,scope,group_id FROM memories WHERE id="old"').fetchone()==(10,'legacy_unknown','legacy_unattributed',None)
    assert db.execute('SELECT time_basis FROM concept_events').fetchone()[0]=='legacy_unknown'
    db.close()


def test_controlled_reembed_success_and_invalid_provider_rollback(tmp_path):
    from tools.reembed import migrate
    data,cfg=legacy(tmp_path)
    before=(data/'memory.db').read_bytes()
    with pytest.raises(ValueError):migrate(data,cfg,'new',4,provider=lambda text:[0,0,0,0])
    assert (data/'memory.db').read_bytes()==before
    assert not (cfg/'api_config.json').exists()
    result=migrate(data,cfg,'new',4,provider=lambda text:[1,2,3,4])
    assert result['vectors']==2
    db=sqlite3.connect(data/'memory.db');assert all(len(row[0])==16 for row in db.execute('SELECT vector FROM memories UNION ALL SELECT vector FROM concepts'));db.close()
    assert json.loads((cfg/'api_config.json').read_text())['embedding_dimension']==4
    assert not (data/'offline_operation.json').exists()


def test_offline_tools_reject_live_instance(tmp_path):
    from app.runtime import InstanceLock
    data,cfg=legacy(tmp_path);lock=InstanceLock(data/'.instance.lock');lock.acquire()
    try:
        result=cli(data,cfg,'--apply');assert result.returncode!=0 and 'already owned' in result.stderr
    finally:lock.release()


def test_management_backup_includes_older_sibling_notes(tmp_path,monkeypatch):
    import zipfile
    import app.services.maintenance as maintenance
    data=tmp_path/'test';data.mkdir()
    notes=tmp_path/'notes';notes.mkdir();(notes/'old.txt').write_text('旧版笔记',encoding='utf-8')
    monkeypatch.setattr(maintenance,'DATA_DIR',data)
    result=maintenance.backup()
    with zipfile.ZipFile(data/'backups'/result['name']) as archive:
        assert archive.read('legacy_notes_original/old.txt').decode()=='旧版笔记'
