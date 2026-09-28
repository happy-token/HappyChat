"""Persisted, platform-wide HappyChat chat model policy."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading

LOCK = threading.RLock()


def policy_path() -> Path:
    return Path(os.environ.get('HAPPYCHAT_MODEL_POLICY_PATH', '/data/model-policy.json'))


def validate(value):
    if not isinstance(value, dict) or set(value) != {'configured', 'groups', 'default_model'}:
        raise ValueError('设置字段不完整')
    if type(value['configured']) is not bool or not isinstance(value['groups'], list) or len(value['groups']) > 200:
        raise ValueError('分组设置无效')
    seen = set()
    for group in value['groups']:
        if not isinstance(group, dict) or set(group) != {'id', 'enabled', 'models'}:
            raise ValueError('分组设置无效')
        name = group['id']
        if not isinstance(name, str) or not name or len(name) > 64 or name in seen:
            raise ValueError('分组名称无效或重复')
        seen.add(name)
        if type(group['enabled']) is not bool:
            raise ValueError('分组启用状态无效')
        models = group['models']
        if models is not None and (not isinstance(models, list) or len(models) > 2000 or any(not isinstance(m, str) or not m or len(m) > 256 for m in models)):
            raise ValueError('模型白名单无效')
    if not isinstance(value['default_model'], str) or len(value['default_model']) > 512:
        raise ValueError('默认模型无效')
    return value


def load():
    with LOCK:
        try:
            return validate(json.loads(policy_path().read_text()))
        except FileNotFoundError:
            return {'configured': False, 'groups': [], 'default_model': ''}


def save(value):
    validate(value)
    with LOCK:
        path = policy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.model-policy-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(value, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def group_of(model):
    return model.get('group') or (model['id'].split('::', 1)[0] if '::' in model['id'] else 'default')


def apply(models, policy=None):
    policy = load() if policy is None else policy
    if not policy['configured']:
        return models
    groups = {g['id']: (i, g) for i, g in enumerate(policy['groups'])}
    result = []
    for model in models:
        group_id = group_of(model)
        entry = groups.get(group_id)
        if entry is None or not entry[1]['enabled']:
            continue
        allowed = entry[1]['models']
        if allowed is not None and model['id'] not in allowed:
            continue
        result.append(model)
    return sorted(result, key=lambda m: (groups[group_of(m)][0], m['id']))
