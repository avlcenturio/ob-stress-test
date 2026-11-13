"""Container health check: the web UI answers on /ob-health (open even when the login is switched on).
OB_HEALTH_SCHEME=https when Locust serves HTTPS (set in docker-compose.server.yml)."""
import os
import ssl
import sys
import urllib.request

scheme = os.getenv("OB_HEALTH_SCHEME", "http")
ctx = None
if scheme == "https":
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # local self-check only
try:
    urllib.request.urlopen(f"{scheme}://127.0.0.1:8089/ob-health", timeout=4, context=ctx)
except Exception:  # noqa: BLE001
    sys.exit(1)
