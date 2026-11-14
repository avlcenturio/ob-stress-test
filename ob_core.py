"""
Tenants for the Open Banking stress test: settings, certificates, discovery, tokens and the
consent authorisation flow (AIS, PIS and CoF). Shared by locustfile.py and ob_web.py.

Files, all in the folder Locust is started from:
  ob_tenants.json        every tenant's settings
  ob_tokens.json         tokens and pending authorisations, per tenant
  certs/<tenant-id>/     uploaded certificates and keys
"""
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import requests
import yaml

WORKDIR = os.getcwd()
TENANTS_FILE = os.getenv("OB_TENANTS_FILE", os.path.join(WORKDIR, "ob_tenants.json"))
TOKENS_FILE = os.getenv("OB_TOKENS_FILE", os.path.join(WORKDIR, "ob_tokens.json"))
LEGACY_SETTINGS = os.path.join(WORKDIR, "ob_settings.json")
CERTS_DIR = os.path.join(WORKDIR, "certs")
AREAS = ["AIS", "PIS", "CoF"]
SOURCES = ["Manual tokens", "Consent flow"]
CA_CHOICES = ["Verify against trusted CAs", "Accept self-signed certificates", "Verify with a custom CA file"]
CA_LEGACY = {"Yes": CA_CHOICES[0], "No (sandbox only)": CA_CHOICES[1], "Use CA file": CA_CHOICES[2]}
_lock = threading.RLock()

# ---------------------------------------------------------------- endpoints ----
# key, method, path, group, description, weight, ticked by default, needs (key it depends on)
E = lambda key, method, path, group, desc="", weight=1, on=True, needs=None, once=False: dict(
    key=key, method=method, path=path, group=group, desc=desc, weight=weight, on=on, needs=needs, once=once)

# UK Open Banking payment types (VRP not included). Keys in the PIS catalogue are "<type>:<call>".
PAYMENT_TYPES = {
    "domestic": {"label": "Domestic payments", "short": "Domestic", "consent": "domestic-payment-consents",
                 "payment": "domestic-payments", "id": "DomesticPaymentId", "funds": True},
    "domestic-scheduled": {"label": "Domestic scheduled payments", "short": "Domestic scheduled",
                           "consent": "domestic-scheduled-payment-consents", "payment": "domestic-scheduled-payments",
                           "id": "DomesticScheduledPaymentId", "funds": False},
    "domestic-standing-order": {"label": "Domestic standing orders", "short": "Domestic standing order",
                                "consent": "domestic-standing-order-consents", "payment": "domestic-standing-orders",
                                "id": "DomesticStandingOrderId", "funds": False},
    "international": {"label": "International payments", "short": "International",
                      "consent": "international-payment-consents", "payment": "international-payments",
                      "id": "InternationalPaymentId", "funds": True},
    "international-scheduled": {"label": "International scheduled payments", "short": "International scheduled",
                                "consent": "international-scheduled-payment-consents",
                                "payment": "international-scheduled-payments",
                                "id": "InternationalScheduledPaymentId", "funds": True},
    "international-standing-order": {"label": "International standing orders",
                                     "short": "International standing order",
                                     "consent": "international-standing-order-consents",
                                     "payment": "international-standing-orders",
                                     "id": "InternationalStandingOrderPaymentId", "funds": False},
    "file": {"label": "File payments", "short": "File", "consent": "file-payment-consents",
             "payment": "file-payments", "id": "FilePaymentId", "funds": False, "file": True},
}
PIS_LEGACY = {"consent-create": "domestic:consent-create", "consent-get": "domestic:consent-get",
              "funds-check": "domestic:funds-check", "payment-create": "domestic:submit",
              "payment-get": "domestic:payment-get"}


def _pis_catalogue():
    out = []
    for t, p in PAYMENT_TYPES.items():
        g, on = p["label"], t == "domestic"
        out.append(E(f"{t}:consent-create", "POST", f"/{p['consent']}", g, "Create a consent (not authorised)", 1, on))
        out.append(E(f"{t}:consent-get", "GET", f"/{p['consent']}/{{ConsentId}}", g, "Read a consent", 3, on))
        if p["funds"]:
            out.append(E(f"{t}:funds-check", "GET", f"/{p['consent']}/{{ConsentId}}/funds-confirmation", g,
                         "Funds check, until the consent is used for its payment", 2, on))
        if p.get("file"):
            out.append(E(f"{t}:file-get", "GET", f"/{p['consent']}/{{ConsentId}}/file", g, "Read the uploaded file", 1, on))
        out.append(E(f"{t}:submit", "POST", f"/{p['payment']}", g, "Submit the payment: once per authorised consent",
                     0, on, once=True))
        out.append(E(f"{t}:payment-get", "GET", f"/{p['payment']}/{{{p['id']}}}", g, "Read a submitted payment", 3, on))
        out.append(E(f"{t}:payment-details", "GET", f"/{p['payment']}/{{{p['id']}}}/payment-details", g,
                     "Payment details", 1, on))
        if p.get("file"):
            out.append(E(f"{t}:report-file", "GET", f"/{p['payment']}/{{{p['id']}}}/report-file", g, "Report file", 1, on))
    return out


def migrate_pis_keys(csv):
    """Endpoint lists saved before payment types existed used plain keys for domestic payments."""
    keys = [PIS_LEGACY.get(k, k) for k in (csv or "").split(",") if k]
    return ",".join(dict.fromkeys(keys))

ENDPOINTS = {
    "AIS": [
        E("accounts", "GET", "/accounts", "Accounts", "Lists the accounts; always called once per user to find account IDs", 2),
        E("account", "GET", "/accounts/{AccountId}", "Accounts", "One account", 2),
        *[E(k, "GET", f"/accounts/{{AccountId}}/{k}", "Per account", "", w) for k, w in
          [("balances", 5), ("transactions", 5), ("beneficiaries", 1), ("direct-debits", 1), ("standing-orders", 1),
           ("scheduled-payments", 1), ("statements", 1), ("product", 1), ("offers", 1), ("party", 1), ("parties", 1)]],
        *[E("bulk-" + k, "GET", f"/{k}", "Bulk (all accounts in one call)", "Optional in the standard", 1, False) for k in
          ["balances", "transactions", "beneficiaries", "direct-debits", "standing-orders", "scheduled-payments",
           "statements", "products", "offers", "party"]],
        E("consent-create", "POST", "/account-access-consents", "Consents (client credentials)", "Create a consent", 1, False),
        E("consent-get", "GET", "/account-access-consents/{ConsentId}", "Consents (client credentials)",
          "Read back a consent created above", 1, False, "consent-create"),
        E("consent-delete", "DELETE", "/account-access-consents/{ConsentId}", "Consents (client credentials)",
          "Delete a consent created above", 1, False, "consent-create"),
    ],
    "PIS": _pis_catalogue(),
    "CoF": [
        E("consent-create", "POST", "/funds-confirmation-consents", "CoF consents (client credentials)", "Create a consent", 1),
        E("consent-get", "GET", "/funds-confirmation-consents/{ConsentId}", "CoF consents (client credentials)",
          "Read back a consent created above", 1, True, "consent-create"),
        E("consent-delete", "DELETE", "/funds-confirmation-consents/{ConsentId}", "CoF consents (client credentials)",
          "Delete a consent created above", 1, False, "consent-create"),
        E("confirm", "POST", "/funds-confirmations", "Confirmations (authorised consent)", "Is the amount available?", 5),
    ],
}
API_SEGMENT = {"AIS": "aisp", "PIS": "pisp", "CoF": "cbpii"}
ENDPOINTS_ORDER = {a: [e["key"] for e in ENDPOINTS[a]] for a in ENDPOINTS}
SCOPE = {"AIS": "accounts", "PIS": "payments", "CoF": "fundsconfirmations"}
KIND = {"ais": "AIS", "pis": "PIS", "cof": "CoF"}
DEFAULT_PERMISSIONS = ("ReadAccountsDetail, ReadBalances, ReadBeneficiariesDetail, ReadDirectDebits, ReadOffers, "
                       "ReadParty, ReadPartyPSU, ReadProducts, ReadScheduledPaymentsDetail, ReadStandingOrdersDetail, "
                       "ReadStatementsDetail, ReadTransactionsCredits, ReadTransactionsDebits, ReadTransactionsDetail")


def default_endpoints(area):
    return ",".join(e["key"] for e in ENDPOINTS[area] if e["on"])


# ------------------------------------------------------------------- schema ----
# Fields grouped by wizard step. kind: text | secret | choice | file | textarea | number | check | source | endpoints
F = lambda key, step, kind, label, default="", hint="", **extra: dict(
    key=key, step=step, kind=kind, label=label, default=default, hint=hint, **extra)
MANUAL, FLOW = "Manual tokens", "Consent flow"

STEPS = [
    {"id": "tenant", "title": "Tenant", "intro": "Create the tenant profile for the financial institution in scope of the "
                                                  "stress testing. Start with its OpenID discovery document: everything the "
                                                  "financial institution publishes there is picked up automatically, and "
                                                  "the following steps build on it. Set it up once; test profiles reuse it."},
    {"id": "addresses", "title": "URLs", "intro": "Pre-filled from the discovery document. "
                                                   "Change anything that doesn't match your environment."},
    {"id": "certificates", "title": "Certificates", "intro": "How the financial institution's server certificates are "
                                                              "checked, and your own certificates and keys. Uploaded "
                                                              "files are checked straight away and stored for this "
                                                              "tenant profile only."},
    {"id": "capabilities", "title": "Supported APIs", "intro": "The API areas this financial institution offers and the "
                                                                "version of the UK Open Banking standard it implements."},
    {"id": "AIS", "title": "AIS APIs", "intro": "The account information endpoints this financial institution supports.", "area": "AIS"},
    {"id": "PIS", "title": "PIS APIs", "intro": "The payment initiation endpoints this financial institution supports.", "area": "PIS"},
    {"id": "CoF", "title": "CoF APIs", "intro": "The confirmation of funds endpoints this financial institution supports.", "area": "CoF"},
    {"id": "review", "title": "Review and save", "intro": "Check the tenant, then save it. You can come back and edit it at any time."},
]

