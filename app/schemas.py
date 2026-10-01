from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Login(StrictModel):
    name: str=Field(default='admin',max_length=100)
    password: str=Field(min_length=1,max_length=1000)

class PasswordChange(StrictModel):
    old_password: str=Field(max_length=1000)
    new_password: str=Field(min_length=12,max_length=1000)

class MemoryInput(StrictModel):
    content: str=Field(min_length=1,max_length=20000)
    half_life: float=Field(default=172800,gt=0,le=315360000,allow_inf_nan=False)
    scope: Literal['group','persona_shared']='group'
    group_id: str|None=Field(default=None,max_length=20,pattern=r'^\d+$')
    @model_validator(mode='after')
    def scope_group(self):
        if not self.content.strip(): raise ValueError('记忆内容不能为空')
        if self.scope=='group' and (not self.group_id or not self.group_id.isdigit()):
            raise ValueError('群内记忆需要有效群号')
        if self.scope=='persona_shared': self.group_id=None
        return self

class ImportInput(StrictModel):
    records: list[MemoryInput]=Field(max_length=100)
    apply: StrictBool=False

class ConfigInput(StrictModel):
    values: dict

class QQInput(StrictModel):
    whitelist_groups: list[str]=Field(max_length=100)
    name_mapping: dict=Field(default_factory=dict)
    @field_validator('whitelist_groups')
    @classmethod
    def groups(cls, values):
        if any(not g.isdigit() or len(g)>20 for g in values): raise ValueError('群号必须为数字')
        return values
    @field_validator('name_mapping')
    @classmethod
    def names(cls,value):
        if len(value)>100: raise ValueError('姓名映射群数超过上限')
        for group,members in value.items():
            if not isinstance(group,str) or not group.isdigit() or len(group)>20 or not isinstance(members,dict) or len(members)>500:
                raise ValueError('姓名映射应为 群号 -> QQ号 -> 显示名')
            for qq,name in members.items():
                if not isinstance(qq,str) or not qq.isdigit() or len(qq)>20 or not isinstance(name,str) or not 1<=len(name)<=100:
                    raise ValueError('姓名映射账号或显示名无效')
        return value

class PauseInput(StrictModel):
    paused: StrictBool

class NoteInput(StrictModel):
    group_id: str=Field(pattern=r'^\d+$',max_length=20)
    name: str=Field(min_length=1,max_length=32)
    text: str=Field(max_length=1500)

class MaintenanceInput(StrictModel):
    kind: Literal['backup','rebuild_indexes','self_test','sleep_cleanup','initialize']

class LinkInput(StrictModel):
    src: str
    tgt: str
    weight: float=Field(gt=0,le=1,allow_inf_nan=False)
    type: Literal['causal','temporal','semantic']='semantic'

class NoteEditInput(StrictModel):
    group_id: str=Field(pattern=r'^\d+$',max_length=20)
    name: str=Field(min_length=1,max_length=32)
    old_text: str=Field(min_length=1,max_length=1500)
    new_text: str=Field(default='',max_length=1500)
