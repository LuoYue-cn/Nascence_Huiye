#!/usr/bin/env python3
"""Offline all-or-nothing embedding migration, with backup and crash marker."""
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


def validate(vector,dimension):
    import numpy as np
    result=np.asarray(vector,dtype=np.float32)
    if result.ndim!=1 or len(result)!=dimension or not np.isfinite(result).all() or np.linalg.norm(result)==0:
        raise ValueError('Embedding provider returned an invalid vector')
    return result.tobytes()


def migrate(data,cfg,model,dimension,provider=None):
    from app.runtime import InstanceLock
    from utils.paths import atomic_json
    from config.api_config import DEFAULT_CONFIG
    lock=InstanceLock(data/'.instance.lock');lock.acquire()
    db=None
    try:
        backup,staged=stage(data,cfg)
        db=sqlite3.connect(staged/'memory.db')
        original=read_json(cfg/'api_config.json',{})
        proposed=DEFAULT_CONFIG|original|{'ollama_embed_model':model,'embedding_dimension':dimension}
        if provider is None:
            import requests
            def provider(text):
                response=requests.post(proposed['ollama_base_url'].rstrip('/')+'/api/embed',json={'model':model,'input':text},timeout=proposed['model_timeout_seconds'])
                response.raise_for_status()
                return response.json()['embeddings'][0]
        prepared=[]
        for table,field in (('memories','content'),('concepts','name')):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone(): continue
            for identifier,text in db.execute(f'SELECT id,{field} FROM {table}').fetchall():
                prepared.append((table,identifier,validate(provider(text),dimension)))
        with db:
            for table,identifier,blob in prepared: db.execute(f'UPDATE {table} SET vector=? WHERE id=?',(blob,identifier))
            db.execute('CREATE TABLE IF NOT EXISTS core_metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('INSERT OR REPLACE INTO core_metadata VALUES(?,?)',('embedding_model',json.dumps(model)))
            db.execute('INSERT OR REPLACE INTO core_metadata VALUES(?,?)',('embedding_dimension',str(dimension)))
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Database integrity failed')
        marker=data/'offline_operation.json'
        atomic_json(marker,{'kind':'reembed','backup':str(backup),'model':model,'dimension':dimension})
        try:
            atomic_json(cfg/'api_config.json',proposed)
            publish_db(db,staged,data); db=None
            marker.unlink()
        except BaseException:
            atomic_json(cfg/'api_config.json',original)
            # The marker intentionally remains: a power failure or failed rollback
            # must be inspected, rather than silently accepting mixed resources.
            raise
        return {'backup':str(backup),'vectors':len(prepared),'model':model,'dimension':dimension}
    finally:
        if db: db.close()
        lock.release()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('HUIYE_DATA_DIR',PROJECT/'data/test')))
    parser.add_argument('--config-dir',type=Path,default=Path(os.environ.get('HUIYE_CONFIG_DIR',PROJECT/'config')))
    parser.add_argument('--model',required=True);parser.add_argument('--dimension',type=int,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not 1<=args.dimension<=8192: parser.error('dimension must be 1–8192')
    data=args.data_dir.resolve();cfg=args.config_dir.resolve()
    os.environ['HUIYE_DATA_DIR']=str(data);os.environ['HUIYE_CONFIG_DIR']=str(cfg)
    if not args.apply:
        print(json.dumps({'preview':True,'model':args.model,'dimension':args.dimension,'data_dir':str(data),'instruction':'Stop bot, pull target Ollama model, then repeat with --apply'},indent=2));return
    print(json.dumps(migrate(data,cfg,args.model,args.dimension),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
