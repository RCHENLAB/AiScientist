"""Compatibility import for historical dataset-preflight callers; no science implementation."""
from ._lib import dataset_preflight as _preflight

def __getattr__(name):
    return getattr(_preflight, name)
