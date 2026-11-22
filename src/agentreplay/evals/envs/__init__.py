"""Construct a fresh isolated environment for each task/trial."""

from .base import Environment, EnvironmentError
from .calc import Calculator
from .files import Files
from .kv import KeyValue
from .search import Search
from .tickets import Tickets

ENVIRONMENTS = {
    "calc": Calculator,
    "kv": KeyValue,
    "files": Files,
    "search": Search,
    "tickets": Tickets,
}


def make_environment(name: str, seed: int) -> Environment:
    if name not in ENVIRONMENTS:
        raise EnvironmentError(f"unknown environment {name!r}")
    return ENVIRONMENTS[name](seed)