SCHEMA = [
    F("name", "tenant", "text", "Tenant name", "", "For your reference, e.g. financial institution and environment."),
    F("well_known", "tenant", "text", "OpenID discovery URL", os.getenv("OB_WELL_KNOWN", ""),
      "Ends in /.well-known/openid-configuration.", wide=True),

    F("rs_host", "addresses", "text", "Resource server", os.getenv("OB_RS_HOST", ""),
      "Base URL of the financial institution's APIs. Not in the discovery document: suggested from its address.",
      wide=True, origin="suggested"),
    F("issuer", "addresses", "text", "Issuer", "", "", origin="discovery"),
    F("auth_endpoint", "addresses", "text", "Authorisation endpoint", "", "", origin="discovery"),
    F("token_endpoint", "addresses", "text", "Token endpoint", "", "", origin="discovery"),
    F("client_id", "addresses", "text", "Client ID", os.getenv("OB_CLIENT_ID", "")),
    F("client_auth", "addresses", "choice", "Client authentication", "tls_client_auth",
      "How the token endpoint identifies your client. Picked from the discovery document; "
      "client_secret_basic sends Authorization: Basic base64(client ID:secret).",
      choices=["tls_client_auth", "client_secret_basic", "client_secret_post"]),
    F("client_secret", "addresses", "secret", "Client secret", os.getenv("OB_CLIENT_SECRET", ""),
      "", show={"client_auth": ["client_secret_basic", "client_secret_post"]}),
    F("redirect_uri", "addresses", "text", "Redirect URL", os.getenv("OB_REDIRECT_URI", ""),
      "Where the financial institution sends the user back after approving a consent. Keep this app's own address "
      "(…/ob-callback) to capture approvals automatically; it must be registered for your client.", wide=True),
    F("response_type", "addresses", "choice", "Response type", "code id_token", choices=["code id_token", "code"]),

    F("ca_check", "certificates", "choice", "TLS certificate verification", CA_CHOICES[0],
      "How this tool checks the financial institution's server certificates. Choose 'Accept self-signed "
      "certificates' when they can't be traced to a publicly trusted root CA.", choices=CA_CHOICES),
    F("ca_file", "certificates", "file", "CA file", "", "The financial institution's CA chain (PEM).",
      show={"ca_check": CA_CHOICES[2]}),
    F("mtls_cert", "certificates", "file", "Transport certificate", "", "PEM. Leave empty if the financial institution doesn't use mTLS."),
    F("mtls_key", "certificates", "file", "Transport key", ""),
    F("signing_key", "certificates", "file", "Signing key", "", "Signs request objects and payment requests."),
    F("signing_kid", "certificates", "text", "Signing key ID (kid)", os.getenv("OB_SIGNING_KID", "")),
    F("jws_iss", "certificates", "text", "Signature issuer", os.getenv("OB_JWS_ISS", ""),
      "<org-id>/<software-statement-id>; only for signed payment requests."),

    F("test_ais", "capabilities", "check", "AIS", "Yes", "Account information: accounts, balances, transactions…"),
    F("test_pis", "capabilities", "check", "PIS", "No", "Payment initiation: domestic payment consents and payments"),
    F("test_cof", "capabilities", "check", "CoF", "No", "Confirmation of funds for card-based payment instrument issuers"),
    F("version", "capabilities", "choice", "UK Open Banking standard version", os.getenv("OB_VERSION", "3.1"),
      "Sets the API paths (/open-banking/v3.1/… or /v4.0/…) and the v4.0 payload formats.", choices=["3.1", "4.0"]),


    F("ais_endpoints", "AIS", "endpoints", "Supported APIs", default_endpoints("AIS")),

    F("pis_endpoints", "PIS", "endpoints", "Supported APIs", default_endpoints("PIS")),

    F("cof_endpoints", "CoF", "endpoints", "Supported APIs", default_endpoints("CoF")),
]
FIELDS = {f["key"]: f for f in SCHEMA}
SECRET_KEYS = {k for k, f in FIELDS.items() if f["kind"] == "secret"} | {"aisp_more_tokens", "pisp_authorised"}
FILE_KEYS = [k for k, f in FIELDS.items() if f["kind"] == "file"]


def fpath(p):
    """Where a stored certificate/key path is on this machine. New uploads are stored relative to the data
    folder (certs/<tenant>/...), so the data can move between machines and into Docker; older absolute
    paths are looked up again under this data folder's certs/."""
    if not p:
        return p
    if not os.path.isabs(p):
        return os.path.join(WORKDIR, p)
    if "/certs/" in p:  # prefer this data folder's copy (data moved between machines or into Docker)
        alt = os.path.join(CERTS_DIR, *p.split("/certs/", 1)[1].split("/"))
        if os.path.exists(alt):
            return alt
    return p


def _write_private(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40] or "tenant"


# ------------------------------------------------------------------ tenants ----
def _load_all():
    if os.path.exists(TENANTS_FILE):
        with open(TENANTS_FILE) as fh:
            return json.load(fh)
    data = {"tenants": {}}
    if os.path.exists(LEGACY_SETTINGS):  # one-off migration from the single-settings version
        try:
            with open(LEGACY_SETTINGS) as fh:
                data["tenants"]["default"] = _from_legacy(json.load(fh))
        except Exception:  # noqa: BLE001
            pass
    return data


def _save_all(data):
    with _lock:
        _write_private(TENANTS_FILE, json.dumps(data, indent=2))


def _from_legacy(old):
    t = new_tenant_values("Default")
    t.update({k: v for k, v in old.items() if k in FIELDS})
    scope = old.get("scope", "AIS only")
    t["test_ais"] = "Yes" if scope != "PIS only" else "No"
    t["test_pis"] = "Yes" if scope != "AIS only" else "No"
    mode = old.get("token_mode", MANUAL)
    t["ais_source"] = t["pis_source"] = mode if mode in SOURCES else MANUAL
    if old.get("ca_check"):
        t["ca_check"] = ca_mode(old["ca_check"])
    t["complete"] = True
    return t


def new_tenant_values(name=""):
    return {**{k: f["default"] for k, f in FIELDS.items()}, "name": name, "complete": False, "discovery": {}}


def list_tenants():
    data, out = _load_all(), []
    for tid, t in data["tenants"].items():
        areas = [a for a in AREAS if t.get("test_" + a.lower()) == "Yes"]
        full = {**new_tenant_values(), **t, "_id": tid}
        issues = validate_tenant(full)
        out.append({"id": tid, "name": t.get("name") or tid, "rs_host": t.get("rs_host", ""), "areas": areas,
                    "complete": bool(t.get("complete")) and not issues, "issues": issues,
                    "profiles": len([p for p in _load_profiles()["profiles"].values() if p.get("tenant") == tid]),
                    "apis": {a: len([k for k in (t.get(a.lower() + "_endpoints") or "").split(",") if k]) for a in areas},
                    "version": t.get("version", "3.1"),
                    })
    return sorted(out, key=lambda x: x["name"].lower())


def get_tenant(tid):
    t = _load_all()["tenants"].get(tid)
    if t is None:
        raise KeyError(f"Unknown tenant: {tid}")
    t = {**new_tenant_values(), **t}
    t["ca_check"] = ca_mode(t.get("ca_check"))
    t["pis_endpoints"] = migrate_pis_keys(t.get("pis_endpoints"))
    return t


def save_tenant(tid, values):
    """Create (tid=None) or update a tenant. Returns (tid, values)."""
    data = _load_all()
    clean = {k: ("" if v is None else str(v)) for k, v in values.items() if k in FIELDS}
    if not tid:
        base = slug(clean.get("name"))
        tid, n = base, 2
        while tid in data["tenants"]:
            tid, n = f"{base}-{n}", n + 1
        current = new_tenant_values()
    else:
        current = {**new_tenant_values(), **data["tenants"].get(tid, {})}
    current.update(clean)
    if "complete" in values:
        current["complete"] = bool(values["complete"])
    if isinstance(values.get("discovery"), dict):
        current["discovery"] = values["discovery"]
    data["tenants"][tid] = current
    _save_all(data)
    return tid, current


def delete_tenant(tid):
    for p in [pid for pid, p in _load_profiles()["profiles"].items() if p.get("tenant") == tid]:
        delete_profile(p)
    data = _load_all()
    data["tenants"].pop(tid, None)
    _save_all(data)


def resolve_tenant(name_or_id=None):
    """Pick a tenant by id or name; with nothing given, the only (or first complete) tenant."""
    tenants = list_tenants()
    if name_or_id:
        for t in tenants:
            if name_or_id in (t["id"], t["name"]):
                return t["id"]
        raise KeyError(f"No tenant called '{name_or_id}'. Saved tenants: {', '.join(t['name'] for t in tenants) or 'none'}")
    complete = [t for t in tenants if t["complete"]] or tenants
    if not complete:
        raise KeyError("No tenant profiles yet: open Tenant profiles and create one.")
    return complete[0]["id"]


def validate_tenant(t):
    """Problems with the tenant itself (what the financial institution is), as human-readable messages."""
    issues = []
    if not t.get("rs_host"):
        issues.append("Resource server is empty (URLs).")
    areas = [a for a in AREAS if t.get("test_" + a.lower()) == "Yes"]
    if not areas:
        issues.append("No area selected (Supported APIs).")
    for a in areas:
        if not [k for k in (t.get(a.lower() + "_endpoints") or "").split(",") if k]:
            issues.append(f"{a}: no supported APIs ticked.")
    if t.get("client_auth") in ("client_secret_basic", "client_secret_post") and not t.get("client_secret"):
        issues.append("Client secret is empty (URLs).")
    if ca_mode(t.get("ca_check")) == CA_CHOICES[2] and not t.get("ca_file"):
        issues.append("CA file not uploaded (Certificates).")
    return issues


