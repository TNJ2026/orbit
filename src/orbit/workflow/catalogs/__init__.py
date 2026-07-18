"""Compile-time catalogs used to resolve workflow capabilities."""

from .extensions import ExtensionManifest, InMemoryExtensionRegistry
from .handlers import HandlerManifest, InMemoryHandlerCatalog
from .schemas import InMemorySchemaCatalog

__all__ = [
    "ExtensionManifest",
    "HandlerManifest",
    "InMemoryExtensionRegistry",
    "InMemoryHandlerCatalog",
    "InMemorySchemaCatalog",
]
