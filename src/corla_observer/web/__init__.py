"""The web interface: a public dashboard and coordinator pages.

- `corla_observer.web.app`: the FastAPI application (`create_app`), with
  server-rendered pages, server-sent events and security headers.
- `corla_observer.web.accounts`: coordinator accounts with scrypt password
  hashes, kept in the workspace (standard library only).
- `corla_observer.web.sessions`: server-side sessions behind signed cookies,
  CSRF tokens and the server secret (standard library only).
- `corla_observer.web.ratelimit`: in-memory rate limits (standard library only).
- `corla_observer.web.forms`: a streaming multipart parser that writes an
  upload straight to the workspace's staging directory.
- `corla_observer.web.views`: the dashboard's content, built from the event
  log and the data map (no web dependencies).

This package's ``__init__`` imports nothing, so the command line can use
`accounts` without loading the web framework.
"""
