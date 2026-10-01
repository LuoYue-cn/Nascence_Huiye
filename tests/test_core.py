import json
import threading
import time
import numpy as np
import pytest
from core import memory_engine as me,concept_store as cs
from utils.session_context import group_context
from utils import persistence as p
from tests.conftest import embedding


def create(text,group='111',vec=None):
    with group_context(group): return me.create_memory(text,vector=vec if vec is not None else embedding(text))

@pytest.mark.parametrize('vector',[[],[1,2],np.full(768,np.nan),np.zeros(768),np.full(768,np.inf)])
def test_bad_vector_never_persists(vector):
    with pytest.raises(ValueError): create('invalid',vec=vector)
    assert me._get_db().execute('SELECT COUNT(*) FROM memories').fetchone()[0]==0
    assert me._faiss_index.ntotal==0


def test_atomic_rollback_restores_all_indexes():
    before=dict(me.wordweb)
    with pytest.raises(RuntimeError),group_context('111'),me.atomic_memories():
        mid=create('回滚关键词')
        cs.record_concept('事务概念',[mid],vector=embedding('事务概念'))
        raise RuntimeError('injected failure')
    assert me._get_db().execute('SELECT COUNT(*) FROM memories').fetchone()[0]==0
    assert cs._get_db().execute('SELECT COUNT(*) FROM concepts').fetchone()[0]==0
    assert me._faiss_index.ntotal==0 and cs._concept_faiss.ntotal==0
    assert me.wordweb==before


def test_sqlite_recovers_without_json_and_with_stale_index():
    mid=create('冷库专属关键词')
    me.memories.clear();me.hot_ids.clear();me._init_faiss_index()
    p.load_all_data()
    assert mid in me._faiss_to_mem
    with group_context('111'): assert mid in {r[1]['id'] for r in me.retrieve_by_exact_keywords(['冷库'])}


def test_scope_before_ranking_and_concept_merge():
    same=np.ones(768,dtype='float32')
    for i in range(15): create('另群'+str(i),'222',same)
    own=create('本群秘密','111',same)
    with group_context('111'):
        results=me.retrieve_similar('same',k=1)
        # Ranking must inspect the allowed partition, regardless of outsiders.
        assert all(row[1]['group_id']=='111' for row in results)
        assert own in {r[1] for r in me._search_visible(same.reshape(1,-1),1)}
        cid=cs.record_concept('同名主题',[own],vector=same)
    other=create('other','222',same)
    with group_context('222'):
        cid2=cs.record_concept('同名主题',[other],vector=same)
        assert cid2!=cid
        assert cs.match_concept('同名主题')[0]==cid2


def test_reinforcement_survives_sleep_cleanup():
    mid=create('仍然重要')
    old=time.time()-1000000
    db=me._get_db();db.execute('UPDATE memories SET half_life=1,last_accessed=? WHERE id=?',(old,mid));db.commit()
    me.memories[mid]['half_life']=1;me.memories[mid]['last_accessed']=old
    me.access_memory(mid)
    p.sleep_cleanup()
    assert db.execute('SELECT id FROM memories WHERE id=?',(mid,)).fetchone()


def test_unknown_time_does_not_expire():
    mid=create('旧时间未确认')
    db=me._get_db();db.execute("UPDATE memories SET time_basis='legacy_unknown',last_accessed=2,half_life=1 WHERE id=?",(mid,));db.commit()
    p.load_all_data();assert me.check_and_handle_expired(mid)
    p.sleep_cleanup();assert db.execute('SELECT 1 FROM memories WHERE id=?',(mid,)).fetchone()


def test_save_failure_is_immediately_retryable(monkeypatch):
    original=p._do_save_all_data
    monkeypatch.setattr(p,'_do_save_all_data',lambda:(_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):p.save_all_data()
    assert p._last_save_time==0
    monkeypatch.setattr(p,'_do_save_all_data',original)
    assert p.save_all_data()


def test_save_and_cleanup_cannot_deadlock():
    errors=[]
    def run(fn):
        try:fn()
        except Exception as exc:errors.append(exc)
    threads=[threading.Thread(target=run,args=(p.save_all_data,)),threading.Thread(target=run,args=(p.sleep_cleanup,))]
    for t in threads:t.start()
    for t in threads:t.join(5)
    assert not any(t.is_alive() for t in threads)
    assert not errors


def test_faiss_edges_respect_inhibition():
    vec=np.ones(768,dtype='float32')
    a=create('A',vec=vec);b=create('B',vec=vec)
    with group_context('111'):
        assert not any(m['id']==b for m,_ in me.pathfind_activation([a],inhibited_edges={(a,b),(b,a)}))


def test_metrics_restore_not_overwritten():
    me._bump_message_sent();p.save_all_data(force=True)
    expected=me._export_metrics_counters()
    me._import_metrics_counters({k:0 for k in expected})
    p.load_all_data();assert me._export_metrics_counters()==expected


def test_memory_edit_delete_share_core():
    from app.services.memory import MemoryService
    svc=MemoryService();mid=svc.create('原文本',172800,'group','111')
    other=create('有联系')
    svc.edit(mid,'新文本',172800,'group','222')
    assert me.get_memory_content(mid)=='新文本'
    assert not any(mid in key for key in me._dirty_links)
    svc.delete(mid)
    assert not me._get_db().execute('SELECT 1 FROM memories WHERE id=?',(mid,)).fetchone()


def test_direct_content_and_dedup_respect_scope(monkeypatch):
    vec=np.ones(768,dtype='float32')
    mid=create('其他群内容','222',vec)
    monkeypatch.setattr(me,'text_to_vector',lambda text:vec)
    with group_context('111'):
        assert me.get_memory_content(mid) is None
        assert me.semantic_dedup('其他群内容') is None
    with group_context('222'):assert me.get_memory_content(mid)=='其他群内容'


def test_inhibited_seeds_excluded_from_direct_retrieval(monkeypatch):
    from core import cognition as cog
    mid=create('反刍记忆')
    monkeypatch.setattr(cog,'retrieve_similar',lambda *args,**kwargs:[(1,me.memories[mid])])
    monkeypatch.setattr(cog,'retrieve_by_exact_keywords',lambda *args,**kwargs:[(1,me.memories[mid])])
    with group_context('111'):assert cog.retrieve_and_diffuse(['反刍'],inhibited_seeds={mid})==[]


def test_empty_round_does_not_reuse_another_groups_seeds():
    from core import cognition as cog
    cog._current_round_seeds=['another-group'];cog._current_round_edges=[('a','b')]
    assert cog.retrieve_and_diffuse([])==[]
    assert cog._current_round_seeds==[] and cog._current_round_edges==[]


def test_existing_word_counts_roll_back_without_mutating_snapshot():
    create('事务回滚关键词')
    before=json.loads(json.dumps({str(k):v for k,v in me.wordweb.items()}))
    with pytest.raises(RuntimeError),group_context('111'),me.atomic_memories():
        create('事务回滚关键词')
        raise RuntimeError('abort')
    assert {str(k):v for k,v in me.wordweb.items()}==before


def test_unknown_wordweb_timestamp_is_preserved():
    me.wordweb[('旧词','原词')]={'forward_count':2,'avg_distance':1,'last_updated':2,'time_basis':'legacy_unknown'}
    p.sleep_cleanup()
    assert ('旧词','原词') in me.wordweb