# --------------------------------------------------------- import / export ----
def export_tenant(tid, include_secrets):
    t = get_tenant(tid)
    out = {"_about": "Open Banking stress test tenant. Import it on the Tenants page.", "name": t.get("name")}
    for step in STEPS:
        sec = {}
        for f in SCHEMA:
            if f["step"] != step["id"] or f["key"] == "name":
                continue
            v = t.get(f["key"], "")
            if f["key"] in SECRET_KEYS and not include_secrets:
                v = ""
            if f["kind"] == "file" and v:
                v = os.path.basename(v)
            sec[f["key"]] = v
        if sec:
            out[step["title"]] = sec
    if include_secrets:
        files = {}
        for k in FILE_KEYS:
            p = fpath(t.get(k))
            if p and os.path.exists(p):
                with open(p) as fh:
                    files[k] = {"name": os.path.basename(p), "pem": fh.read()}
        if files:
            out["files"] = files
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=120)


def import_tenant(text, tid=None):
    """Create a new tenant (or update tid) from an exported YAML/JSON. Returns (tid, notes)."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("The config must be a YAML or JSON object.")
    flat = {}
    for k, v in data.items():
        if isinstance(v, dict) and k != "files":
            flat.update({kk: vv for kk, vv in v.items() if kk in FIELDS and kk not in FILE_KEYS})
        elif k in FIELDS and k not in FILE_KEYS:
            flat[k] = v
    flat = {k: v for k, v in flat.items() if v is not None}
    tid, _ = save_tenant(tid, flat)
    notes = []
    for k, f in (data.get("files") or {}).items():
        if k in FILE_KEYS and isinstance(f, dict) and f.get("pem"):
            info = save_upload(tid, k, f["pem"].encode())
            notes.append(f"{FIELDS[k]['label']}: {info['summary']}")
    return tid, notes


# ------------------------------------------------------------- certificates ----
def describe_file(key, path):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    if not path:
        return {"ok": None, "summary": "Not uploaded"}
    path = fpath(path)
    if not os.path.exists(path):
        return {"ok": False, "summary": f"File not found: {path}. Upload it again."}
    try:
        data = open(path, "rb").read()
    except OSError as e:
        return {"ok": False, "summary": f"Can't read the file ({e.strerror}). Upload it again."}
    try:
        if key in ("mtls_key", "signing_key"):
            if b"ENCRYPTED" in data:
                return {"ok": False, "summary": "Key is password-protected; remove the password first."}
            k = serialization.load_pem_private_key(data, None)
            return {"ok": True, "summary": f"Private key ({k.key_size}-bit)"}
        certs = x509.load_pem_x509_certificates(data)
        c = certs[0]
        exp = c.not_valid_after_utc
        days = (exp - datetime.now(timezone.utc)).days
        summary = f"{len(certs)} certificate(s) · {c.subject.rfc4514_string()[:70]} · expires {exp:%d %b %Y}"
        if days < 0:
            return {"ok": False, "summary": summary + " (EXPIRED)"}
        return {"ok": True, "summary": summary + (f" ({days} days left)" if days < 30 else "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"Not a readable PEM file: {str(e)[:120]}"}


def cert_key_match(cert_path, key_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    try:
        c = x509.load_pem_x509_certificates(open(fpath(cert_path), "rb").read())[0]
        k = serialization.load_pem_private_key(open(fpath(key_path), "rb").read(), None)
        fmt = (serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        return c.public_key().public_bytes(*fmt) == k.public_key().public_bytes(*fmt)
    except Exception:  # noqa: BLE001
        return None


def save_upload(tid, key, content: bytes):
    if key not in FILE_KEYS:
        raise ValueError("Unknown file slot")
    if b"-----BEGIN" not in content:
        raise ValueError("Please upload a PEM file (text starting with -----BEGIN). "
                         "Convert .p12/.pfx/.cer files with openssl first.")
    ext = ".key" if key in ("mtls_key", "signing_key") else ".pem"
    rel = os.path.join("certs", tid, f"{key}{ext}")
    with _lock:
        _write_private(os.path.join(WORKDIR, rel), content.decode())
    save_tenant(tid, {key: rel})
    info = describe_file(key, rel)
    info["path"] = rel
    return info


def file_status(t):
    out = {k: {**describe_file(k, t.get(k)), "path": t.get(k, "")} for k in FILE_KEYS}
    if t.get("mtls_cert") and t.get("mtls_key"):
        m = cert_key_match(t["mtls_cert"], t["mtls_key"])
        if m is False:
            out["mtls_key"].update(ok=False, summary=out["mtls_key"]["summary"] + " · does NOT match the certificate")
        elif m:
            out["mtls_key"]["summary"] += " · matches the certificate"
    return out


# ---------------------------------------------------------------- discovery ----
def ca_mode(value):
    return CA_LEGACY.get(value, value) if value else CA_CHOICES[0]


def _verify(t):
    mode = ca_mode(t.get("ca_check"))
    if mode == CA_CHOICES[1]:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    if mode == CA_CHOICES[2] and t.get("ca_file"):
        return fpath(t["ca_file"])
    return True


def suggest_api_host(discovery_url):
    """API hosts aren't published in discovery; suggest rs1.<rest of the discovery host>."""
    import ipaddress
    host = urllib.parse.urlsplit(discovery_url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return ""  # an IP address has no prefix to swap
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) > 2:
        return "https://" + ".".join(["rs1"] + parts[1:])
    return ""


