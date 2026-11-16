"""Küçük JSON Schema doğrulayıcı (yalnızca kullandığımız alt küme; ek bağımlılık yok).

Ollama'nın `format` alanı modeli şemaya uymaya zorlar, ancak uygulama buna güvenmez:
her yanıt burada yeniden doğrulanır (talimat: "Bütün JSON çıktıları uygulama tarafından doğrulansın").

Desteklenen anahtarlar: type (tek veya liste, "null" dahil), enum, const, properties, required,
additionalProperties (yalnızca false), items, minItems, maxItems, minLength, maxLength,
minimum, maximum, pattern.
"""
from __future__ import annotations

import re
from typing import Any

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


class SchemaError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def _type_ok(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    expected = _TYPES.get(name)
    return expected is not None and isinstance(value, expected)


def validation_errors(value: Any, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_type_ok(value, n) for n in names):
            return [f"{path}: tür {'/'.join(names)} olmalı"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: {schema['const']!r} olmalı")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: izin verilmeyen değer {value!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: çok kısa")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: çok uzun ({len(value)} > {schema['maxLength']})")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: biçim uygun değil")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: en az {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: en fazla {schema['maximum']}")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key}: zorunlu alan eksik")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: tanımsız alan")
        for key, sub in properties.items():
            if key in value:
                errors.extend(validation_errors(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: en az {schema['minItems']} öğe")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: en fazla {schema['maxItems']} öğe")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validation_errors(item, item_schema, f"{path}[{index}]"))
    return errors


def validate(value: Any, schema: dict) -> Any:
    errors = validation_errors(value, schema)
    if errors:
        raise SchemaError(errors)
    return value
