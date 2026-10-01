#!/usr/bin/env python3
"""Preview by default. Preserve uncertain timestamps and unassigned group scope."""
import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.offline import PROJECT,read_json,stage,publish_db


def inspect(data):
    counts={}
    path=data/'memory.db'
    if path.exists():
        db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
        try:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in ('memories','links','concepts','concept_events','messages'):
                if name in tables: counts[name]=db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
            if 'core_metadata' in tables:
                row=db.execute("SELECT value FROM core_metadata WHERE key='time_schema'").fetchone()
                if row: counts['time_schema']=json.loads(row[0])
            if 'memories' in tables:
                counts['memory_time_range']=list(db.execute('SELECT MIN(creation_time),MAX(creation_time) FROM memories').fetchone())
                counts['time_samples']=[{'id':row[0],'creation_time':row[1],'last_accessed':row[2]} for row in db.execute('SELECT id,creation_time,last_accessed FROM memories LIMIT 5')]
        finally: db.close()
    legacy=read_json(data/'memory.json',{})
    counts['json_memories']=len(legacy.get('memories',{}))
    counts['clock_available']=(data/'clock_state.txt').exists()
    counts['policy']='Unknown timestamps stay unchanged and do not decay; group attribution requires --group.'
    return counts


def apply(data,config_dir,group=None,time_map=None):
    from app.runtime import InstanceLock
    lock=InstanceLock(data/'.instance.lock'); lock.acquire()
    try:
        report=inspect(data)
        if report.get('time_schema')=='unix_v1': return dict(report,already_migrated=True)
        backup,staged=stage(data,config_dir)
        # Import the core only after selecting the isolated staging directory.
        os.environ['HUIYE_DATA_DIR']=str(staged); os.environ['HUIYE_CONFIG_DIR']=str(config_dir)
        # InstanceLock imported Runtime's repositories/paths already. They do not
        # import memory_engine, but refresh their path constants for this CLI.
        import utils.paths as paths
        paths.DATA_DIR=staged; paths.NOTE_DIR=staged/'notes'; paths.LOG_DIR=staged/'logs'
        from core import memory_engine as me,concept_store as cs
        db=me._get_db(); cs._get_db()
        from app.repositories.sqlite import Repository
        repository=Repository(staged/'memory.db'); repository.close()
        db.execute('CREATE TABLE IF NOT EXISTS legacy_time_originals(table_name TEXT,record_id TEXT,fields TEXT,PRIMARY KEY(table_name,record_id))')
        legacy=read_json(staged/'memory.json',{})
        imported=0
        for mid,mem in legacy.get('memories',{}).items():
            if db.execute('SELECT 1 FROM memories WHERE id=?',(mid,)).fetchone(): continue
            vec=me.validate_vector(mem.get('vector'))
            db.execute('''INSERT INTO memories(id,content,vector,half_life,last_accessed,creation_time,last_strengthen_time,concept_tag_ids)
                VALUES(?,?,?,?,?,?,?,?)''',(mid,str(mem['content']),vec.tobytes(),float(mem['half_life']),float(mem.get('last_accessed',0)),float(mem.get('creation_time',0)),float(mem.get('last_strengthen_time',mem.get('creation_time',0))),json.dumps(mem.get('concept_tag_ids',[]))))
            imported+=1
        for key,link in legacy.get('links',{}).items():
            src,tgt=key.split('||',1)
            if not db.execute('SELECT 1 FROM memories WHERE id=?',(src,)).fetchone() or not db.execute('SELECT 1 FROM memories WHERE id=?',(tgt,)).fetchone(): continue
            db.execute('INSERT OR IGNORE INTO links(src,tgt,weight,type,last_accessed,creation_time) VALUES(?,?,?,?,?,?)',(src,tgt,float(link['weight']),link.get('type','semantic'),float(link.get('last_accessed',0)),float(link.get('creation_time',0))))
        mapping=read_json(time_map,{}) if time_map else {}
        for table,fields,keyexpr in [('memories',['creation_time','last_accessed','last_strengthen_time'],'id'),('links',['creation_time','last_accessed'],"src||'||'||tgt")]:
            if table=='links':
                cols={r[1] for r in db.execute('PRAGMA table_info(links)')}
                if 'time_basis' not in cols: db.execute("ALTER TABLE links ADD COLUMN time_basis TEXT DEFAULT 'legacy_unknown'")
            for row in db.execute(f"SELECT {keyexpr},{','.join(fields)} FROM {table}").fetchall():
                identifier=row[0]; values=dict(zip(fields,row[1:]))
                db.execute('INSERT OR IGNORE INTO legacy_time_originals VALUES(?,?,?)',(table,identifier,json.dumps(values)))
                rule=mapping.get(table,{}).get(identifier,{})
                basis=rule.get('basis','legacy_unknown')
                if basis not in ('legacy_unknown','unix_utc','qq_virtual'): raise ValueError('Invalid timestamp basis for '+identifier)
                if basis=='qq_virtual':
                    offset=rule.get('offset')
                    if type(offset) not in (int,float) or not math.isfinite(offset): raise ValueError('Virtual timestamps need a verified finite offset')
                    values={key:float(value)+offset for key,value in values.items()}
                    basis='unix_utc'
                if any(not math.isfinite(float(value)) for value in values.values()): raise ValueError('Invalid stored timestamps')
                db.execute(f"UPDATE {table} SET "+','.join(f'{key}=?' for key in fields)+f',time_basis=? WHERE {keyexpr}=?',(*values.values(),basis,identifier))
        for content,half in db.execute('SELECT content,half_life FROM memories'):
            if not isinstance(content,str) or not content.strip() or not math.isfinite(half) or half<=0: raise ValueError('Legacy memory content or half-life is invalid')
        # Validate every stored vector before publishing anything.
        for blob, in db.execute('SELECT vector FROM memories UNION ALL SELECT vector FROM concepts'):
            me.validate_vector(__import__('numpy').frombuffer(blob,dtype='float32'))
        if group:
            db.execute("UPDATE memories SET scope='group',group_id=? WHERE scope='legacy_unattributed'",(group,))
            db.execute("UPDATE concepts SET scope='group',group_id=? WHERE scope='legacy_unattributed'",(group,))
        # Concept timestamps were originally Unix time. Preserve them; event
        # bounds are derived only from members with confirmed time domains.
        for eid,raw in db.execute('SELECT id,member_ids FROM concept_events').fetchall():
            members=json.loads(raw); times=[]
            for mid in members:
                row=db.execute('SELECT creation_time,time_basis FROM memories WHERE id=?',(mid,)).fetchone()
                if row and row[1]=='unix_utc': times.append(row[0])
            if len(times)==len(members) and times:
                db.execute("UPDATE concept_events SET start_time=?,end_time=?,time_basis='unix_utc' WHERE id=?",(min(times),max(times),eid))
        words=legacy.get('wordweb',{})
        for identifier,value in words.items():
            original_time=float(value['last_updated'])
            db.execute('INSERT OR IGNORE INTO legacy_time_originals VALUES(?,?,?)',('wordweb',identifier,json.dumps({'last_updated':original_time})))
            rule=mapping.get('wordweb',{}).get(identifier,{})
            basis=rule.get('basis','legacy_unknown')
            if basis not in ('legacy_unknown','unix_utc','qq_virtual'): raise ValueError('Invalid wordweb timestamp domain')
            if basis=='qq_virtual':
                offset=rule.get('offset')
                if type(offset) not in (int,float) or not math.isfinite(offset): raise ValueError('Wordweb virtual time needs a verified offset')
                value['last_updated']=original_time+offset; basis='unix_utc'
            value['time_basis']=basis
        for key,file,default in [('wordweb','memory.json',{}),('metrics','metrics_counters.json',None)]:
            value=legacy.get('wordweb',{}) if key=='wordweb' else read_json(staged/file,default)
            if value is not None: db.execute('INSERT OR REPLACE INTO core_metadata VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
        # Legacy history timestamps came from time.time(); no offset is applied.
        history=read_json(staged/'message_state.json',[])
        if not isinstance(history,list): raise ValueError('Legacy message history must be a list')
        import uuid,time
        for item in history:
            db.execute('''INSERT OR IGNORE INTO messages(id,group_id,sender_id,sender,role,content,quote,received_at,source,time_basis,bot_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(uuid.uuid4().hex,group or 'legacy_unattributed',str(item.get('sender_id','legacy')),str(item.get('sender','legacy')),item.get('role','user'),str(item.get('content','')),json.dumps(item.get('quote'),ensure_ascii=False),float(item.get('timestamp',item.get('time',0))),'legacy','unix_utc','legacy'))
        db.execute('INSERT OR REPLACE INTO core_metadata VALUES(?,?)',('embedding_model',json.dumps(me.OLLAMA_EMBED_MODEL)))
        db.execute('INSERT OR REPLACE INTO core_metadata VALUES(?,?)',('time_schema','"unix_v1"'))
        db.execute('INSERT OR REPLACE INTO core_metadata VALUES(?,?)',('migration',json.dumps({'backup':str(backup),'group':group,'imported':imported})))
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Staged database integrity failed')
        # Optional single-group legacy files; existing group folders are never overwritten.
        new_group=None
        if group:
            new_group=data/'groups'/group
            if new_group.exists(): raise ValueError('Group directory already exists; migrate legacy files manually after inspecting the backup')
            group_stage=staged/'groups'/group; group_stage.mkdir(parents=True,exist_ok=True)
            for name in ('assets','notes'):
                origin=staged/name
                if name=='notes' and not origin.exists():
                    origin=staged/'legacy_notes'
                    if not origin.exists(): origin=staged/'legacy_notes_original'
                if origin.exists(): shutil.copytree(origin,group_stage/name,dirs_exist_ok=True,symlinks=True)
            new_group.parent.mkdir(parents=True,exist_ok=True)
            shutil.copytree(group_stage,new_group,symlinks=True)
        try: publish_db(db,staged,data)
        except BaseException:
            if new_group: shutil.rmtree(new_group)
            raise
        return {'backup':str(backup),'imported_json':imported,'time_schema':'unix_v1','group':group,'unknown_time_preserved':True}
    finally: lock.release()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('HUIYE_DATA_DIR',PROJECT/'data/test')))
    parser.add_argument('--config-dir',type=Path,default=Path(os.environ.get('HUIYE_CONFIG_DIR',PROJECT/'config')))
    parser.add_argument('--group',help='Explicitly attribute all legacy records/assets/notes to one QQ group')
    parser.add_argument('--time-map',type=Path,help='Field-domain evidence: {memories:{id:{basis:qq_virtual,offset:...}},links:{src||tgt:{...}}}')
    parser.add_argument('--preview',action='store_true'); parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.group and not args.group.isdigit(): parser.error('--group must be numeric')
    if args.apply and args.preview: parser.error('Choose preview or apply')
    # These values must precede the import of Runtime/paths.
    data=args.data_dir.resolve(); cfg=args.config_dir.resolve()
    os.environ['HUIYE_DATA_DIR']=str(data);os.environ['HUIYE_CONFIG_DIR']=str(cfg)
    print(json.dumps(apply(data,cfg,args.group,args.time_map) if args.apply else inspect(data),ensure_ascii=False,indent=2))

if __name__=='__main__': main()
