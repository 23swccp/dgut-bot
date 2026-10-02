"""Allowlisted structural export; raw page text, field values and URL parameters never go to disk."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
from urllib.parse import urlsplit, parse_qs

UI_TEXT = set('单选题 多选题 判断题 填空题 简答题 正确 错误 提交 开始答题 下一页 上一页 下一题 上一题 确定 取消 继续 留在本页 确定离开 我知道了 知道了 重试 继续学习 本章统计 没有了'.split())
SAFE_PATH = set('api lms courses course students question questions quiz quizzes answer answers submit save record records learnCourse learnCourse.html agent-course.html index.html'.split())
SENSITIVE = re.compile(r'password|passwd|secret|token|authorization|cookie|credential|sessionid|student|user.?id|phone|email', re.I)
SYMBOL = re.compile(r'^[A-Za-z_$][A-Za-z0-9_$-]{0,79}$')


class Redactor:
    def __init__(self):
        self._key = secrets.token_bytes(32)

    def alias(self, value, kind='value'):
        digest = hmac.new(self._key, str(value).encode('utf-8'), hashlib.sha256).hexdigest()[:12]
        return f'{kind}_{digest}'

    def text(self, value):
        value = str(value or '')
        stripped = value.strip()
        if not stripped:
            return value
        if stripped in UI_TEXT or re.fullmatch(r'[A-Z][.。]?', stripped):
            return stripped
        return '[' + self.alias(value, 'text') + ']'

    def symbol(self, value):
        value = str(value)
        if SYMBOL.fullmatch(value) and not re.search(r'\d{5,}|[a-fA-F0-9]{20,}', value):
            return value
        return self.alias(value, 'symbol')

    def url(self, value):
        try:
            parsed = urlsplit(str(value))
            if parsed.scheme not in {'http', 'https'}:
                return {'scheme': parsed.scheme if parsed.scheme in {'about', 'blob', 'data'} else 'other', 'reference': self.alias(value, 'url')}
            path = '/'.join(p if p in SAFE_PATH or not p else self.alias(p, 'segment') for p in parsed.path.split('/'))
            return {'scheme': parsed.scheme, 'host': parsed.hostname, 'port': parsed.port, 'path': path,
                    'hasQuery': bool(parsed.query), 'hasFragment': bool(parsed.fragment),
                    'querySchema': self.shape(parse_qs(parsed.query, keep_blank_values=True, max_num_fields=150))}
        except ValueError:
            return {'invalid': True}

    def shape(self, value, depth=0):
        if depth > 10:
            return {'type': 'truncated', 'reason': 'depth'}
        if isinstance(value, dict):
            return {'type': 'object', 'properties': {self.symbol(k): {'type': 'redacted'} if SENSITIVE.search(k) else self.shape(v, depth + 1)
                    for k, v in list(value.items())[:150]}, 'truncated': len(value) > 150}
        if isinstance(value, list):
            variants = []
            for item in value[:20]:
                schema = self.shape(item, depth + 1)
                if schema not in variants:
                    variants.append(schema)
            return {'type': 'array', 'length': len(value), 'variants': variants, 'truncated': len(value) > 20}
        return {'type': 'null' if value is None else 'boolean' if isinstance(value, bool) else 'number' if isinstance(value, (int, float)) else 'string'}

    def body(self, raw, content_type=''):
        if not raw:
            return {'state': 'empty'}
        if len(raw.encode('utf-8')) > 1_048_576:
            return {'state': 'omitted', 'reason': 'size-limit'}
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            if content_type.split(';')[0].strip().lower() == 'application/x-www-form-urlencoded':
                try:
                    fields = parse_qs(raw, keep_blank_values=True, max_num_fields=150)
                    # Decode JSON embedded in a form field, but export only its schema.
                    decoded = {}
                    for name, values in fields.items():
                        decoded[name] = []
                        for item in values:
                            try:
                                decoded[name].append(json.loads(item))
                            except (ValueError, RecursionError):
                                decoded[name].append(item)
                    return {'state': 'captured', 'encoding': 'form-urlencoded', 'schema': self.shape(decoded)}
                except ValueError:
                    return {'state': 'omitted', 'reason': 'form-field-limit'}
            return {'state': 'omitted', 'reason': 'not-json', 'bytes': len(raw.encode('utf-8'))}
        result = {'state': 'captured', 'schema': self.shape(value)}
        if isinstance(value, dict):
            result['outcome'] = {k: v for k, v in value.items() if k in {'success', 'ok', 'accepted', 'status', 'code'}
                                 and (isinstance(v, bool) or type(v) in (int, float) and -1000 <= v <= 1000)}
        return result

    def bindings(self, value):
        result = {}
        for part in str(value).split(',')[:40]:
            name, sep, expression = part.partition(':')
            if sep:
                name, expression = name.strip(), expression.strip()
                result[self.symbol(name)] = expression if re.fullmatch(r'[A-Za-z_$][\w.$]{0,100}', expression) else '[expression]'
        return result

    def model(self, value, field=''):
        if isinstance(value, dict):
            return {self.symbol(k): self.model(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.model(v, field) for v in value]
        if field in {'unreadFunction', 'status', 'isHide', 'length', 'contentType'} and isinstance(value, (bool, int, float)):
            return value
        if field in {'unavailable', 'kind'} and value in {'observable-read-failed', 'accessor-not-invoked', 'object', 'array'}:
            return value
        if field == 'keys':
            return self.symbol(value)
        return None if value is None else self.alias(value, 'identity')

    def snapshot(self, raw):
        result = {k: raw[k] for k in ('readyState', 'viewport', 'features', 'limits', 'revision')}
        result.update(url=self.url(raw['url']), viewModel=self.model(raw['viewModel']), nodes=[])
        for original in raw['nodes']:
            node = {k: v for k, v in original.items() if k not in {'text', 'value', 'attrs'}}
            if 'text' in original:
                node.update(text=self.text(original['text']), textLength=len(original['text']))
            if original.get('value') is not None:
                node.update(value=('[' + self.alias(original['value'], 'input') + ']') if original['value'] else '', valueLength=len(original['value']))
            attrs = {}
            for k, v in original.get('attrs', {}).items():
                if k == 'class':
                    attrs[k] = ' '.join(self.symbol(p) for p in v.split())
                elif k in {'id', 'for', 'name'}:
                    attrs[k] = self.alias(v, 'dom')
                elif k == 'data-bind':
                    attrs[k] = self.bindings(v)
                elif k in {'src', 'href'}:
                    attrs[k] = self.url(v)
                elif k in {'aria-label', 'placeholder'}:
                    attrs[k] = self.text(v)
                elif k in {'type', 'role', 'contenteditable', 'aria-disabled', 'aria-checked', 'aria-selected', 'aria-expanded', 'colspan', 'rowspan'}:
                    attrs[k] = v if re.fullmatch(r'\d{1,3}|true|false|mixed', v) else self.symbol(v)
            node['attrs'] = attrs
            result['nodes'].append(node)
        return result


def render_snapshot(frame):
    """Inert structural fixture: no original scripts, URLs, CSS resources or request handlers."""
    children = {}
    for node in frame.get('nodes', []):
        children.setdefault(node['parent'], []).append(node)
    safe_tags = set('div span p a button input textarea select option label ul ol li main section header footer nav article aside h1 h2 h3 h4 table tbody thead tr td th br hr strong em b i form fieldset legend video audio canvas iframe img svg math body'.split())
    void = {'input', 'br', 'hr', 'img'}

    def render(parent):
        pieces = []
        for n in children.get(parent, []):
            if n['tag'] == '#text':
                pieces.append(html.escape(n['text']))
                continue
            tag = n['tag'] if n['tag'] in safe_tags else 'div'
            if tag in {'iframe', 'video', 'audio', 'img', 'canvas', 'svg', 'math', 'form', 'body'}:
                tag = 'div'  # A visual placeholder; originals are described in JSON.
            attrs = {'data-capture-node': str(n['node'])}
            for k, v in n.get('attrs', {}).items():
                if k in {'src', 'href', 'for'}:
                    continue
                attrs[k] = ', '.join(f'{a}: {b}' for a, b in v.items()) if k == 'data-bind' else str(v)
            for k in ('disabled', 'hidden', 'checked'):
                if n.get(k):
                    attrs[k] = k
            if n.get('value') is not None:
                attrs['value'] = n['value']
            # All style values are computed primitives, never external resource references.
            attrs['style'] = ';'.join(f'{k}:{v}' for k, v in n.get('styles', {}).items() if not re.search(r'url|expression|[<>]', v, re.I))
            encoded = ''.join(f' {k}="{html.escape(v, quote=True)}"' for k, v in attrs.items())
            pieces.append(f'<{tag}{encoded}>')
            if tag not in void:
                pieces.append(render(n['node']))
                pieces.append(f'</{tag}>')
        return ''.join(pieces)
    return '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; form-action \'none\'"><title>脱敏结构快照（静态）</title>' + render(None)
