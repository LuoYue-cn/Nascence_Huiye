"""A QQ group context follows work into the single core executor."""
from contextvars import ContextVar
from contextlib import contextmanager

current_group = ContextVar("huiye_group", default=None)
current_source = ContextVar("huiye_source", default="internal")
current_message = ContextVar("huiye_message", default=None)

@contextmanager
def group_context(group_id, source="qq", message_id=None):
    tokens = (current_group.set(str(group_id) if group_id is not None else None),
              current_source.set(source), current_message.set(message_id))
    try:
        yield
    finally:
        current_message.reset(tokens[2])
        current_source.reset(tokens[1])
        current_group.reset(tokens[0])

current_deadline = ContextVar('huiye_model_deadline',default=None)

def model_timeout(default=None):
    import time
    from config.api_config import config
    limit=float(config.get('model_timeout_seconds',45) if default is None else default)
    deadline=current_deadline.get()
    if deadline is not None:
        remaining=deadline-time.monotonic()
        if remaining<=0: raise TimeoutError('QQ processing deadline exceeded')
        limit=min(limit,remaining)
    return limit
