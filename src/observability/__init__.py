"""
Sprint 46R (audit M11.4): observability helpers package.

Public surface (so far):
  - dead_mans_switch.ping_dead_mans_switch
  - dead_mans_switch.get_ping_state

Sprint 46R (audit M11.3): the healthcheck helpers live in
src.api.server (/api/health). They read the dead-man's-switch
state via dead_mans_switch.get_ping_state() and surface it in
the health response body.
"""
