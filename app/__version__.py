"""Single source of truth for the project version string.

Bump this and recomputed values flow through to:
- /health response (``version`` field)
- /cluster/status response
- FastAPI app factory ``version=`` arg (drives /openapi.json and /docs title)
- OpenTelemetry service tags via init_tracer / set_service_info

Older releases hardcoded the version in 5 places in ``app/main.py`` plus
``app/api/cluster.py``; v2.7.6 consolidates here.
"""
__version__ = "3.0.24"
