"""How a table's primary keys are decided."""

from django_data_shape.keys.key_function import KeyFunction
from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.keys.sequential_keys import SequentialKeys
from django_data_shape.keys.sql_keys import SqlKeys
from django_data_shape.keys.uuid_keys import UuidKeys

__all__ = ["KeyFunction", "KeyStrategy", "SequentialKeys", "SqlKeys", "UuidKeys"]
