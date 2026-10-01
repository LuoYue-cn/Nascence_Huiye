"""SQLite is authoritative. Snapshots are disposable exports, not recovery gates."""
import json
import os
import time
from utils.paths import data_path, atomic_json
from core import memory_engine as me

_io_lock = me._data_lock  # One reentrant order for save and sleep maintenance.
MEMORY_FILE = data_path('memory.json')
STATE_FILE = data_path('dialogue_state.json')
FAISS_INDEX_FILE = data_path('faiss.index')
FAISS_MAPPING_FILE = data_path('faiss_mapping.json')
METRICS_COUNTERS_FILE = data_path('metrics_counters.json')
DIALOGUE_LOG_FILE = data_path('dialogue_log.jsonl')
_SAVE_MIN_INTERVAL = 3.0
_last_save_time = 0.0
_save_count = 0

def _metadata(key, value=None):
    db = me._get_db()
    if value is not None:
        db.execute('INSERT OR REPLACE INTO core_metadata VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))
        db.commit()
    row = db.execute('SELECT value FROM core_metadata WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None

def save_all_data(force=False):
    global _last_save_time, _save_count
    with _io_lock:
        now = time.monotonic()
        if not force and now - _last_save_time < _SAVE_MIN_INTERVAL:
            return False
        _do_save_all_data()
        _last_save_time = time.monotonic()  # Advance only after a successful save.
        _save_count += 1
        return True

def _do_save_all_data():
    me._sync_all_links_to_sqlite()
    _metadata('wordweb', {f'{a}||{b}': val for (a,b),val in me.wordweb.items()})
    _metadata('metrics', me._export_metrics_counters())
    me._get_db().execute('PRAGMA wal_checkpoint(PASSIVE)')
    atomic_json(MEMORY_FILE, {
        'memories': {mid: me.memories[mid] for mid in me.hot_ids if mid in me.memories},
        'links': {f'{a}||{b}': v for (a,b),v in me.links.items()},
        'wordweb': {f'{a}||{b}': v for (a,b),v in me.wordweb.items()},
    })
    from core.biorhythm import BIORHYTHM
    BIORHYTHM.save()
    from utils.message_history import flush_to_file
    flush_to_file()

def load_all_data():
    with _io_lock:
        db = me._get_db()
        # JSON-only legacy stores must be migrated explicitly before runtime readiness.
        me.memories.clear(); me.hot_ids.clear(); me.links.clear()
        rows = db.execute('SELECT * FROM memories ORDER BY last_accessed DESC LIMIT ?', (me.MAX_HOT_SIZE,)).fetchall()
        for row in rows:
            mem = me._row_to_memory(row)
            me.memories[mem['id']] = mem; me.hot_ids.add(mem['id'])
        me._dirty_links.clear(); me._deleted_links.clear()
        me._build_word_to_memories()
        me._rebuild_faiss_index()  # Includes commits made after any old cache snapshot.
        me.wordweb.clear()
        for key, value in (_metadata('wordweb') or {}).items():
            a,b = key.split('||',1); me.wordweb[(a,b)] = value
        metrics = _metadata('metrics')
        if metrics is None:
            if os.path.exists(METRICS_COUNTERS_FILE):
                with open(METRICS_COUNTERS_FILE, encoding='utf-8-sig') as f:
                    metrics = json.load(f)
            else:
                me._init_metrics_counters()
        if metrics is not None:
            me._import_metrics_counters(metrics)
        from core.biorhythm import BIORHYTHM
        BIORHYTHM.load()
        from core.concept_store import reload_from_db
        reload_from_db()

def save_state():
    from utils.dialogue_state import export_states
    with _io_lock:
        _metadata('dialogue_states', export_states())

def load_state():
    from utils.dialogue_state import import_states
    with _io_lock:
        import_states(_metadata('dialogue_states') or {})

def sleep_cleanup():
    with _io_lock:
        me._sync_all_links_to_sqlite()
        expired = me._purge_expired_memories()
        if expired:
            me._rebuild_faiss_index()
        now = time.time()
        for key, data in list(me.links.items()):
            if data.get('time_basis')!='unix_utc': continue
            if data['weight'] * me.time_decay(max(0,now-data.get('last_accessed',now)), me.LINK_HALF_LIFE) < .01:
                del me.links[key]; me._dirty_links.discard(key); me._deleted_links.add(key); me._bump_link_deleted()
        db = me._get_db()
        for src,tgt,weight,access in db.execute("SELECT src,tgt,weight,last_accessed FROM links WHERE time_basis='unix_utc'").fetchall():
            if weight * me.time_decay(max(0,now-access),me.LINK_HALF_LIFE) < .01:
                db.execute('DELETE FROM links WHERE src=? AND tgt=?',(src,tgt))
        db.commit()
        for key,val in list(me.wordweb.items()):
            if val.get('time_basis')!='unix_utc': continue
            if val['forward_count'] * me.time_decay(max(0,now-val['last_updated']),me.LINK_HALF_LIFE) < .5:
                del me.wordweb[key]
        me._evict_cold_memories(); me._evict_cold_links()
        save_all_data(force=True)
        me._write_daily_metrics()
        return {'expired': len(expired)}
