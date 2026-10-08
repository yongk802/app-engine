"""Small, explicit JSON Schema profile for declarative app tools (no code loading)."""
from __future__ import annotations

import math


_TYPES = {'object': dict, 'array': list, 'string': str, 'integer': int, 'number': (int, float), 'boolean': bool, 'null': type(None)}
_KEYS = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'description', 'minimum', 'maximum', 'minLength', 'maxLength', 'minItems', 'maxItems'}


def check_schema(schema: dict, depth: int = 0) -> None:
    if depth > 12 or not isinstance(schema, dict) or set(schema) - _KEYS or schema.get('type') not in _TYPES:
        raise ValueError('Unsupported input schema')
    kind = schema['type']
    if 'description' in schema and not isinstance(schema['description'], str):
        raise ValueError('Invalid schema description')
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']):
        raise ValueError('Invalid enum')
    for key in ('minimum', 'maximum', 'minLength', 'maxLength', 'minItems', 'maxItems'):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError('Invalid schema bound')
    if kind == 'object':
        props = schema.get('properties', {})
        required = schema.get('required', [])
        if not isinstance(props, dict) or len(props) > 64 or schema.get('additionalProperties') is not False:
            raise ValueError('Object schemas must disallow extra properties')
        if not isinstance(required, list) or any(not isinstance(k, str) or k not in props for k in required):
            raise ValueError('Invalid required properties')
        for child in props.values():
            check_schema(child, depth + 1)
    if kind == 'array':
        check_schema(schema.get('items'), depth + 1)


def validate(value, schema: dict, depth: int = 0) -> None:
    kind = schema['type']
    if depth > 20 or not isinstance(value, _TYPES[kind]) or (kind in ('integer', 'number') and isinstance(value, bool)):
        raise ValueError('Tool arguments do not match the input schema')
    if 'enum' in schema and not any(type(value) is type(option) and value == option for option in schema['enum']):
        raise ValueError('Tool argument is outside its allowed values')
    if kind == 'object':
        props = schema.get('properties', {})
        if set(schema.get('required', [])) - value.keys() or (schema.get('additionalProperties') is False and value.keys() - props.keys()):
            raise ValueError('Tool arguments contain missing or unexpected fields')
        for name, item in value.items():
            if name in props:
                validate(item, props[name], depth + 1)
    elif kind == 'array':
        for item in value:
            validate(item, schema['items'], depth + 1)
    if kind in ('number', 'integer'):
        if not math.isfinite(value) or value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise ValueError('Tool argument is outside its allowed range')
    if kind in ('string', 'array'):
        lower, upper = ('minLength', 'maxLength') if kind == 'string' else ('minItems', 'maxItems')
        if not schema.get(lower, 0) <= len(value) <= schema.get(upper, math.inf):
            raise ValueError('Tool argument has an invalid length')
