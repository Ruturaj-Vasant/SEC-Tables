"""SEC filing proxy for the sec-tables browser app.

Server-side fetching, browser-side extraction. See ../ARCHITECTURE.md.
"""
from .core import FilingService, ProxyError, SUPPORTED_FORMS, SUPPORTED_TABLES

__all__ = ["FilingService", "ProxyError", "SUPPORTED_FORMS", "SUPPORTED_TABLES"]
