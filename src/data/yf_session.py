"""
Robust Yahoo Finance session.

Sprint 9: yfinance 0.2.40 (Feb 2024) está ROTO. Yahoo cambió el endpoint y devuelve
HTML/challenge en vez de JSON cuando detecta un cliente "no-browser" desde data-center IPs
(como la del VPS Contabo 13.140.181.29).

Este módulo:
1. Crea una `requests.Session` por proceso con `curl_cffi` impersonando Chrome 124.
   Eso bypassea el anti-bot challenge de Yahoo.
2. Cachea la session en `functools.lru_cache` (una por proceso).
3. La expone como `get_yf_session()` — yfinance 1.x la acepta via el kwarg `session=`.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

_session = None
_session_failed = False


def get_yf_session():
    """Devuelve una `requests.Session` que impersona Chrome, o `None` si curl_cffi no está."""
    global _session, _session_failed
    if _session is not None:
        return _session
    if _session_failed:
        return None
    try:
        from curl_cffi import requests as cffi_requests
        _session = cffi_requests.Session(impersonate="chrome124")
        log.info("[yfinance] curl_cffi session created (impersonate=chrome124)")
        return _session
    except Exception as e:
        _session_failed = True
        log.warning(f"[yfinance] curl_cffi unavailable, falling back to plain requests: {e}")
        return None