def fetch_discovery(url, ca_check="", ca_file=""):
    if not url:
        raise ValueError("Enter the discovery URL first.")
    try:
        r = requests.get(url, verify=_verify({"ca_check": ca_check, "ca_file": ca_file}), timeout=20,
                         headers={"Accept": "application/json"})
    except requests.exceptions.SSLError as e:
        raise ValueError(f"The server's certificate couldn't be verified ({str(e)[:120]}). If the financial institution "
                         "uses self-signed certificates, set 'TLS certificate verification' to 'Accept self-signed "
                         "certificates' (Certificates step), or upload its CA file there.")
    except requests.RequestException as e:
        raise ValueError(f"Could not reach the discovery URL: {str(e)[:160]}")
    if r.status_code != 200:
        raise ValueError(f"The discovery URL returned {r.status_code}.")
    try:
        d = r.json()
    except ValueError:
        raise ValueError("The discovery URL didn't return JSON. Check it ends in /.well-known/openid-configuration.")
    aliases = d.get("mtls_endpoint_aliases") or {}
    found = {
        "issuer": d.get("issuer", ""),
        "authorization_endpoint": d.get("authorization_endpoint", ""),
        "token_endpoint": aliases.get("token_endpoint") or d.get("token_endpoint", ""),
        "token_endpoint_mtls_alias": bool(aliases.get("token_endpoint")),
        "pushed_authorization_request_endpoint": aliases.get("pushed_authorization_request_endpoint")
        or d.get("pushed_authorization_request_endpoint", ""),
        "jwks_uri": d.get("jwks_uri", ""),
        "token_endpoint_auth_methods_supported": d.get("token_endpoint_auth_methods_supported", []),
        "response_types_supported": d.get("response_types_supported", []),
        "request_object_signing_alg_values_supported": d.get("request_object_signing_alg_values_supported", []),
        "scopes_supported": d.get("scopes_supported", []),
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    fill = {"issuer": found["issuer"], "auth_endpoint": found["authorization_endpoint"],
            "token_endpoint": found["token_endpoint"], "rs_host": suggest_api_host(url)}
    methods = found["token_endpoint_auth_methods_supported"]
    for m in ("tls_client_auth", "client_secret_basic", "client_secret_post"):
        if m in methods:
            fill["client_auth"] = m
            break
    rt = found["response_types_supported"]
    if rt:
        fill["response_type"] = "code id_token" if "code id_token" in rt else ("code" if "code" in rt else "")
    return found, {k: v for k, v in fill.items() if v}


# -------------------------------------------------------------- token store ----
def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as fh:
            data = json.load(fh)
        if "ais" in data or "pending" in data:  # migrate the single-settings layout
            data = {"default": {"ais": data.get("ais", []), "pis": data.get("pis", []), "cof": [],
                                "pending": data.get("pending", {})}}
        return data
    return {}


def save_tokens(t):
    with _lock:
        _write_private(TOKENS_FILE, json.dumps(t, indent=2))


def update_tokens(fn):
    with _lock:
        t = load_tokens()
        fn(t)
        save_tokens(t)
        return t


def tenant_tokens(tid):
    t = load_tokens().get(tid, {})
    return {"ais": t.get("ais", []), "pis": t.get("pis", []), "cof": t.get("cof", []), "pending": t.get("pending", {})}


def _tok(t, tid):
    b = t.setdefault(tid, {})
    for k in ("ais", "pis", "cof"):
        b.setdefault(k, [])
    b.setdefault("pending", {})
    return b


# ------------------------------------------------------------- consent flow ----
class FlowError(Exception):
    pass


class AuthClient:
    """Authorisation server calls for one tenant, authenticating with the transport certificate."""

    def __init__(self, t):
        self.t = t
        self._cc = {}

    @property
    def cert(self):
        c, k = self.t.get("mtls_cert"), self.t.get("mtls_key")
        return (fpath(c), fpath(k)) if c and k else None

    @property
    def verify(self):
        return _verify(self.t)

    def endpoints(self):
        d = {"issuer": self.t.get("issuer"), "authorization_endpoint": self.t.get("auth_endpoint"),
             "token_endpoint": self.t.get("token_endpoint")}
        missing = [k for k, v in d.items() if not v]
        if missing:
            raise FlowError("Missing " + ", ".join(m.replace("_", " ") for m in missing) +
                            " (URLs). Fetch the discovery document or fill them in.")
        return d

    def _token_request(self, data):
        cid, method = self.t.get("client_id"), self.t.get("client_auth") or "tls_client_auth"
        if not cid:
            raise FlowError("Client ID is empty (URLs).")
        auth, data = None, {**data, "client_id": cid}
        if method == "tls_client_auth":
            if not self.cert:
                raise FlowError("tls_client_auth needs the transport certificate and key (Certificates).")
        else:
            if not self.t.get("client_secret"):
                raise FlowError(f"{method} needs the client secret (URLs).")
            if method == "client_secret_basic":
                # Authorization: Basic base64(client_id ":" client_secret), sent exactly as issued
                auth = (cid, self.t["client_secret"])
                data.pop("client_id")
            else:
                data["client_secret"] = self.t["client_secret"]
        try:
            r = requests.post(self.endpoints()["token_endpoint"], data=data, auth=auth, cert=self.cert,
                              verify=self.verify, timeout=30, headers={"Accept": "application/json"})
        except requests.RequestException as e:
            raise FlowError(f"Token endpoint not reachable: {e}")
        if r.status_code != 200:
            raise FlowError(f"Token endpoint returned {r.status_code}: {r.text[:300]}")
        body = r.json()
        body["expires_at"] = time.time() + int(body.get("expires_in", 300))
        return body

    def client_credentials(self, scope):
        with _lock:
            t = self._cc.get(scope)
            if t and t["expires_at"] - 30 > time.time():
                return t["access_token"]
            t = self._token_request({"grant_type": "client_credentials", "scope": scope})
            self._cc[scope] = t
            return t["access_token"]

    def exchange_code(self, code):
        return self._token_request({"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": self.t.get("redirect_uri", "")})

    def refresh(self, refresh_token):
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def authorise_url(self, consent_id, area):
        import jwt  # PyJWT
        key_path = fpath(self.t.get("signing_key"))
        if not key_path or not os.path.exists(key_path):
            raise FlowError("The signing key is needed to sign the request object (Certificates).")
        d = self.endpoints()
        state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        scope = "openid " + SCOPE[area]
        intent = {"openbanking_intent_id": {"value": consent_id, "essential": True}}
        acr = {"essential": True, "values": ["urn:openbanking:psd2:sca", "urn:openbanking:psd2:ca"]}
        now = int(time.time())
        claims = {"iss": self.t["client_id"], "aud": d["issuer"], "client_id": self.t["client_id"],
                  "response_type": self.t.get("response_type") or "code id_token",
                  "redirect_uri": self.t.get("redirect_uri", ""), "scope": scope, "state": state, "nonce": nonce,
                  "max_age": 86400, "iat": now, "nbf": now, "exp": now + 3600, "jti": str(uuid.uuid4()),
                  "claims": {"userinfo": intent, "id_token": {**intent, "acr": acr}}}
        with open(key_path, "rb") as fh:
            request = jwt.encode(claims, fh.read(), algorithm="PS256",
                                 headers={"kid": self.t.get("signing_kid", ""), "typ": "JWT"})
        q = {"client_id": self.t["client_id"], "response_type": claims["response_type"], "scope": scope,
             "redirect_uri": claims["redirect_uri"], "state": state, "nonce": nonce, "request": request}
        ep = d["authorization_endpoint"]
        return ep + ("&" if "?" in ep else "?") + urllib.parse.urlencode(q, quote_via=urllib.parse.quote), state, nonce


def api_path(t, area):
    return f"/open-banking/v{t.get('version', '3.1')}/{API_SEGMENT[area]}"


def api_headers(t, token, extra=None):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json", "x-fapi-interaction-id": str(uuid.uuid4())}
    h.update(extra or {})
    return h


def account_consent_body(t):
    perms = [p.strip() for p in (t.get("aisp_permissions") or DEFAULT_PERMISSIONS).replace("\n", ",").split(",") if p.strip()]
    days = int(float(t.get("consent_days") or 30))
    expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    return {"Data": {"Permissions": list(dict.fromkeys(perms)), "ExpirationDateTime": expiry}, "Risk": {}}


def payment_consent_body(t):
    v4, ref = t.get("version") == "4.0", "LOADTEST"
    remittance = ({"Structured": [{"CreditorReferenceInformation": {"Reference": ref}}],
                   "Unstructured": ["Locust stress test"]} if v4 else {"Reference": ref, "Unstructured": "Locust stress test"})
    sid = lambda p: f"{p}{uuid.uuid4().hex[:30]}"[:35]
    return {"Data": {"Initiation": {
        "InstructionIdentification": sid("LT"), "EndToEndIdentification": sid("E2E"),
        "InstructedAmount": {"Amount": t.get("amount") or "1.00", "Currency": t.get("currency") or "GBP"},
        "CreditorAccount": {"SchemeName": t.get("creditor_scheme"), "Identification": t.get("creditor_id"),
                            "Name": t.get("creditor_name")},
        "RemittanceInformation": remittance}}, "Risk": json.loads(t.get("risk") or "{}")}


def cof_consent_body(t):
    expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
    return {"Data": {"ExpirationDateTime": expiry, "DebtorAccount": {
        "SchemeName": t.get("debtor_scheme"), "Identification": t.get("debtor_id"), "Name": t.get("debtor_name")}}}


def funds_confirmation_body(t, consent_id):
    return {"Data": {"ConsentId": consent_id, "Reference": f"LT{uuid.uuid4().hex[:12]}",
                     "InstructedAmount": {"Amount": t.get("cof_amount") or "10.00", "Currency": t.get("cof_currency") or "GBP"}}}


def jws_signature(t, body: bytes):
    """Detached JWS per OB v3.1.4+ / v4.0 (no b64 claim): header..signature"""
    if t.get("sign_payments") != "Yes" or not t.get("signing_key"):
        return None
    import jwt
    headers = {"kid": t.get("signing_kid", ""), "typ": "JOSE", "cty": "application/json",
               "http://openbanking.org.uk/iat": int(time.time()), "http://openbanking.org.uk/iss": t.get("jws_iss", ""),
               "http://openbanking.org.uk/tan": "openbanking.org.uk",
               "crit": ["http://openbanking.org.uk/iat", "http://openbanking.org.uk/iss", "http://openbanking.org.uk/tan"]}
    with open(fpath(t["signing_key"]), "rb") as fh:
        head, _, sig = jwt.api_jws.encode(body, fh.read(), algorithm="PS256", headers=headers).split(".")
    return f"{head}..{sig}"


# Data templates per payment type and version: the "Data" object of the consent request, with placeholders.
# Editable per test profile (test data), because financial institutions differ in the details they accept.
_REM3 = '{"Reference": "{{Reference}}", "Unstructured": "Locust stress test"}'
_REM4 = '{"Structured": [{"CreditorReferenceInformation": {"Reference": "{{Reference}}"}}], "Unstructured": ["Locust stress test"]}'
_CRED = '{"SchemeName": "{{CreditorScheme}}", "Identification": "{{CreditorId}}", "Name": "{{CreditorName}}"}'
_ICRED = '{"SchemeName": "{{IntlCreditorScheme}}", "Identification": "{{IntlCreditorId}}", "Name": "{{IntlCreditorName}}"}'
_IDS = '"InstructionIdentification": "{{InstructionIdentification}}", "EndToEndIdentification": "{{EndToEndIdentification}}"'


def _tpl(v4, kind):
    rem = _REM4 if v4 else _REM3
    amt = '{"Amount": "{{Amount}}", "Currency": "{{Currency}}"}'
    iamt = '{"Amount": "{{Amount}}", "Currency": "{{IntlCurrency}}"}'
    freq = ('"MandateRelatedInformation": {"Frequency": {"Type": "{{Frequency}}", "CountPerPeriod": 1, "PointInTime": "01"}}'
            if v4 else '"Frequency": "{{Frequency}}", "Reference": "{{Reference}}", "NumberOfPayments": "1"')
    rem_so = f', "RemittanceInformation": {rem}' if v4 else ""
    t = {
        "domestic": f'{{"Initiation": {{{_IDS}, "InstructedAmount": {amt}, "CreditorAccount": {_CRED}, '
                    f'"RemittanceInformation": {rem}}}}}',
        "domestic-scheduled": f'{{"Permission": "Create", "Initiation": {{{_IDS}, "RequestedExecutionDateTime": '
                              f'"{{{{ExecutionDateTime}}}}", "InstructedAmount": {amt}, "CreditorAccount": {_CRED}, '
                              f'"RemittanceInformation": {rem}}}}}',
        "domestic-standing-order": f'{{"Permission": "Create", "Initiation": {{{freq}, "FirstPaymentDateTime": '
                                   f'"{{{{FirstPaymentDateTime}}}}", "FirstPaymentAmount": {amt}, '
                                   f'"CreditorAccount": {_CRED}{rem_so}}}}}',
        "international": f'{{"Initiation": {{{_IDS}, "CurrencyOfTransfer": "{{{{IntlCurrency}}}}", '
                         f'"InstructedAmount": {iamt}, "CreditorAccount": {_ICRED}, "RemittanceInformation": {rem}}}}}',
        "international-scheduled": f'{{"Permission": "Create", "Initiation": {{{_IDS}, "RequestedExecutionDateTime": '
                                   f'"{{{{ExecutionDateTime}}}}", "CurrencyOfTransfer": "{{{{IntlCurrency}}}}", '
                                   f'"InstructedAmount": {iamt}, "CreditorAccount": {_ICRED}, '
                                   f'"RemittanceInformation": {rem}}}}}',
        "international-standing-order": f'{{"Permission": "Create", "Initiation": {{{freq}, "FirstPaymentDateTime": '
                                        f'"{{{{FirstPaymentDateTime}}}}", "CurrencyOfTransfer": "{{{{IntlCurrency}}}}", '
                                        f'"InstructedAmount": {iamt}, "CreditorAccount": {_ICRED}{rem_so}}}}}',
        "file": '{"Initiation": {"FileType": "{{FileType}}", "FileHash": "{{FileHash}}", '
                '"NumberOfTransactions": "{{NumberOfTransactions}}", "ControlSum": "{{ControlSum}}"}}',
    }
    return json.dumps(json.loads(t[kind]), indent=2)


def standard_templates():
    return {k: {"3.1": _tpl(False, k), "4.0": _tpl(True, k)} for k in PAYMENT_TYPES}


def _fill(template, values):
    out = template
    for k, v in values.items():
        token = "{{" + k + "}}"
        if k in ("ControlSum",):        # a number in the request: the quoted placeholder becomes a bare number
            out = out.replace(f'"{token}"', str(v))
        out = out.replace(token, json.dumps(str(v))[1:-1])
    return out


def payment_file(t):
    """A payment file with a few domestic payments, its content type and its totals."""
    n = max(1, min(int(float(t.get("file_transactions") or 3)), 500))
    amount = float(t.get("amount") or "1.00")
    sid = lambda p: f"{p}{uuid.uuid4().hex[:30]}"[:35]
    ftype = t.get("file_type") or "UK.OBIE.PaymentInitiation.3.1"
    if "pain.001" in ftype:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        txs = "".join(
            f"<CdtTrfTxInf><PmtId><InstrId>{sid('LT')}</InstrId><EndToEndId>{sid('E2E')}</EndToEndId></PmtId>"
            f"<Amt><InstdAmt Ccy=\"{t.get('currency') or 'GBP'}\">{amount:.2f}</InstdAmt></Amt>"
            f"<Cdtr><Nm>{t.get('creditor_name')}</Nm></Cdtr><CdtrAcct><Id><Othr><Id>{t.get('creditor_id')}</Id></Othr>"
            f"</Id></CdtrAcct></CdtTrfTxInf>" for _ in range(n))
        body = (f'<?xml version="1.0" encoding="UTF-8"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.08">'
                f"<CstmrCdtTrfInitn><GrpHdr><MsgId>{sid('MSG')}</MsgId><CreDtTm>{now}</CreDtTm><NbOfTxs>{n}</NbOfTxs>"
                f"<CtrlSum>{amount * n:.2f}</CtrlSum><InitgPty><Nm>Load test</Nm></InitgPty></GrpHdr>"
                f"<PmtInf><PmtInfId>{sid('PMT')}</PmtInfId><PmtMtd>TRF</PmtMtd>{txs}</PmtInf></CstmrCdtTrfInitn></Document>")
        content, ctype = body.encode(), "text/xml"
    else:
        rem = (json.loads(_REM4.replace("{{Reference}}", "LOADTEST")) if t.get("version") == "4.0"
               else json.loads(_REM3.replace("{{Reference}}", "LOADTEST")))
        payments = [{"InstructionIdentification": sid("LT"), "EndToEndIdentification": sid("E2E"),
                     "InstructedAmount": {"Amount": f"{amount:.2f}", "Currency": t.get("currency") or "GBP"},
                     "CreditorAccount": {"SchemeName": t.get("creditor_scheme"), "Identification": t.get("creditor_id"),
                                         "Name": t.get("creditor_name")},
                     "RemittanceInformation": rem} for _ in range(n)]
        content, ctype = json.dumps({"Data": {"DomesticPayments": payments}}).encode(), "application/json"
    import base64
    import hashlib
    return {"content": content, "type": ctype, "file_type": ftype, "count": n, "control_sum": round(amount * n, 2),
            "hash": base64.b64encode(hashlib.sha256(content).digest()).decode()}


def payment_consent_body_for(t, ptype):
    """Consent request for a payment type, from the profile's template (or the standard one)."""
    if ptype == "domestic" and not (t.get("tpl_domestic") or "").strip():
        return payment_consent_body(t), None
    v4 = t.get("version") == "4.0"
    template = (t.get("tpl_" + ptype.replace("-", "_")) or "").strip() or _tpl(v4, ptype)
    sid = lambda p: f"{p}{uuid.uuid4().hex[:30]}"[:35]
    when = (datetime.now(timezone.utc) + timedelta(days=int(float(t.get("exec_days") or 2)))).replace(
        hour=9, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    values = {"InstructionIdentification": sid("LT"), "EndToEndIdentification": sid("E2E"),
              "Amount": t.get("amount") or "1.00", "Currency": t.get("currency") or "GBP",
              "CreditorScheme": t.get("creditor_scheme"), "CreditorId": t.get("creditor_id"),
              "CreditorName": t.get("creditor_name"), "Reference": "LOADTEST",
              "ExecutionDateTime": when, "FirstPaymentDateTime": when,
              "Frequency": (t.get("so_frequency") or "").strip() or ("MNTH" if v4 else "EvryWorkgDay"),
              "IntlCurrency": t.get("intl_currency") or "EUR", "IntlCreditorScheme": t.get("intl_creditor_scheme"),
              "IntlCreditorId": t.get("intl_creditor_id"), "IntlCreditorName": t.get("intl_creditor_name")}
    f = None
    if PAYMENT_TYPES[ptype].get("file"):
        f = payment_file(t)
        values.update(FileType=f["file_type"], FileHash=f["hash"], NumberOfTransactions=str(f["count"]),
                      ControlSum=f["control_sum"])
    try:
        data = json.loads(_fill(template, values))
    except ValueError as e:
        raise FlowError(f"The {PAYMENT_TYPES[ptype]['short']} template isn't valid JSON after filling in: {e}")
    return {"Data": data, "Risk": json.loads(t.get("risk") or "{}")}, f


CONSENT_RESOURCE = {"AIS": ("account-access-consents", account_consent_body, False),
                    "PIS": ("domestic-payment-consents", payment_consent_body, True),
                    "CoF": ("funds-confirmation-consents", cof_consent_body, False)}


def test_connection(tid):
    t = get_tenant(tid)
    out = [(st["ok"], f"{FIELDS[k]['label']}: {st['summary']}") for k, st in file_status(t).items() if st["ok"] is not None]
    if t.get("well_known"):
        try:
            found, _ = fetch_discovery(t["well_known"], t.get("ca_check", ""), t.get("ca_file", ""))
            out.append((True, f"Discovery document read · issuer {found['issuer']}"))
        except ValueError as e:
            out.append((False, str(e)))
    flow_areas = [a for a in AREAS if t.get("test_" + a.lower()) == "Yes"]
    if flow_areas and t.get("client_id") and t.get("token_endpoint"):
        auth = AuthClient(t)
        for a in flow_areas:
            try:
                auth.client_credentials(SCOPE[a])
                out.append((True, f"Client-credentials token for '{SCOPE[a]}' obtained"))
            except FlowError as e:
                out.append((False, str(e)))
                break
    for issue in validate_tenant({**t, "_id": tid}):
        out.append((False, issue))
    return out


# ============================================================== test profiles ====
# A test profile belongs to one tenant and holds what changes from test to test: load, which of the
# tenant's APIs to call (and how often), tokens, and test data. Stored in ob_profiles.json; tokens per
# profile in ob_tokens.json.
PROFILES_FILE = os.getenv("OB_PROFILES_FILE", os.path.join(WORKDIR, "ob_profiles.json"))

PROFILE_STEPS = [
    {"id": "profile", "title": "Profile", "intro": "Name the test profile and choose the tenant profile it runs against."},
    {"id": "scope", "title": "Scope", "intro": "Choose the areas this test covers and, for each, which of the "
                                                "tenant profile's supported APIs to call and how often."},
    {"id": "load", "title": "Load", "intro": "How many users, how fast they start, and how many requests each one sends."},
    {"id": "AIS", "title": "AIS tokens", "intro": "How this test gets its account information tokens.", "area": "AIS"},
    {"id": "PIS", "title": "PIS tokens", "intro": "How this test gets its payment initiation tokens.", "area": "PIS"},
    {"id": "CoF", "title": "CoF tokens", "intro": "How this test gets its confirmation of funds tokens.", "area": "CoF"},
    {"id": "review", "title": "Review and save", "intro": "Check the profile, then save it. Choose it on Locust's start page to run it."},
]

PROFILE_SCHEMA = [
    F("name", "profile", "text", "Profile name", "", "e.g. AIS soak test, or Peak load 200 users."),
    F("tenant", "profile", "tenant", "Tenant profile", "", "The financial institution this test profile runs against."),

    F("run_ais", "scope", "check", "AIS", "Yes", "Account information"),
    F("run_pis", "scope", "check", "PIS", "No", "Payment initiation"),
    F("run_cof", "scope", "check", "CoF", "No", "Confirmation of funds"),

    F("users", "load", "number", "Number of users", "20", "Concurrent simulated users."),
    F("spawn_rate", "load", "number", "Ramp up (users started per second)", "2", "e.g. 2 = all 20 users running after 10 s."),
    F("user_tps", "load", "number", "Requests per second per user", os.getenv("OB_USER_TPS", "2"),
      "0 = random 1–3 s pauses. All three can still be changed on Locust's start page."),
    F("run_time", "load", "text", "Run time", "10m",
      "How long the test runs, e.g. 30s, 10m, 1h30m. Leave empty to run until you press Stop."),
    F("peak_rps", "load", "number", "Peak-hour requests per second in the existing digital channel", "",
      "Optional. How many requests per second the existing digital channel (PSU interface) handles in its busiest "
      "hour. With the stress factor it sets the throughput target the report checks."),
    F("stress_factor", "load", "number", "Stress factor", "2",
      "Throughput target = peak-hour requests per second × stress factor. 2 adds the stress element."),
    F("mix_ais", "load", "number", "AIS share of users (%)", "60", show_multi=True),
    F("mix_pis", "load", "number", "PIS share of users (%)", "30", show_multi=True),
    F("mix_cof", "load", "number", "CoF share of users (%)", "10", show_multi=True),

    F("ais_tests", "AIS", "tests", "APIs to call", "", area_tests="AIS"),
    F("ais_source", "AIS", "source", "Token source", MANUAL, choices=SOURCES),
    F("aisp_token", "AIS", "secret", "Access token", os.getenv("OB_AISP_TOKEN", ""), show={"ais_source": MANUAL}),
    F("aisp_more_tokens", "AIS", "textarea", "More access tokens", "", "Optional, one per line. Users take tokens in turn.",
      show={"ais_source": MANUAL}),
    F("aisp_cc_token", "AIS", "secret", "Client-credentials token", os.getenv("OB_AISP_CC_TOKEN", ""),
      "Only needed for the consent endpoints.", show={"ais_source": MANUAL}),
    F("aisp_permissions", "AIS", "textarea", "Consent permissions", DEFAULT_PERMISSIONS, "Used when creating consents.", wide=True, testdata=True, data_for=['consent-create']),
    F("consent_days", "AIS", "number", "Consent valid for (days)", "30", testdata=True, data_for=['consent-create']),

    F("pis_tests", "PIS", "tests", "APIs to call", "", area_tests="PIS"),
    F("pis_source", "PIS", "source", "Token source", MANUAL, choices=SOURCES),
    F("pisp_cc_token", "PIS", "secret", "Client-credentials token", os.getenv("OB_PISP_CC_TOKEN", ""),
      "Creates and reads payment consents.", show={"pis_source": MANUAL}),
    F("pisp_authorised", "PIS", "textarea", "Authorised payment consents", "",
      "Lines of ConsentId,AccessToken,type. Type is optional (domestic if left out): domestic, domestic-scheduled, "
      "domestic-standing-order, international, international-scheduled, international-standing-order or file. "
      "Each consent is submitted once as a payment.", show={"pis_source": MANUAL}),
    F("amount", "PIS", "text", "Amount", os.getenv("OB_PAYMENT_AMOUNT", "1.00"), testdata=True, data_for=["*:consent-create", "*:submit"]),
    F("currency", "PIS", "text", "Currency", os.getenv("OB_PAYMENT_CURRENCY", "GBP"), testdata=True, data_for=["*:consent-create", "*:submit"]),
    F("creditor_name", "PIS", "text", "Payee name", os.getenv("OB_CREDITOR_NAME", "Load Test Payee"), testdata=True, data_for=["*:consent-create", "*:submit"]),
    F("creditor_id", "PIS", "text", "Payee account", os.getenv("OB_CREDITOR_ID", "08080021325698"), testdata=True, data_for=["*:consent-create", "*:submit"]),
    F("creditor_scheme", "PIS", "text", "Payee account scheme", os.getenv("OB_CREDITOR_SCHEME", "UK.OBIE.SortCodeAccountNumber"), testdata=True, data_for=["*:consent-create", "*:submit"]),
    F("risk", "PIS", "textarea", "Risk block (JSON)", os.getenv("OB_PAYMENT_RISK", "{}"), 'e.g. {"PaymentContextCode":"PartyToParty"}', testdata=True, data_for=["*:consent-create", "*:submit"]),
    F("exec_days", "PIS", "number", "Execution date: days from today", "2",
      "For scheduled payments and the first payment of standing orders.", testdata=True,
      data_for=["domestic-scheduled:*", "international-scheduled:*", "domestic-standing-order:*",
                "international-standing-order:*"]),
    F("so_frequency", "PIS", "text", "Standing order frequency", "",
      "Empty = standard (v3.1: EvryWorkgDay, v4.0: MNTH). Use a value your financial institution supports.",
      testdata=True, data_for=["domestic-standing-order:*", "international-standing-order:*"]),
    F("intl_currency", "PIS", "text", "International: currency of transfer", "EUR", testdata=True,
      data_for=["international:*", "international-scheduled:*", "international-standing-order:*"]),
    F("intl_creditor_name", "PIS", "text", "International: payee name", "Load Test Payee EU", testdata=True,
      data_for=["international:*", "international-scheduled:*", "international-standing-order:*"]),
    F("intl_creditor_id", "PIS", "text", "International: payee account", "DE89370400440532013000", testdata=True,
      data_for=["international:*", "international-scheduled:*", "international-standing-order:*"]),
    F("intl_creditor_scheme", "PIS", "text", "International: payee account scheme", "UK.OBIE.IBAN", testdata=True,
      data_for=["international:*", "international-scheduled:*", "international-standing-order:*"]),
    F("file_type", "PIS", "choice", "File payments: file type", "UK.OBIE.PaymentInitiation.3.1",
      "The format your financial institution accepts.", choices=["UK.OBIE.PaymentInitiation.3.1",
                                                                 "UK.OBIE.pain.001.001.08"],
      testdata=True, data_for=["file:*"]),
    F("file_transactions", "PIS", "number", "File payments: payments per file", "3", testdata=True,
      data_for=["file:*"]),
    F("tpl_domestic", "PIS", "template", "Domestic: request template (JSON)", "",
      "Empty = the standard template for your UK Open Banking version. Edit only if your financial institution "
      "needs something different.", testdata=True, data_for=["domestic:*"], wide=True),
    F("tpl_domestic_scheduled", "PIS", "template", "Domestic scheduled: request template (JSON)", "",
      "Empty = the standard template for your UK Open Banking version. Edit only if your financial institution "
      "needs something different.", testdata=True, data_for=["domestic-scheduled:*"], wide=True),
    F("tpl_domestic_standing_order", "PIS", "template", "Domestic standing order: request template (JSON)", "",
      "Empty = the standard template for your UK Open Banking version. Edit only if your financial institution "
      "needs something different.", testdata=True, data_for=["domestic-standing-order:*"], wide=True),
    F("tpl_international", "PIS", "template", "International: request template (JSON)", "",
      "Empty = the standard template for your UK Open Banking version. Edit only if your financial institution "
      "needs something different.", testdata=True, data_for=["international:*"], wide=True),
    F("tpl_international_scheduled", "PIS", "template", "International scheduled: request template (JSON)", "",
      "Empty = the standard template for your UK Open Banking version. Edit only if your financial institution "
      "needs something different.", testdata=True, data_for=["international-scheduled:*"], wide=True),
    F("tpl_international_standing_order", "PIS", "template", "International standing order: request template (JSON)", "",
      "Empty = the standard template for your UK Open Banking version. Edit only if your financial institution "
      "needs something different.", testdata=True, data_for=["international-standing-order:*"], wide=True),
    F("tpl_file", "PIS", "template", "File: request template (JSON)", "",
      "Empty = the standard template for your UK Open Banking version. Edit only if your financial institution "
      "needs something different.", testdata=True, data_for=["file:*"], wide=True),
    F("sign_payments", "PIS", "choice", "Sign payment requests (x-jws-signature)", "No",
      "Uses the tenant's signing key.", choices=["No", "Yes"], testdata=True, data_for=["*:consent-create", "*:submit"]),

    F("cof_tests", "CoF", "tests", "APIs to call", "", area_tests="CoF"),
    F("cof_source", "CoF", "source", "Token source", MANUAL, choices=SOURCES),
    F("cof_token", "CoF", "secret", "Access token", "", "From an authorised funds-confirmation consent.",
      show={"cof_source": MANUAL}),
    F("cof_consent_id", "CoF", "text", "Consent ID of that token", "", "", show={"cof_source": MANUAL}),
    F("cof_cc_token", "CoF", "secret", "Client-credentials token", "", "Only needed for the consent endpoints.",
      show={"cof_source": MANUAL}),
    F("cof_amount", "CoF", "text", "Amount to confirm", "10.00", testdata=True, data_for=['confirm']),
    F("cof_currency", "CoF", "text", "Currency", "GBP", testdata=True, data_for=['confirm']),
    F("debtor_name", "CoF", "text", "Debtor name", "Load Test Debtor", "Used when creating CoF consents.", testdata=True, data_for=['consent-create']),
    F("debtor_id", "CoF", "text", "Debtor account", "08080021325698", testdata=True, data_for=['consent-create']),
    F("debtor_scheme", "CoF", "text", "Debtor account scheme", "UK.OBIE.SortCodeAccountNumber", testdata=True, data_for=['consent-create']),
]
PROFILE_FIELDS = {f["key"]: f for f in PROFILE_SCHEMA}
PROFILE_SECRETS = {k for k, f in PROFILE_FIELDS.items() if f["kind"] == "secret"} | {"aisp_more_tokens", "pisp_authorised"}
LEGACY_TEST_KEYS = [k for k in PROFILE_FIELDS if k not in ("name", "tenant", "ais_tests", "pis_tests", "cof_tests",
                                                           "run_ais", "run_pis", "run_cof", "users", "spawn_rate")]


def default_weights(area):
    return {e["key"]: e["weight"] for e in ENDPOINTS[area]}


def new_profile_values(name="", tenant=""):
    v = {k: f["default"] for k, f in PROFILE_FIELDS.items()}
    v.update(name=name, tenant=tenant, weights={a: default_weights(a) for a in AREAS})
    return v


def _load_profiles():
    if os.path.exists(PROFILES_FILE):
        with open(PROFILES_FILE) as fh:
            return json.load(fh)
    return _migrate_profiles()


def _save_profiles(data):
    with _lock:
        _write_private(PROFILES_FILE, json.dumps(data, indent=2))


def _migrate_profiles():
    """First run with profiles: each tenant's old test settings and tokens become '<tenant> – default'."""
    data = {"profiles": {}}
    raw = _load_all()["tenants"]
    store = load_tokens()
    for tid, t in raw.items():
        if not any(k in t for k in ("ais_source", "pis_source", "cof_source", "aisp_token", "user_tps")) and tid not in store:
            continue
        pid = f"{tid}-default"
        p = new_profile_values(f"{t.get('name') or tid} – default", tid)
        p.update({k: t[k] for k in LEGACY_TEST_KEYS if k in t})
        for a in AREAS:
            supported = t.get(a.lower() + "_endpoints") or ""
            p[a.lower() + "_tests"] = supported
            p["run_" + a.lower()] = t.get("test_" + a.lower(), "No")
        data["profiles"][pid] = p
        if tid in store:
            store[pid] = store.pop(tid)
    if data["profiles"]:
        _save_profiles(data)
        save_tokens(store)
    return data


def list_profiles():
    tenants = {t["id"]: t for t in list_tenants()}
    runs_by_profile = {}
    for r in list_runs():
        runs_by_profile.setdefault(r.get("profile_id"), []).append(r)
    out = []
    for pid, p in _load_profiles()["profiles"].items():
        t = tenants.get(p.get("tenant"), {})
        run = None
        try:
            run = get_run(pid)
            issues = validate_profile(run)
        except KeyError as e:
            issues = [str(e).strip("'\"")]
        areas = [a for a in AREAS if p.get("run_" + a.lower()) == "Yes" and a in t.get("areas", [])]
        tk = load_tokens().get(pid, {})
        out.append({"id": pid, "name": p.get("name") or pid, "tenant": p.get("tenant"), "tenant_name": t.get("name", "(deleted)"),
                    "rs_host": t.get("rs_host", ""), "version": t.get("version", ""), "areas": areas,
                    "users": p.get("users", "20"), "spawn_rate": p.get("spawn_rate", "2"), "user_tps": p.get("user_tps", "2"),
                    "run_time": p.get("run_time", ""), "peak_rps": p.get("peak_rps", ""),
                    "stress_factor": p.get("stress_factor", "2"),
                    "issues": issues, "ready": not issues,
                    "tokens": {a: len(tk.get(a.lower(), [])) for a in AREAS},
                    "sources": {a: p.get(a.lower() + "_source", MANUAL) for a in AREAS},
                    "tests": {a: len([k for k in (p.get(a.lower() + "_tests") or "").split(",") if k]) for a in areas},
                    "runs": runs_by_profile.get(pid, [])[:1], "run_count": len(runs_by_profile.get(pid, []))})
    return sorted(out, key=lambda x: (x["tenant_name"].lower(), x["name"].lower()))


def get_profile(pid):
    p = _load_profiles()["profiles"].get(pid)
    if p is None:
        raise KeyError(f"Unknown test profile: {pid}")
    v = {**new_profile_values(), **p}
    saved = {a: dict((p.get("weights") or {}).get(a, {})) for a in AREAS}
    saved["PIS"] = {PIS_LEGACY.get(k, k): w for k, w in saved["PIS"].items()}
    v["weights"] = {a: {**default_weights(a), **saved[a]} for a in AREAS}
    v["pis_tests"] = migrate_pis_keys(v.get("pis_tests"))
    return v


def save_profile(pid, values):
    data = _load_profiles()
    clean = {k: ("" if v is None else str(v)) for k, v in values.items() if k in PROFILE_FIELDS}
    if not pid:
        base = slug(clean.get("name") or "profile")
        pid, n = base, 2
        while pid in data["profiles"]:
            pid, n = f"{base}-{n}", n + 1
        current = new_profile_values()
    else:
        current = {**new_profile_values(), **data["profiles"].get(pid, {})}
    current.update(clean)
    if isinstance(values.get("weights"), dict):
        current["weights"] = {a: {k: max(0, int(float(w))) for k, w in (values["weights"].get(a) or {}).items()}
                              for a in AREAS}
    data["profiles"][pid] = current
    _save_profiles(data)
    return pid, get_profile(pid)


def delete_profile(pid):
    data = _load_profiles()
    data["profiles"].pop(pid, None)
    _save_profiles(data)
    update_tokens(lambda t: t.pop(pid, None))


def resolve_profile(name_or_id=None):
    profiles = list_profiles()
    if name_or_id:
        for p in profiles:
            if name_or_id in (p["id"], p["name"]):
                return p["id"]
        raise KeyError(f"No test profile called '{name_or_id}'. Saved profiles: "
                       f"{', '.join(p['name'] for p in profiles) or 'none'}")
    ready = [p for p in profiles if p["ready"]] or profiles
    if not ready:
        raise KeyError("No test profiles yet: open Test profiles and create one.")
    return ready[0]["id"]


def get_run(pid):
    """Everything a test needs: the tenant's stable configuration with the profile on top."""
    p = get_profile(pid)
    if not p.get("tenant"):
        raise KeyError("No tenant profile chosen for this test profile.")
    t = get_tenant(p["tenant"])
    run = {**t, **{k: v for k, v in p.items() if k not in ("name", "tenant")},
           "_id": pid, "profile_name": p.get("name"), "tenant_id": p["tenant"], "tenant_name": t.get("name"),
           "name": f"{t.get('name')} · {p.get('name')}"}
    for a in AREAS:
        supported = set(filter(None, (t.get(a.lower() + "_endpoints") or "").split(",")))
        chosen = [k for k in (p.get(a.lower() + "_tests") or "").split(",") if k in supported]
        on = p.get("run_" + a.lower()) == "Yes" and t.get("test_" + a.lower()) == "Yes"
        run["test_" + a.lower()] = "Yes" if on else "No"
        run[a.lower() + "_endpoints"] = ",".join(chosen)   # what the test calls
    return run


RUN_TIME = re.compile(r"^\s*(\d+\s*$|(\d+h)?\s*(\d+m)?\s*(\d+s)?\s*$)", re.I)


def run_time_seconds(text):
    """'1h30m' -> 5400, '90' -> 90, '' -> None. Raises ValueError for anything else."""
    text = (text or "").strip().lower()
    if not text:
        return None
    if not RUN_TIME.match(text) or not re.search(r"\d", text):
        raise ValueError(f"Run time '{text}' isn't valid: use e.g. 30s, 10m or 1h30m (Load).")
    if text.isdigit():
        return int(text)
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def validate_profile(run):
    """Everything still missing before this profile can run, tenant problems included."""
    issues = [f"Tenant profile: {i}" for i in validate_tenant(get_tenant(run["tenant_id"]))]
    try:
        run_time_seconds(run.get("run_time"))
    except ValueError as e:
        issues.append(str(e))
    areas = [a for a in AREAS if run.get("test_" + a.lower()) == "Yes"]
    if not areas:
        issues.append("No area selected that the tenant profile supports (Scope).")
    for a in areas:
        if not run.get(a.lower() + "_endpoints"):
            issues.append(f"{a}: no APIs ticked.")
    toks = load_tokens().get(run["_id"], {})
    if "AIS" in areas:
        if run.get("ais_source") == MANUAL and not (run.get("aisp_token") or run.get("aisp_more_tokens")):
            issues.append("AIS: no access token.")
        if run.get("ais_source") == FLOW and not toks.get("ais"):
            issues.append("AIS: no authorised consent yet.")
    if "PIS" in areas and run.get("pis_source") == MANUAL and not run.get("pisp_cc_token"):
        issues.append("PIS: no client-credentials token.")
    if "CoF" in areas:
        eps = run.get("cof_endpoints", "").split(",")
        if "confirm" in eps and run.get("cof_source") == MANUAL and not (run.get("cof_token") and run.get("cof_consent_id")):
            issues.append("CoF: confirmations need an access token and its consent ID.")
        if "confirm" in eps and run.get("cof_source") == FLOW and not toks.get("cof"):
            issues.append("CoF: no authorised consent yet.")
    return issues


def export_profile(pid, include_secrets):
    p = get_profile(pid)
    out = {"_about": "Open Banking stress test profile. Import it on the Test profiles page.",
           "name": p.get("name"), "tenant": get_tenant(p["tenant"]).get("name") if p.get("tenant") else ""}
    for step in PROFILE_STEPS:
        sec = {}
        for f in PROFILE_SCHEMA:
            if f["step"] != step["id"] or f["key"] in ("name", "tenant"):
                continue
            v = p.get(f["key"], "")
            sec[f["key"]] = "" if f["key"] in PROFILE_SECRETS and not include_secrets else v
        if step.get("area"):
            sec["weights"] = p["weights"][step["area"]]
        if sec:
            out[step["title"]] = sec
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=120)


def import_profile(text):
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("The profile must be a YAML or JSON object.")
    flat, weights = {"name": data.get("name", "")}, {}
    for title, sec in data.items():
        if isinstance(sec, dict):
            area = next((s["area"] for s in PROFILE_STEPS if s["title"] == title and s.get("area")), None)
            if area and isinstance(sec.get("weights"), dict):
                weights[area] = sec["weights"]
            flat.update({k: v for k, v in sec.items() if k in PROFILE_FIELDS})
    tname = data.get("tenant") or ""
    try:
        flat["tenant"] = resolve_tenant(tname) if tname else ""
    except KeyError:
        flat["tenant"] = ""
    pid, _ = save_profile(None, {**flat, "weights": weights} if weights else flat)
    return pid, ([] if flat["tenant"] else [f"Tenant '{tname}' not found: choose one on the Profile step."])


# ------------------------------------------------------------ profile tokens ----
def tenant_tokens(pid):  # kept name: tokens now belong to a test profile
    t = load_tokens().get(pid, {})
    return {"ais": t.get("ais", []), "pis": t.get("pis", []), "cof": t.get("cof", []), "pending": t.get("pending", {})}


def create_consents(pid, area, count, ptype=None):
    """Create consents with a client-credentials token and return their authorisation links.
    For PIS, ptype is the payment type; file payments also get their payment file uploaded."""
    run = get_run(pid)
    host = (run.get("rs_host") or "").rstrip("/")
    if not host:
        raise FlowError("The tenant profile has no resource server URL (Tenant profiles → URLs).")
    auth = AuthClient(run)
    cc = auth.client_credentials(SCOPE[area])
    resource, body_fn, sign = CONSENT_RESOURCE[area]
    if area == "PIS":
        ptype = ptype if ptype in PAYMENT_TYPES else "domestic"
        resource = PAYMENT_TYPES[ptype]["consent"]
    created = []
    for _ in range(max(1, min(int(count), 200))):
        file = None
        if area == "PIS":
            body, file = payment_consent_body_for(run, ptype)
        else:
            body = body_fn(run)
        data = json.dumps(body).encode()
        extra = {"Content-Type": "application/json", "x-idempotency-key": uuid.uuid4().hex}
        if sign and (sig := jws_signature(run, data)):
            extra["x-jws-signature"] = sig
        base = f"{host}{api_path(run, area)}/{resource}"
        r = requests.post(base, data=data, headers=api_headers(run, cc, extra), cert=auth.cert, verify=auth.verify,
                          timeout=30)
        if r.status_code != 201:
            raise FlowError(f"Creating the consent failed ({r.status_code}): {r.text[:300]}")
        consent_id = r.json()["Data"]["ConsentId"]
        if file:   # file payments: upload the payment file before the PSU authorises
            fx = {"Content-Type": file["type"], "x-idempotency-key": uuid.uuid4().hex}
            if (sig := jws_signature(run, file["content"])):
                fx["x-jws-signature"] = sig
            fr = requests.post(f"{base}/{consent_id}/file", data=file["content"], headers=api_headers(run, cc, fx),
                               cert=auth.cert, verify=auth.verify, timeout=60)
            if fr.status_code not in (200, 201, 204):
                raise FlowError(f"Uploading the payment file failed ({fr.status_code}): {fr.text[:300]}")
        link, state, nonce = auth.authorise_url(consent_id, area)
        update_tokens(lambda s: _tok(s, pid)["pending"].__setitem__(state, {
            "area": area, "consent_id": consent_id, "nonce": nonce, "created": time.time(), "url": link,
            **({"ptype": ptype} if area == "PIS" else {})}))
        created.append({"consent_id": consent_id, "state": state, "url": link, **({"ptype": ptype} if ptype else {})})
    return created


def complete_authorisation(redirected_url, state_hint=None):
    """Swap the code from the redirect for tokens and store them with the right test profile.
    Accepts the full redirect URL, or just the authorisation code together with the consent's state."""
    text = (redirected_url or "").strip()
    if text and "://" not in text and "code=" not in text:
        params = {"code": text, "state": state_hint}
    else:
        parsed = urllib.parse.urlsplit(text)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        params.update(dict(urllib.parse.parse_qsl(parsed.fragment)))
        if state_hint and not params.get("state"):
            params["state"] = state_hint
    if "error" in params:
        raise FlowError(f"The financial institution returned an error: {params['error']} {params.get('error_description', '')}")
    code, state = params.get("code"), params.get("state")
    if not code:
        raise FlowError("No authorisation code found in that address. Paste the full URL after the redirect.")
    store = load_tokens()
    match = [(pid, s["pending"][state]) for pid, s in store.items() if state and state in s.get("pending", {})]
    if not match:
        pend = [(pid, k, v) for pid, s in store.items() for k, v in s.get("pending", {}).items()]
        if not state and len(pend) == 1:
            pid, state, p = pend[0]
            match = [(pid, p)]
        else:
            raise FlowError("This authorisation doesn't match a pending consent (unknown state). "
                            "It may have been completed already.")
    pid, p = match[0]
    tok = AuthClient(get_run(pid)).exchange_code(code)
    entry = {"consent_id": p["consent_id"], "access_token": tok["access_token"],
             **({"ptype": p["ptype"]} if p.get("ptype") else {}),
             "refresh_token": tok.get("refresh_token", ""), "expires_at": tok["expires_at"], "created": time.time()}
    area = p.get("area") or KIND.get(p.get("kind", "ais"), "AIS")

    def put(s):
        b = _tok(s, pid)
        b["pending"].pop(state, None)
        b[area.lower()].append(entry)
    update_tokens(put)
    return {"profile": pid, "area": area, "consent_id": p["consent_id"], "refresh": bool(entry["refresh_token"]),
            "expires_in": int(tok["expires_at"] - time.time())}


def token_overview(pid):
    t, now = tenant_tokens(pid), time.time()
    view = lambda e: {"consent_id": e["consent_id"], "expires_in": int(e.get("expires_at", 0) - now),
                      "refresh": bool(e.get("refresh_token")), "ptype": e.get("ptype", "domestic")}
    return {"AIS": [view(e) for e in t["ais"]], "PIS": [view(e) for e in t["pis"]], "CoF": [view(e) for e in t["cof"]],
            "pending": [{"state": k, "area": v.get("area") or KIND.get(v.get("kind", "ais"), "AIS"),
                         "consent_id": v["consent_id"], "url": v["url"], "ptype": v.get("ptype", "")}
                        for k, v in t["pending"].items()]}


def remove_tokens(pid, area, consent_id=None):
    def rm(s):
        b = _tok(s, pid)
        if area == "pending":
            b["pending"] = {k: v for k, v in b["pending"].items() if consent_id and v["consent_id"] != consent_id}
        else:
            b[area.lower()] = [e for e in b[area.lower()] if consent_id and e["consent_id"] != consent_id]
    update_tokens(rm)


# ================================================================= run history ====
# Each test run is saved in results/<date_time>/ (CSVs + reports). run.json links it to its test profile.
RESULTS_DIR = os.path.join(WORKDIR, "results")
RUN_ID = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}$")


