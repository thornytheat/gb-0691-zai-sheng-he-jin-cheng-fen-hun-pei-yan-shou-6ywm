"""配方版本登记。

配方版本与文件规则版本（manifest.rule_version）相互独立：
- 文件规则版本描述“计算程序/证据格式”，随代码升级；
- 配方版本描述“质量口径”（元素目标区间、来料可配条件），随订单/工艺更新。

每个版本的定义一经发布即固化；修改定义会生成新的指纹（digest），
已落库的验收结论在读取时据此识别“配方规则已更新”并重算。
"""
import hashlib
import json
from decimal import Decimal

from app.errors import DomainError

# V1 为投料版（教学配方）；V2 为新订单口径。
RECIPES = {
    'V1': {
        'label': 'V1',
        'name': '投料版',
        'status': '已冻结',
        'effective': '2026-01-01',
        'targets': {'Cu': ('7.5', '12.5'), 'Zn': ('3', '7')},
        'max_moisture': '5',
        'revisions': ('1',),
        'note': '投料时锁定的旧配方，在制炉次沿用。',
    },
    'V2': {
        'label': 'V2',
        'name': '验收版',
        'status': '已发布',
        'effective': '2026-11-01',
        'targets': {'Cu': ('8', '12'), 'Zn': ('3.5', '7.5')},
        'max_moisture': '5',
        'revisions': ('1', '2'),
        'note': '新订单质量口径，未投料炉次与重判炉次使用。',
    },
}
DEFAULT_RECIPE = 'V1'
RECIPE_ORDER = ('V1', 'V2')


def normalize_version(value, default=None):
    """把 V1/v1/1 等写法归一为登记版本号。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default or DEFAULT_RECIPE
    text = str(value).strip().upper()
    if text.startswith('V'):
        text = text[1:]
    if not text.isdigit() or ('V' + text) not in RECIPES:
        raise DomainError('RECIPE_VERSION', '配方版本不存在：%s' % value)
    return 'V' + text


def definition(version=None):
    return RECIPES[normalize_version(version)]


def targets(version, element):
    item = definition(version)['targets'].get(element)
    if item is None:
        raise DomainError('ELEMENT', '未知元素：%s' % element)
    return Decimal(item[0]), Decimal(item[1])


def max_moisture(version=None):
    return Decimal(definition(version)['max_moisture'])


def accepts_revision(version, revision):
    return str(revision) in definition(version)['revisions']


def digest(version=None):
    """配方定义指纹：用于识别规则更新后需要重算的旧结论。"""
    body = json.dumps(definition(version), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]


def catalogue():
    """给前端/接口的版本目录（不含内部元组，全部为可序列化值）。"""
    return [
        {
            'version': key,
            'label': item['label'],
            'name': item['name'],
            'status': item['status'],
            'effective': item['effective'],
            'targets': {element: {'min': bounds[0], 'max': bounds[1]}
                        for element, bounds in item['targets'].items()},
            'max_moisture': item['max_moisture'],
            'revisions': list(item['revisions']),
            'digest': digest(key),
            'note': item['note'],
        }
        for key, item in RECIPES.items()
    ]


POLICY_ALIASES = {
    'charging': '投料版',
    'charge': '投料版',
    '投料': '投料版',
    '投料版': '投料版',
    'lock': '投料版',
    '锁定': '投料版',
    'acceptance': '验收版',
    'accept': '验收版',
    '验收': '验收版',
    '验收版': '验收版',
    'rejudge': '验收版',
    '重判': '验收版',
}
POLICY_DEFAULT = '投料版'
# 口径 → 目标版本；投料版锁定旧配方，验收版对齐新订单。
POLICY_VERSION = {'投料版': 'V1', '验收版': 'V2'}


def policy_version(value):
    return POLICY_VERSION[normalize_policy(value)]


def normalize_policy(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return POLICY_DEFAULT
    key = str(value).strip().lower()
    if key in POLICY_ALIASES:
        return POLICY_ALIASES[key]
    raise DomainError('RECIPE_POLICY', '切换口径无效：%s' % value)
