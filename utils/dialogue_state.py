"""Short-term interpretation state is keyed by QQ group, not process-wide."""
import copy
import threading
from utils.session_context import current_group

DEFAULT_STATE = {"参与者": [], "最近话题": "无"}
_states = {}
_lock = threading.RLock()

def get_state():
    with _lock:
        return copy.deepcopy(_states.get(current_group.get(), DEFAULT_STATE))

def set_state(new_state):
    if not isinstance(new_state, dict):
        raise ValueError("Dialogue state must be an object")
    with _lock:
        _states[current_group.get()] = {
            "参与者": list(new_state.get("参与者", [])),
            "最近话题": str(new_state.get("最近话题", "无")),
        }

def reset_state():
    with _lock:
        _states.pop(current_group.get(), None)

def export_states():
    with _lock:
        return copy.deepcopy({str(k): v for k, v in _states.items() if k is not None})

def import_states(states):
    with _lock:
        _states.clear()
        _states.update(copy.deepcopy(states))