def run_dir(run_id):
    if not RUN_ID.match(run_id or ""):
        raise KeyError("Unknown run")
    d = os.path.join(RESULTS_DIR, run_id)
    if not os.path.isdir(d):
        raise KeyError("Unknown run")
    return d


def record_run(run_id, info):
    """Called when a run ends: store which profile it belongs to, plus its summary."""
    d = os.path.join(RESULTS_DIR, run_id)
    os.makedirs(d, exist_ok=True)
    data = {**info, "summary": summarize_run(d)}
    with open(os.path.join(d, "run.json"), "w") as fh:
        json.dump(data, fh, indent=2)
    return data


def summarize_run(d):
    """A very general summary from the run's CSVs and the quick report's verdict."""
    import csv
    s = {}
    try:
        with open(os.path.join(d, "run_stats.csv"), newline="") as fh:
            agg = next((r for r in csv.DictReader(fh) if r.get("Name") == "Aggregated"), None)
        if agg:
            num = lambda k: float(agg.get(k) or 0)
            s.update(requests=int(num("Request Count")), failures=int(num("Failure Count")),
                     avg_ms=round(num("Average Response Time")), p95_ms=round(num("95%")), rps=round(num("Requests/s"), 1))
            s["error_pct"] = round(100 * s["failures"] / s["requests"], 2) if s["requests"] else 0
        with open(os.path.join(d, "run_stats_history.csv"), newline="") as fh:
            hist = [r for r in csv.DictReader(fh)]
        if hist:
            ts = [float(r["Timestamp"]) for r in hist if r.get("Timestamp")]
            s.update(started=min(ts), ended=max(ts), duration_s=int(max(ts) - min(ts)),
                     peak_users=int(max(float(r.get("User Count") or 0) for r in hist)),
                     peak_rps=round(max(float(r.get("Requests/s") or 0) for r in hist), 1))
    except FileNotFoundError:
        pass
    md = os.path.join(d, "run_summary.md")
    if os.path.exists(md):
        m = re.search(r"\*\*(Passed|Failed)\.\*\*\s*(.+)", open(md, encoding="utf-8").read())
        if m:
            s.update(verdict=m.group(1), verdict_text=m.group(2).strip())
    return s


