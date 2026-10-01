"""Management jobs use the core executor and produce authenticated artifacts."""
import json
import os
import sqlite3
import uuid
import zipfile
from pathlib import Path
from core import memory_engine as me
from config.api_config import config
from utils.paths import DATA_DIR, CONFIG_DIR


def backup():
    from utils.persistence import save_all_data,save_state
    # Legacy data can be backed up even when model initialization is unavailable.
    db=me._get_db()
    if db.execute("SELECT 1 FROM core_metadata WHERE key='time_schema'").fetchone():
        save_all_data(force=True); save_state()
    target=DATA_DIR/'backups'; target.mkdir(parents=True,exist_ok=True)
    identifier=uuid.uuid4().hex
    snapshot=target/('snapshot-'+identifier+'.sqlite')
    output=target/('backup-'+identifier+'.zip'); temporary=output.with_suffix('.tmp')
    try:
        copy=sqlite3.connect(snapshot)
        try: db.backup(copy)
        finally: copy.close()
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot,'memory.db')
            archive.writestr('manifest.json',json.dumps({'format':'huiye-backup-v1','time_schema':'inspect core_metadata','notes':'Stop bot before restore; revoke restored admin_sessions'},ensure_ascii=False))
            archive.writestr('config/api_config.json',json.dumps(config,ensure_ascii=False))
            manifest=CONFIG_DIR/'qq_manifest.json'
            if manifest.exists(): archive.write(manifest,'config/qq_manifest.json')
            roots=[(DATA_DIR/'groups','groups'),(DATA_DIR/'assets','assets'),(DATA_DIR/'notes','notes'),(DATA_DIR/'legacy_notes','legacy_notes')]
            if DATA_DIR.name=='test': roots.append((DATA_DIR.parent/'notes','legacy_notes_original'))
            for root,label in roots:
                if root.exists() and not root.is_symlink():
                    for file in root.rglob('*'):
                        if file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(root.resolve()):
                            archive.write(file,str(Path(label)/file.relative_to(root)))
            for name in ('biorhythm.json','clock_state.txt','message_state.json','message_history.log','dialogue_log.jsonl','memory.json','metrics_daily.jsonl','metrics_baseline.json','metrics_counters.json'):
                file=DATA_DIR/name
                if file.is_file(): archive.write(file,name)
        os.replace(temporary,output)
        return {'name':output.name,'bytes':output.stat().st_size}
    finally:
        snapshot.unlink(missing_ok=True); temporary.unlink(missing_ok=True)


def rebuild_indexes():
    from core.concept_store import reload_from_db
    me._rebuild_faiss_index(); me._build_word_to_memories(); reload_from_db()
    return {'vectors':me._faiss_index.ntotal,'keywords':len(me.word_to_memories)}


def self_test():
    result={'sqlite':me._get_db().execute('PRAGMA integrity_check').fetchone()[0]}
    vector=me.validate_vector(me.text_to_vector('向量自检'))
    result.update(embedding_dimensions=len(vector),model=config['ollama_embed_model'])
    return result
