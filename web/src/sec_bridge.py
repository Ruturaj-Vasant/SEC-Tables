"""The Python half of the browser bridge.

Installed into site-packages as `sec_bridge` at preparation time and imported
once. Everything the worker calls lives here, and nothing is ever written to
the interpreter's global namespace: a per-request global is a leak that outlives
the request, and the worker asserts the namespace is unchanged after every call.

Design rules:

* **Return a JSON string, never an object.** A Python object crossing into JS
  becomes a PyProxy the caller must destroy by hand, and one missed `destroy()`
  pins Python memory for the life of the page. A `str` converts to a JS string
  outright, so the steady-state proxy count for an extraction is zero.
* **Call the real API.** `sec_tables.extract()` with bytes and a `date`, exactly
  as `cli.py` calls it. Nothing here reimplements selection, normalisation or
  assembly, and nothing here knows the name of any table.
* **Do not import `sec_tables.fetch`.** It is not imported by the package, and
  the browser has no business making SEC requests: no User-Agent can be honestly
  declared from a page, and `/Archives` serves no permissive CORS anyway.
"""
from __future__ import annotations

import gc
import json
import sys
import time
from datetime import date

import sec_tables as st
from sec_tables.types import Extraction, Table

RESULT_SCHEMA_VERSION = 1


def _to_bytes(document) -> bytes:
    """Accept a JS Uint8Array (as a JsBuffer) or Python bytes.

    The worker always passes a typed-array view over the transferred
    ArrayBuffer, so this is one copy into the wasm heap and no base64 anywhere.
    """
    to_bytes = getattr(document, "to_bytes", None)
    if to_bytes is not None:
        return to_bytes()
    return bytes(document)


def _parse_date(filing_date):
    if not filing_date:
        return None
    y, m, d = (int(p) for p in str(filing_date).split("-"))
    return date(y, m, d)


def _result(extraction: Extraction, profile: str, execution_ms: float) -> dict:
    """Flatten an `Extraction` into the wire shape.

    `columns` follows `Table.to_csv()`: roles when they were assigned, the raw
    header otherwise, so the browser shows the same column names the library
    writes to CSV.
    """
    table: Table | None = extraction.table
    return {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "ok": extraction.ok,
        # The resolved profile name, which differs from the requested one when
        # an alias was used.
        "profile": extraction.meta.get("profile", profile),
        "era": extraction.era,
        "backend": extraction.backend.value if extraction.backend is not None else None,
        "columns": list(table.roles or table.header) if table is not None else [],
        "rows": [list(row) for row in table.rows] if table is not None else [],
        "flags": list(extraction.flags),
        "reviewRequired": extraction.needs_review,
        "provenance": list(extraction.provenance_flags),
        "metadata": dict(extraction.meta),
        # Overwritten by the worker with its own measurement; present here so a
        # result is complete even when read directly from Python.
        "preparationMs": 0.0,
        "executionMs": execution_ms,
    }


def extract_json(document, filing_date, profile) -> str:
    """One extraction. Returns a JSON string matching `ExtractionResult`.

    `default=str` on the encoder is deliberate. `Extraction.meta` is an open
    `dict[str, Any]` that the library is free to add to, and a future
    non-serializable value there should degrade to its repr rather than break
    every extraction in the browser.
    """
    raw = _to_bytes(document)
    started = time.perf_counter()
    extraction = st.extract(raw, profile=profile, filing_date=_parse_date(filing_date))
    execution_ms = (time.perf_counter() - started) * 1000.0
    return json.dumps(_result(extraction, profile, execution_ms), default=str)


def runtime_info() -> str:
    from lxml import etree

    return json.dumps(
        {
            "pythonVersion": ".".join(str(p) for p in sys.version_info[:3]),
            "lxmlVersion": ".".join(str(p) for p in etree.LXML_VERSION[:3]),
            "secTablesVersion": st.__version__,
            "availableProfiles": st.available_tables(),
        }
    )


def diagnostics() -> str:
    """Python-side leak counters.

    `liveExtractions` is the load-bearing one: it counts real `Extraction`
    objects still reachable after a collection, independent of whatever the JS
    side believes about its own proxies. A bridge that stashed results in a
    cache or a global would show up here and nowhere else.
    """
    gc.collect()
    live_extractions = 0
    live_tables = 0
    for obj in gc.get_objects():
        if isinstance(obj, Extraction):
            live_extractions += 1
        elif isinstance(obj, Table):
            live_tables += 1
    # The interpreter's own global namespace, read from here rather than from
    # JS: `pyodide.globals.keys()` would allocate a proxy per call, which is
    # exactly the thing these counters exist to detect.
    main_globals = sorted(
        k for k in vars(sys.modules["__main__"]) if not k.startswith("__")
    )
    return json.dumps(
        {
            "liveExtractions": live_extractions,
            "liveTables": live_tables,
            "mainGlobals": main_globals,
        }
    )


def raise_for_test(kind: str) -> str:
    """Fault injection, used by the browser suite to exercise error conversion.

    This exists because no *valid* filing input reaches a Python `raise`:
    sec-tables reports every failure it anticipates as flags on a returned
    `Extraction`. The pathological inputs that might raise — a `colspan` of
    two billion, a `rowspan` of a million — do not raise, they expand the grid
    until the thread stops responding, which is a hang rather than an
    exception and is why cancellation is implemented by terminating the worker.
    So the error path is exercised deliberately, through the same worker code
    that wraps a real extraction, rather than left untested.
    """
    if kind == "value_error":
        raise ValueError("injected failure from sec_bridge.raise_for_test")
    if kind == "key_error":
        raise KeyError("injected-missing-key")
    raise RuntimeError(f"injected failure ({kind})")
