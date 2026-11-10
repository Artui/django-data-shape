from __future__ import annotations

import datetime
import hashlib
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import Enum

from django_data_shape.databases.unhashable_shape import UnhashableShape
from django_data_shape.declaration.shape import Shape
from django_data_shape.types.canonical import Canonical

# Mixed into every digest so that a change to the encoding below cannot be
# mistaken for a shape that happens to hash the same. Bump it whenever a tag
# changes meaning: every existing key is then a different key, which costs one
# rebuild and removes an entire class of stale answer.
_FORMAT = b"django-data-shape shape-digest 1"

# 16 bytes, so the hex form is 32 characters and a template database name built
# from it fits inside PostgreSQL's 63-byte identifier limit with room for a
# prefix. Collisions at 128 bits are not a risk anybody has to reason about.
_DIGEST_BYTES = 16

# One byte per kind, so that two values of different types can never encode to
# the same bytes -- 1, "1", True and Decimal("1") are four different
# declarations and must be four different digests.
_NONE = b"n"
_BOOL = b"b"
_INT = b"i"
_FLOAT = b"f"
_STR = b"s"
_BYTES = b"y"
_DECIMAL = b"d"
_DATETIME = b"T"
_DATE = b"D"
_TIME = b"t"
_TIMEDELTA = b"L"
_UUID = b"u"
_ENUM = b"e"
_SEQUENCE = b"q"
_MAPPING = b"m"
_CANONICAL = b"c"


def shape_digest(shape: Shape) -> str:
    """A hex digest of everything in ``shape`` that decides the data, stable across runs."""
    hasher = hashlib.blake2b(digest_size=_DIGEST_BYTES)
    hasher.update(_FORMAT)
    _feed(hasher, shape, "this shape")
    return hasher.hexdigest()


def _feed(hasher: hashlib.blake2b, value: object, where: str) -> None:
    """Absorb one value by kind; leaf types come first, since ``Decimal`` has ``canonical()``."""
    if value is None:
        _tagged(hasher, _NONE, b"")
    elif isinstance(value, bool):
        _tagged(hasher, _BOOL, b"1" if value else b"0")
    elif isinstance(value, Enum):
        _tagged(hasher, _ENUM, _type_name(value).encode())
        _feed(hasher, value.value, where)
    elif isinstance(value, int):
        _tagged(hasher, _INT, str(value).encode())
    elif isinstance(value, float):
        _tagged(hasher, _FLOAT, value.hex().encode())
    elif isinstance(value, str):
        _tagged(hasher, _STR, value.encode())
    elif isinstance(value, bytes):
        _tagged(hasher, _BYTES, value)
    elif isinstance(value, Decimal):
        _tagged(hasher, _DECIMAL, str(value).encode())
    elif isinstance(value, datetime.datetime):
        _tagged(hasher, _DATETIME, value.isoformat().encode())
    elif isinstance(value, datetime.date):
        _tagged(hasher, _DATE, value.isoformat().encode())
    elif isinstance(value, datetime.time):
        _tagged(hasher, _TIME, value.isoformat().encode())
    elif isinstance(value, datetime.timedelta):
        _tagged(
            hasher,
            _TIMEDELTA,
            f"{value.days}:{value.seconds}:{value.microseconds}".encode(),
        )
    elif isinstance(value, uuid.UUID):
        _tagged(hasher, _UUID, value.hex.encode())
    elif isinstance(value, Canonical):
        _tagged(hasher, _CANONICAL, _type_name(value).encode())
        _feed(hasher, value.canonical(), f"{where} -> {type(value).__name__}")
    elif isinstance(value, Mapping):
        _tagged(hasher, _MAPPING, str(len(value)).encode())
        for key, item in value.items():
            _feed(hasher, key, where)
            _feed(hasher, item, f"{where}[{key!r}]")
    elif isinstance(value, Sequence):
        _tagged(hasher, _SEQUENCE, str(len(value)).encode())
        for item in value:
            _feed(hasher, item, where)
    else:
        raise UnhashableShape(
            f"{where} holds {value!r}, and this package cannot say what it contributes to the "
            "data, so it will not pretend to. A cache key that leaves something out is a key "
            "that stays the same after the data has changed, which serves a stale database to a "
            "suite that will pass. Derived and KeyFunction are the usual answer: each wraps a "
            "callable, and a callable is code rather than data -- two lambdas share a name, and "
            "even identical bytecode returns something different when a constant it reads is "
            "edited elsewhere. Build this shape with build() instead, or implement Canonical on "
            "a declaration that really is data."
        )


def _tagged(hasher: hashlib.blake2b, tag: bytes, payload: bytes) -> None:
    """One kind byte, then the payload's length and the payload, so no encoding is ambiguous."""
    hasher.update(tag)
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)


def _type_name(value: object) -> str:
    """Module and name of a value's class, so two same-named classes do not collide."""
    kind = type(value)
    return f"{kind.__module__}.{kind.__qualname__}"
