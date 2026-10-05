from __future__ import annotations

from decimal import Decimal
import numbers
import re


_INTEGRAL_TEXT_RE = re.compile(r"^[+-]?\d+\.0+$")


def normalize_feature_id(value: object) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if _INTEGRAL_TEXT_RE.match(text):
            return text.split(".", 1)[0].lstrip("+")
        return text

    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return str(int(value))
        return format(value.normalize(), "f")

    if isinstance(value, numbers.Integral) and not isinstance(value, bool):
        return str(int(value))

    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else str(value)

    return str(value).strip()