def list_runs(pid=None):
    out = []
    if not os.path.isdir(RESULTS_DIR):
        return out
    for name in sorted(os.listdir(RESULTS_DIR), reverse=True):
        f = os.path.join(RESULTS_DIR, name, "run.json")
        if RUN_ID.match(name) and os.path.exists(f):
            try:
                data = json.load(open(f))
            except ValueError:
                continue
            if pid is None or data.get("profile_id") == pid:
                files = set(os.listdir(os.path.join(RESULTS_DIR, name)))
                out.append({"id": name, **data, "has": {"report": "run_report.html" in files,
                                                        "exemption": "run_exemption_report.html" in files}})
    return out


def delete_run(run_id):
    import shutil
    shutil.rmtree(run_dir(run_id))


def run_context(run, run_id):
    """Details for the report header: tenant profile, test profile, standard and every known URL."""
    d = run.get("discovery") or {}
    urls = [("Discovery document", run.get("well_known")),
            ("Issuer", run.get("issuer") or d.get("issuer")),
            ("Authorisation endpoint", run.get("auth_endpoint") or d.get("authorization_endpoint")),
            ("Token endpoint", run.get("token_endpoint") or d.get("token_endpoint")),
            ("Pushed authorisation (PAR) endpoint", d.get("pushed_authorization_request_endpoint")),
            ("Keys (JWKS)", d.get("jwks_uri")),
            ("Resource server", run.get("rs_host"))]
    def _num(v):
        try:
            return float(v) if str(v).strip() else None
        except ValueError:
            return None
    return {"run_id": run_id, "tenant": run.get("tenant_name"), "profile": run.get("profile_name"),
            "peak_rps": _num(run.get("peak_rps")), "stress_factor": _num(run.get("stress_factor")) or 2.0,
            "standard": f"UK Open Banking v{run.get('version', '')}",
            "areas": [a for a in AREAS if run.get("test_" + a.lower()) == "Yes"],
            "urls": [{"label": k, "url": v} for k, v in urls if v]}
