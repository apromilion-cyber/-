"""Strict JSON shared by user inputs and backup verification."""

import json


def reject_constant(value):
    raise ValueError("JSON은 NaN/Infinity를 허용하지 않습니다")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 객체에 중복 키가 있습니다")
        result[key] = value
    return result


def parse_json(value: str):
    return json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_keys)
