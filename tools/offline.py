"""Safe offline staging shared by migration tools. No model calls on import."""
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]

def read_json(path,default=None):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).exists() else default

def snapshot(data,config):
    backup=data/'backups'/('offline-'+uuid.uuid4().hex)
    backup.mkdir(parents=True,mode=0o700)
    for path in data.iterdir():
        if path.name in ('backups','.instance.lock') or path.name.endswith(('-wal','-shm')): continue
        if path.is_symlink(): continue
        if path.is_dir(): shutil.copytree(path,backup/path.name,symlinks=True)
        elif path.name=='memory.db':
            src=sqlite3.connect(path); dst=sqlite3.connect(backup/path.name)
            try: src.backup(dst)
            finally: src.close();dst.close()
        else: shutil.copy2(path,backup/path.name)
    legacy_notes=data.parent/'notes'
    if data.name=='test' and legacy_notes.is_dir() and not legacy_notes.is_symlink():
        shutil.copytree(legacy_notes,backup/'legacy_notes',symlinks=True)
    if config.exists(): shutil.copytree(config,backup/'config',symlinks=True,dirs_exist_ok=True)
    return backup

def stage(data,config):
    backup=snapshot(data,config)
    staged=backup/'staging'; staged.mkdir()
    for file in backup.iterdir():
        if file.name in ('staging','config'): continue
        if file.is_dir(): shutil.copytree(file,staged/file.name,symlinks=True)
        else: shutil.copy2(file,staged/file.name)
    return backup,staged

def publish_db(db,stage_dir,data):
    db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.close()
    for suffix in ('-wal','-shm'): (data/('memory.db'+suffix)).unlink(missing_ok=True)
    os.replace(stage_dir/'memory.db',data/'memory.db')
