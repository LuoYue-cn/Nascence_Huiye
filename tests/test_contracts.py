import json
from types import SimpleNamespace
import pytest
from core import llm_interface as llm,action_layer as actions,asset_library as assets,memory_engine as me
from config.api_config import config
from utils.session_context import group_context


def response(value):return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))])

@pytest.mark.parametrize('value',[[],None,123,{'mem':None},{'mem':[None]},{'k':'abc'},{'s':{'participants':'abc'}},{'m':1},{'c':{}}])
def test_understanding_contract_rejects_wrong_types(monkeypatch,value):
    monkeypatch.setattr(llm.client.chat.completions,'create',lambda **kwargs:response(value))
    with pytest.raises(ValueError):llm.decompose_input('input')


def test_empty_understanding_fragments_are_valid(monkeypatch):
    monkeypatch.setattr(llm.client.chat.completions,'create',lambda **kwargs:response({'mem':[],'k':[],'s':None}))
    assert llm.decompose_input('input')[0]==[]

@pytest.mark.parametrize('value',[{'say':False,'text':'private'},{'say':'false','text':'private'},{'say':True,'text':42}])
def test_verbalization_never_publishes_invalid_or_private(monkeypatch,value):
    monkeypatch.setattr(llm,'call_api_thinking',lambda *args,**kwargs:json.dumps(value))
    result=llm.verbalize([])
    assert not (result['say'] and result['text'])


def test_last_asset_page_and_single_ordered_pool(monkeypatch):
    config['action_max_pages']=2;config['action_page_size']=1
    pool=[{'id':'first','desc':'first','kind':'image','path':'x'},{'id':'last','desc':'last','kind':'image','path':'y'}]
    calls=[];prompts=[]
    def ordered(kind,keywords=None):calls.append(kind);return pool if kind=='image' else []
    monkeypatch.setattr(assets,'ordered_pool',ordered)
    monkeypatch.setattr(assets,'resolve_token',lambda kind,token:next((item for item in pool if item['id']==token),None))
    outputs=iter([json.dumps({'action':'image_next_page'}),json.dumps({'action':'image','id':'last'})])
    def model(messages,**kwargs):prompts.append(str(messages));return next(outputs)
    monkeypatch.setattr(actions,'call_api_thinking',model)
    result=actions.decide_action('thought',True,'reply',[],'QQ')
    assert calls.count('image')==1
    assert '第2/2页' in prompts[-1]
    assert result['action']=='image'


def test_bounded_memory_id_tracker():
    assert me._last_created_ids.maxlen==200
    me._last_created_ids.extend(str(i) for i in range(500));assert len(me._last_created_ids)==200


def test_real_clock_has_no_simulation_controls():
    import time
    from core.virtual_clock import clock
    assert abs(clock.now()-time.time())<1
    assert not hasattr(clock,'set_speed') and not hasattr(clock,'enable_qq_mode')


def test_model_deadline_is_propagated_into_core_executor():
    import time
    from utils.session_context import current_deadline,model_timeout
    token=current_deadline.set(time.monotonic()+.2)
    try:assert 0<model_timeout(45)<.3
    finally:current_deadline.reset(token)
    token=current_deadline.set(time.monotonic()-1)
    try:
        with pytest.raises(TimeoutError):model_timeout(45)
    finally:current_deadline.reset(token)


def test_log_redaction_includes_exception_and_environment():
    import logging
    from utils.logging import SecretFormatter
    text=SecretFormatter().format(logging.LogRecord('test',logging.ERROR,'',1,'failed token=test-napcat-token-123456',(),None))
    assert 'test-napcat-token-123456' not in text
    assert '[redacted]' in text
