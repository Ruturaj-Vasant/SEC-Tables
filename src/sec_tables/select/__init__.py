"""Selection backends: DOM for parseable HTML, text for ASCII/SGML filings."""
from . import chain, dom, text

__all__ = ["chain", "dom", "text"]
