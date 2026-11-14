"""
Locust stress tests for UK Open Banking Read/Write APIs (v3.1 and v4.0): AIS, PIS and CoF.

Tenants (stable bank configuration):   http://localhost:8089/ob-tenants
Test profiles (load, APIs, tokens):     http://localhost:8089/ob-profiles
Choose the test profile on Locust's start page.
Headless:  locust -f locustfile.py --headless -u 20 -r 2 -t 10m --ob-profile "Peak AIS"
"""
import collections
import itertools
import json
import re
import logging
import os
import random
import threading
import time
import uuid
from datetime import datetime

from locust import HttpUser, between, constant_throughput, events

import ob_brand
import ob_core as core
import ob_web

log = logging.getLogger("openbanking")
MANUAL, FLOW = core.MANUAL, core.FLOW


# ------------------------------------------------------------- launch options ----
@events.init_command_line_parser.add_listener
def add_options(parser):
    # Shown on Locust's start page by ob_brand (tenant dropdown and rate field), so not in "Custom parameters"
    parser.add_argument("--ob-profile", dest="ob_profile", env_var="OB_PROFILE", default=os.getenv("OB_PROFILE", ""),
                        help="Test profile name or id (default: the first ready profile)", include_in_web_ui=False)
    parser.add_argument("--ob-user-tps", dest="ob_user_tps", env_var="OB_USER_TPS", default=os.getenv("OB_USER_TPS", ""),
                        help="Requests per second per user (default: the tenant's)", include_in_web_ui=False)


@events.init.add_listener
def headless_profile_settings(environment, **_):
    """Headless runs: users, ramp up and run time come from the test profile unless given on the command line."""
    o = environment.parsed_options
    if not o or not getattr(o, "headless", False) or getattr(o, "worker", False):
        return
    try:
        pid = core.resolve_profile((getattr(o, "ob_profile", "") or "").strip() or None)
        p = core.get_profile(pid)
    except KeyError:
        return
    if o.num_users is None and p.get("users"):
        o.num_users = int(float(p["users"]))
    if o.spawn_rate is None and p.get("spawn_rate"):
        o.spawn_rate = float(p["spawn_rate"])
    if not o.run_time:
        try:
            o.run_time = core.run_time_seconds(p.get("run_time"))
        except ValueError as e:
            log.warning("%s Running until stopped.", e)


@events.init.add_listener
def add_pages(environment, **_):
    if environment.web_ui is None:
        return
    app = environment.web_ui.app
    ob_web.register(app, environment)
    ob_brand.register(app)
    _protect(app)

    @app.before_request
    def start_from_profile():
        """Start: users, ramp up, requests per second, run time and host all come from the chosen test profile."""
        from flask import request
        from werkzeug.datastructures import ImmutableMultiDict
        if request.path != "/swarm" or request.method != "POST" or not request.form.get("ob_profile"):
            return None
        try:
            p = core.get_profile(request.form["ob_profile"])
            run_cfg = core.get_run(request.form["ob_profile"])
        except KeyError:
            return None
        form = [(k, v) for k, v in request.form.items(multi=True)
                if k not in ("user_count", "spawn_rate", "run_time", "host", "ob_user_tps")]
        form += [("user_count", str(int(float(p.get("users") or 1)))), ("spawn_rate", str(p.get("spawn_rate") or 1)),
                 ("host", run_cfg.get("rs_host", "")), ("ob_user_tps", str(p.get("user_tps") or ""))]
        if (p.get("run_time") or "").strip():
            form.append(("run_time", p["run_time"].strip()))
        request.form = ImmutableMultiDict(form)   # what Locust's start handler reads
        return None

    @app.before_request
    def live_rate_change():
        """Edit during a run: apply a new requests-per-second value straight away."""
        from flask import request
        if request.path == "/swarm" and request.method == "POST" and cfg.ready:
            tps = (request.form.get("ob_user_tps") or "").strip()
            if tps:
                try:
                    set_rate(float(tps))
                except ValueError:
                    pass


def _protect(app):
    """Optional login for every page (set OB_WEB_USER and OB_WEB_PASSWORD), e.g. when running on a server."""
    import hmac
    from flask import Response, request
    user, password = os.getenv("OB_WEB_USER", ""), os.getenv("OB_WEB_PASSWORD", "")

    @app.route("/ob-health")
    def ob_health():
        return {"ok": True}

    if not (user and password):
        return

    @app.before_request
    def require_login():
        if request.path == "/ob-health":
            return None
        a = request.authorization
        if a and hmac.compare_digest(a.username or "", user) and hmac.compare_digest(a.password or "", password):
            return None
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Open Banking Stress Test"'})


# ------------------------------------------------------------- live settings ----
class Cfg:
    ready = False
    error = None


cfg = Cfg()
_lock = threading.Lock()


def set_rate(tps):
    cfg.tps = tps
    cfg.wait = constant_throughput(tps) if tps > 0 else between(1, 3)
    log.info("Requests per second per user: %s", tps if tps > 0 else "random 1-3 s pauses")


def configure(environment):
    o = environment.parsed_options
    pid = core.resolve_profile((getattr(o, "ob_profile", "") or "").strip() or None)
    t = core.get_run(pid)
    issues = core.validate_profile(t)
    if issues:
        raise ValueError(f"Test profile '{t.get('name')}' isn't ready: " + " ".join(issues))
    c = Cfg()
    c.tid, c.t = pid, t   # tokens are stored per test profile
    c.weights = t.get("weights") or {}
    c.host = t["rs_host"].rstrip("/")
    c.areas = [a for a in core.AREAS if t.get("test_" + a.lower()) == "Yes"]
    c.paths = {a: core.api_path(t, a) for a in core.AREAS}
    c.eps = {a: set(filter(None, (t.get(a.lower() + "_endpoints") or "").split(","))) for a in core.AREAS}
    mix = {"AIS": t.get("mix_ais"), "PIS": t.get("mix_pis"), "CoF": t.get("mix_cof")}
    c.mix = {a: max(float(mix[a] or 0), 0.0) for a in c.areas} if len(c.areas) > 1 else {c.areas[0]: 1.0}
    if not any(c.mix.values()):
        c.mix = {a: 1.0 for a in c.areas}
    c._rr = {a: 0.0 for a in c.mix}
    c.auth = core.AuthClient(t)
    c.cert, c.verify = c.auth.cert, c.auth.verify
    store = core.tenant_tokens(pid)
    c.flow = {a: t.get(a.lower() + "_source") == FLOW for a in core.AREAS}
    # AIS
    if c.flow["AIS"]:
        c.ais_entries = store["ais"]
    else:
        toks = [t.get("aisp_token", "")] + (t.get("aisp_more_tokens") or "").splitlines()
        c.ais_entries = [{"consent_id": "", "access_token": x.strip(), "refresh_token": ""} for x in toks if x.strip()]
    c.ais_cycle = itertools.cycle(c.ais_entries or [None])
    # PIS: each authorised consent is submitted as a payment exactly once; reads are repeatable
    if c.flow["PIS"]:
        pis_entries = [dict(e) for e in store["pis"]]
    else:
        pis_entries = []
        for line in (t.get("pisp_authorised") or "").splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                ptype = parts[2] if len(parts) > 2 and parts[2] in core.PAYMENT_TYPES else "domestic"
                pis_entries.append({"consent_id": parts[0], "access_token": parts[1], "ptype": ptype})
    for e in pis_entries:
        e.setdefault("ptype", "domestic")
    pis_keys = set(c.eps.get("PIS", []))
    c.pis_types = sorted({k.split(":")[0] for k in pis_keys})
    c.pis = {"to_submit": collections.deque(e for e in pis_entries if f"{e['ptype']}:submit" in pis_keys),
             "authorised": [e for e in pis_entries if e["ptype"] in c.pis_types],
             "used": set(), "payments": [], "created": collections.deque(maxlen=500), "notices": set()}
    once = {e["key"] for e in core.ENDPOINTS["PIS"] if e.get("once")}
    weights = (t.get("weights") or {}).get("PIS", {})
    c.pis_actions = [(k, int(weights.get(k, next(e["weight"] for e in core.ENDPOINTS["PIS"] if e["key"] == k))))
                     for k in core.ENDPOINTS_ORDER["PIS"] if k in pis_keys and k not in once]
    c.pis_actions = [(k, w) for k, w in c.pis_actions if w > 0]
    n = len(c.pis["to_submit"])
    run_s = getattr(environment.parsed_options, "run_time", None) if environment.parsed_options else None
    window = (run_s * 0.9) if run_s else 60.0       # spread over the run (or the first minute)
    c.pis["interval"] = window / n if n else None
    for ptype in c.pis_types:      # reads of payments that this run never submits
        reads = [k for k in pis_keys if k.startswith(ptype + ":") and k.split(":")[1] in
                 ("payment-get", "payment-details", "report-file")]
        if reads and f"{ptype}:submit" not in pis_keys:
            c.pis["notices_due"] = c.pis.get("notices_due", []) + [
                f"PIS: {core.PAYMENT_TYPES[ptype]['short']} payment reads are ticked but payment submission isn't, so "
                "there are no payments to read. Tick the submission (once per consent) or untick the reads."]
    c.pis["next_due"] = time.time() + (c.pis["interval"] / 2 if n else 0)
    # CoF: authorised consents are reusable
    if c.flow["CoF"]:
        c.cof_entries = store["cof"]
    else:
        c.cof_entries = ([{"consent_id": t["cof_consent_id"], "access_token": t["cof_token"], "refresh_token": ""}]
                         if t.get("cof_token") and t.get("cof_consent_id") else [])
    c.cof_cycle = itertools.cycle(c.cof_entries or [None])
    c.manual_cc = {"AIS": t.get("aisp_cc_token", ""), "PIS": t.get("pisp_cc_token", ""), "CoF": t.get("cof_cc_token", "")}
    tps_raw = (getattr(o, "ob_user_tps", "") or "").strip() or t.get("user_tps") or "0"
    c.ready = True
    return c, float(tps_raw)


@events.test_start.add_listener
def on_test_start(environment, **_):
    global cfg
    try:
        cfg, tps = configure(environment)
        set_rate(tps)
        if environment.host != cfg.host:
            environment.host = cfg.host
        log.info("Test profile '%s' | %s | UK OB v%s | API %s | mix %s", cfg.t.get("name"), ", ".join(cfg.areas),
                 cfg.t.get("version"), cfg.host, {a: round(w, 2) for a, w in cfg.mix.items()})
    except Exception as e:  # noqa: BLE001
        cfg = Cfg()
        cfg.error = str(e)
        log.error("Settings problem: %s", e)


def next_role():
    """Smooth weighted round-robin: users follow the tenant's AIS/PIS/CoF mix evenly."""
    with _lock:
        total = sum(cfg.mix.values())
        for a, w in cfg.mix.items():
            cfg._rr[a] += w
        role = max(cfg._rr, key=cfg._rr.get)
        cfg._rr[role] -= total
        return role


def report_config_problem(user, message):
    user.environment.events.request.fire(request_type="CONFIG", name=message, response_time=0,
                                         response_length=0, exception=Exception(message), context={})
    log.error(message)
    user.disabled = True


def refresh_entry(entry, failed_token, kind):
    """Renew a user token with its refresh token (consent flow). Returns the token to use, or None."""
    if not entry.get("refresh_token"):
        return None
    with _lock:
        if entry["access_token"] != failed_token:
            return entry["access_token"]  # another user already renewed it
        if entry.get("_renew_failed") == failed_token:
            return None  # already tried for this token: don't hammer the token endpoint
        started = time.perf_counter()
        try:
            t = cfg.auth.refresh(entry["refresh_token"])
            events.request.fire(request_type="AUTH", name="Token renewal (refresh_token)",
                                response_time=(time.perf_counter() - started) * 1000, response_length=0,
                                exception=None, context={})
        except core.FlowError as e:
            events.request.fire(request_type="AUTH", name="Token renewal (refresh_token)",
                                response_time=(time.perf_counter() - started) * 1000, response_length=0,
                                exception=e, context={})
            entry["_renew_failed"] = failed_token
            log.error("Token renewal failed for %s consent %s: %s. Authorise a new consent on the Tenants page.",
                      kind.upper(), entry.get("consent_id"), e)
            return None
        entry.update(access_token=t["access_token"], refresh_token=t.get("refresh_token") or entry["refresh_token"],
                     expires_at=t["expires_at"])
        if kind in ("ais", "cof"):
            def persist(store):
                for e in core._tok(store, cfg.tid)[kind]:
                    if e["consent_id"] == entry["consent_id"]:
                        e.update(access_token=entry["access_token"], refresh_token=entry["refresh_token"],
                                 expires_at=entry["expires_at"])
            core.update_tokens(persist)
        log.info("Renewed token for %s consent %s", kind.upper(), entry.get("consent_id"))
        return entry["access_token"]


def _idle(user):
    pass


class OpenBankingUser(HttpUser):
    """One simulated TPP user; becomes an AIS, PIS or CoF user following the tenant's mix."""
    tasks = [_idle]
    disabled = False

    def wait_time(self):
        if self.disabled or not cfg.ready:
            return 3600
        return cfg.wait(self)

    def on_start(self):
        self.entry, self.account_ids, self.role, self.created = None, [], None, collections.deque(maxlen=50)
        if cfg.error:
            return report_config_problem(self, f"Settings problem: {cfg.error}")
        self.client.base_url = cfg.host
        self.client.cert, self.client.verify = cfg.cert, cfg.verify
        self.role = next_role()
        cls, eps = type(self), cfg.eps[self.role]
        wt = lambda key, default: int((cfg.weights.get(self.role) or {}).get(key, default))
        tasks = []
        if self.role == "AIS":
            with _lock:
                self.entry = next(cfg.ais_cycle)
            self.load_accounts()
            for e in core.ENDPOINTS["AIS"]:
                if e["key"] not in eps:
                    continue
                fn = {"accounts": cls.ais_accounts, "consent-create": cls.ais_consent_create,
                      "consent-get": cls.ais_consent_get, "consent-delete": cls.ais_consent_delete}.get(e["key"])
                if fn is None:
                    fn = (lambda key: (lambda u: u.ais_resource(key)))(e["key"])
                tasks += [fn] * wt(e["key"], e["weight"])
        elif self.role == "PIS":
            tasks = [cls.pis_tick]       # one-time submissions when due, otherwise a weighted repeatable call
        else:
            with _lock:
                self.entry = next(cfg.cof_cycle)
            fns = {"consent-create": cls.cof_consent_create, "consent-get": cls.cof_consent_get,
                   "consent-delete": cls.cof_consent_delete, "confirm": cls.cof_confirm}
            for e in core.ENDPOINTS["CoF"]:
                if e["key"] in eps:
                    tasks += [fns[e["key"]]] * wt(e["key"], e["weight"])
        if not tasks:
            return report_config_problem(self, f"{self.role}: no APIs with a weight above 0 in this test profile")
        self.tasks = tasks

    # --------------------------------------------------------------- helpers ----
    @staticmethod
    def headers(token, extra=None):
        return {k: v for k, v in core.api_headers(cfg.t, token, extra).items() if v}

    def call(self, method, path, token, name, body=None, expect=(200,), sign=False, renew=None, raw=False,
             file=None):
        """One request; `renew` gets a fresh token after a 401 (one retry). raw: don't expect JSON back.
        file: {"content": bytes, "type": content type} to send a payment file instead of JSON."""
        extra, data = {}, None
        if body is not None or file is not None:
            data = file["content"] if file is not None else json.dumps(body).encode()
            extra = {"Content-Type": file["type"] if file is not None else "application/json",
                     "x-idempotency-key": uuid.uuid4().hex}
            if sign and (sig := core.jws_signature(cfg.t, data)):
                extra["x-jws-signature"] = sig
        for attempt in (1, 2):
            expired = False
            with self.client.request(method, path, data=data, headers=self.headers(token, extra),
                                     name=name, catch_response=True) as r:
                if r.status_code == 401 and attempt == 1 and renew:
                    # timed separately: renew after this request's timer stops, then retry
                    expired = True
                    r.success()
                    r.request_meta["name"] = name + " (token expired)"
                if expired:
                    pass
                elif r.status_code == 401:
                    r.failure("401: token expired or invalid")
                    return None
                elif r.status_code not in expect:
                    r.failure(f"{r.status_code}: {r.text[:300]}")
                    return None
                elif not r.content:
                    return {}
                elif raw:
                    return {"_raw": len(r.content)}
                else:
                    try:
                        return r.json()
                    except ValueError:
                        r.failure("Response is not JSON")
                        return None
            if expired:
                new = renew()
                if not new:
                    self.environment.events.request.fire(
                        request_type="AUTH", name=name + " (token could not be renewed)", response_time=0,
                        response_length=0, exception=Exception("401 and the token could not be renewed"), context={})
                    return None
                token = new

    def cc_call(self, area, method, path, name, body=None, expect=(200,), sign=False, raw=False, file=None):
        """Call with a client-credentials token: pasted (manual) or fetched and renewed (consent flow)."""
        try:
            token = cfg.auth.client_credentials(core.SCOPE[area]) if cfg.flow[area] else cfg.manual_cc[area]
        except core.FlowError as e:
            self.environment.events.request.fire(request_type="AUTH", name=f"client credentials ({core.SCOPE[area]})",
                                                 response_time=0, response_length=0, exception=e, context={})
            return None
        if not token:
            self.environment.events.request.fire(request_type="CONFIG", name=f"{area}: client-credentials token is empty",
                                                 response_time=0, response_length=0,
                                                 exception=Exception("Add it on the Tenants page"), context={})
            return None

        def renew():
            if not cfg.flow[area]:
                return None
            cfg.auth._cc.pop(core.SCOPE[area], None)
            return cfg.auth.client_credentials(core.SCOPE[area])
        return self.call(method, f"{cfg.paths[area]}{path}", token, name, body, expect, sign, renew, raw, file)

    def user_call(self, area, kind, method, path, name, body=None, expect=(200,), sign=False):
        if not self.entry:
            return None
        token = self.entry["access_token"]
        renew = (lambda: refresh_entry(self.entry, token, kind)) if cfg.flow[area] else None
        return self.call(method, f"{cfg.paths[area]}{path}", token, name, body, expect, sign, renew)

    # ------------------------------------------------------------------- AIS ----
    def load_accounts(self):
        body = self.user_call("AIS", "ais", "GET", "/accounts", "GET /accounts")
        if body:
            self.account_ids = [a["AccountId"] for a in body.get("Data", {}).get("Account", [])]

    def ais_accounts(self):
        self.load_accounts()

    def ais_resource(self, key):
        if key.startswith("bulk-"):
            ep = key[5:]
            return self.user_call("AIS", "ais", "GET", f"/{ep}", f"GET /{ep}")
        if not self.account_ids:
            return
        acc = random.choice(self.account_ids)
        if key == "account":
            return self.user_call("AIS", "ais", "GET", f"/accounts/{acc}", "GET /accounts/[id]")
        return self.user_call("AIS", "ais", "GET", f"/accounts/{acc}/{key}", f"GET /accounts/[id]/{key}")

    def _consent_create(self, area, resource, body, sign=False):
        res = self.cc_call(area, "POST", f"/{resource}", f"POST /{resource}", body, expect=(201,), sign=sign)
        cid = (res or {}).get("Data", {}).get("ConsentId")
        if cid:
            self.created.append(cid)

    def _consent_get(self, area, resource):
        if self.created:
            self.cc_call(area, "GET", f"/{resource}/{self.created[-1]}", f"GET /{resource}/[id]")

    def _consent_delete(self, area, resource):
        if self.created:
            cid = self.created.popleft()
            self.cc_call(area, "DELETE", f"/{resource}/{cid}", f"DELETE /{resource}/[id]", expect=(204, 200))

    def ais_consent_create(self):
        self._consent_create("AIS", "account-access-consents", core.account_consent_body(cfg.t))

    def ais_consent_get(self):
        self._consent_get("AIS", "account-access-consents")

    def ais_consent_delete(self):
        self._consent_delete("AIS", "account-access-consents")

    # ------------------------------------------------------------------- PIS ----
    def pis_notice(self, message):
        """Tell the user once why PIS has nothing to do, instead of going quiet."""
        if message in cfg.pis["notices"]:
            return
        cfg.pis["notices"].add(message)
        self.environment.events.request.fire(request_type="CONFIG", name=message, response_time=0, response_length=0,
                                             exception=Exception(message), context={})
        log.warning(message)

    def pis_tick(self):
        P = cfg.pis
        for message in P.pop("notices_due", []):
            self.pis_notice(message)
        item = None
        with _lock:
            if P["to_submit"] and P["interval"] and time.time() >= P["next_due"]:
                item = P["to_submit"].popleft()
                P["next_due"] += P["interval"]
        if item:
            return self.pis_submit(item)
        ready = [(k, w) for k, w in cfg.pis_actions if self.pis_can(k)]
        if not ready:
            if P["to_submit"]:
                self.pis_notice("PIS: waiting for the next payment submission; none of the ticked repeatable calls can "
                                "run before payments exist. Tick consent reads or funds checks to keep the load going.")
            elif not cfg.pis_actions:
                self.pis_notice(f"PIS: all {len(P['authorised'])} authorised payment consents have been submitted and no "
                                "repeatable calls are ticked. Authorise more consents or tick repeatable calls.")
            else:
                self.pis_notice("PIS: none of the ticked calls can run: they need authorised consents or submitted "
                                "payments. Authorise consents in the test profile (PIS tokens).")
            gevent.sleep(0.5)
            return None
        key = random.choices([k for k, _ in ready], [w for _, w in ready])[0]
        return self.pis_do(key)

    def _pis_unused(self, ptype):
        return [e for e in cfg.pis["authorised"] if e["ptype"] == ptype and e["consent_id"] not in cfg.pis["used"]]

    def _pis_consents(self, ptype):
        return ([e["consent_id"] for e in cfg.pis["authorised"] if e["ptype"] == ptype] +
                [cid for t, cid in cfg.pis["created"] if t == ptype])

    def pis_can(self, key):
        ptype, what = key.split(":", 1)
        if what == "consent-create":
            return True
        if what in ("consent-get", "file-get"):
            return bool(self._pis_consents(ptype))
        if what == "funds-check":
            return bool(self._pis_unused(ptype))
        if what in ("payment-get", "payment-details", "report-file"):
            return any(t == ptype for t, _ in cfg.pis["payments"])
        return False

    def pis_do(self, key):
        ptype, what = key.split(":", 1)
        p = core.PAYMENT_TYPES[ptype]
        if what == "consent-create":
            body, file = core.payment_consent_body_for(cfg.t, ptype)
            res = self.cc_call("PIS", "POST", f"/{p['consent']}", f"POST /{p['consent']}", body, expect=(201,), sign=True)
            cid = (res or {}).get("Data", {}).get("ConsentId")
            if cid and file:
                self.cc_call("PIS", "POST", f"/{p['consent']}/{cid}/file", f"POST /{p['consent']}/[id]/file",
                             expect=(200, 201, 204), sign=True, raw=True, file=file)
            if cid:
                cfg.pis["created"].append((ptype, cid))
        elif what == "consent-get":
            cid = random.choice(self._pis_consents(ptype))
            self.cc_call("PIS", "GET", f"/{p['consent']}/{cid}", f"GET /{p['consent']}/[id]")
        elif what == "file-get":
            cid = random.choice(self._pis_consents(ptype))
            self.cc_call("PIS", "GET", f"/{p['consent']}/{cid}/file", f"GET /{p['consent']}/[id]/file", raw=True)
        elif what == "funds-check":
            e = random.choice(self._pis_unused(ptype))
            renew = (lambda: refresh_entry(e, e["access_token"], "pis")) if cfg.flow["PIS"] else None
            self.call("GET", f"{cfg.paths['PIS']}/{p['consent']}/{e['consent_id']}/funds-confirmation", e["access_token"],
                      f"GET /{p['consent']}/[id]/funds-confirmation", renew=renew)
        else:
            pid = random.choice([i for t, i in cfg.pis["payments"] if t == ptype])
            suffix = {"payment-get": "", "payment-details": "/payment-details", "report-file": "/report-file"}[what]
            self.cc_call("PIS", "GET", f"/{p['payment']}/{pid}{suffix}", f"GET /{p['payment']}/[id]{suffix}",
                         raw=(what == "report-file"))

    def pis_submit(self, item):
        """Submit one authorised consent as its payment: the only call that uses the consent up."""
        ptype, cid = item["ptype"], item["consent_id"]
        p = core.PAYMENT_TYPES[ptype]
        cfg.pis["used"].add(cid)
        if cfg.flow["PIS"]:      # single use: never offered again, also not in later runs
            core.update_tokens(lambda s: core._tok(s, cfg.tid).__setitem__(
                "pis", [e for e in core._tok(s, cfg.tid)["pis"] if e["consent_id"] != cid]))
            if item.get("expires_at", 1e18) - 10 < time.time():
                refresh_entry(item, item["access_token"], "pis")
        renew = (lambda: refresh_entry(item, item["access_token"], "pis")) if cfg.flow["PIS"] else None
        consent = self.cc_call("PIS", "GET", f"/{p['consent']}/{cid}", f"GET /{p['consent']}/[id]")
        if not consent or "Initiation" not in consent.get("Data", {}):
            return None
        body = {"Data": {"ConsentId": cid, "Initiation": consent["Data"]["Initiation"]}, "Risk": consent.get("Risk", {})}
        res = self.call("POST", f"{cfg.paths['PIS']}/{p['payment']}", item["access_token"], f"POST /{p['payment']}",
                        body, expect=(201,), sign=True, renew=renew)
        pid = (res or {}).get("Data", {}).get(p["id"])
        if pid:
            cfg.pis["payments"].append((ptype, pid))

    # ------------------------------------------------------------------- CoF ----
    def cof_consent_create(self):
        self._consent_create("CoF", "funds-confirmation-consents", core.cof_consent_body(cfg.t))

    def cof_consent_get(self):
        self._consent_get("CoF", "funds-confirmation-consents")

    def cof_consent_delete(self):
        self._consent_delete("CoF", "funds-confirmation-consents")

    def cof_confirm(self):
        if not self.entry:
            return
        self.user_call("CoF", "cof", "POST", "/funds-confirmations", "POST /funds-confirmations",
                       core.funds_confirmation_body(cfg.t, self.entry["consent_id"]), expect=(201, 200))


# ------------------------------------------------------ reports in the web UI ----
# Every run is saved to results/<date_time>/ when it ends, with the quick report.
# The Download Data tab gets links to the quick report, summary and exemption report.
import csv as _csv
import importlib.util
import subprocess
import sys

import gevent
from flask import Response, send_file
from locust.runners import WorkerRunner
from locust.stats import PERCENTILES_TO_REPORT, StatsCSV

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(os.getcwd(), "results")
EXEMPTION_CONFIG = os.getenv("OB_EXEMPTION_CONFIG", os.path.join(os.getcwd(), "exemption_config.yaml"))
HAS_PDF = importlib.util.find_spec("playwright") is not None
run = {"dir": None, "history": [], "sampler": None}
# Per-second detail for the report: requests, errors by status code and per-endpoint timings (API calls only)
timeline = {"secs": {}, "errors": {}, "phases": {}, "rtt": []}

# Connection setup (DNS + TCP, then TLS) is timed inside urllib3 and attributed to the request that opened it.
_conn_log = {}   # greenlet -> [(started_epoch, tcp_ms, tls_ms), ...]


def _patch_connection_timing():
    try:
        import urllib3.connection as uc
    except ImportError:
        return
    if getattr(uc.HTTPSConnection, "_ob_timed", False) or not hasattr(uc.HTTPConnection, "_new_conn"):
        return
    orig_new, orig_connect = uc.HTTPConnection._new_conn, uc.HTTPSConnection.connect

    def new_conn(self):
        t = time.perf_counter()
        try:
            return orig_new(self)
        finally:
            self._ob_tcp_ms = (time.perf_counter() - t) * 1000

    def connect(self):
        started, t = time.time(), time.perf_counter()
        self._ob_tcp_ms = None
        result = orig_connect(self)
        total = (time.perf_counter() - t) * 1000
        tcp = self._ob_tcp_ms if self._ob_tcp_ms is not None else total
        _conn_log.setdefault(gevent.getcurrent(), []).append((started, tcp, max(0.0, total - tcp)))
        return result
    uc.HTTPConnection._new_conn, uc.HTTPSConnection.connect = new_conn, connect
    uc.HTTPSConnection._ob_timed = True


_patch_connection_timing()


def _error_message(exception):
    text = str(exception)
    m = re.match(r"^\w+\((['\"])(.*)\1\)$", text, re.S)   # CatchResponseError('503: ...') -> 503: ...
    return (m.group(2) if m else text)[:240]


@events.request.add_listener
def record_timeline(request_type, name, response_time, response_length, response=None, context=None,
                    exception=None, **_):
    conns = _conn_log.pop(gevent.getcurrent(), [])
    if not run["dir"] or request_type in ("AUTH", "CONFIG") or name.endswith("(token expired)"):
        return
    sec = int(time.time())
    b = timeline["secs"].setdefault(sec, {"n": 0, "codes": {}, "ep": {}})
    b["n"] += 1
    ep = b["ep"].setdefault(name, [0, 0.0])
    ep[0] += 1
    ep[1] += float(response_time or 0)
    _record_phases(name, response_time, response, exception, conns, _.get("start_time"))
    if exception is not None:
        code = int(getattr(response, "status_code", 0) or 0)
        key = str(code) if code else "no reply"
        b["codes"][key] = b["codes"].get(key, 0) + 1
        e = timeline["errors"].setdefault(key, {"count": 0, "first": sec, "last": sec, "endpoints": {},
                                                "example": _error_message(exception)})
        e["count"] += 1
        e["last"] = sec
        e["endpoints"][name] = e["endpoints"].get(name, 0) + 1


def _record_phases(name, response_time, response, exception, conns, start_time):
    """Split one response time: connection setup, waiting for the first byte, download."""
    elapsed = getattr(response, "elapsed", None)
    if response is None or elapsed is None or not getattr(response, "status_code", 0):
        return   # no reply: nothing to split
    mine = [c for c in conns if start_time is None or c[0] >= start_time - 0.05]
    tcp = sum(c[1] for c in mine)
    tls = sum(c[2] for c in mine)
    to_headers = elapsed.total_seconds() * 1000          # connection setup + sending + waiting for the first byte
    wait = max(0.0, to_headers - tcp - tls)
    download = max(0.0, float(response_time or 0) - to_headers)
    p = timeline["phases"].setdefault(name, {"n": 0, "conn_n": 0, "tcp_ms": 0.0, "tls_ms": 0.0, "wait_ms": 0.0,
                                            "dl_ms": 0.0, "total_ms": 0.0})
    p["n"] += 1
    p["conn_n"] += len(mine)
    p["tcp_ms"] += tcp
    p["tls_ms"] += tls
    p["wait_ms"] += wait
    p["dl_ms"] += download
    p["total_ms"] += float(response_time or 0)
    b = timeline["secs"].get(int(time.time()))
    if b is not None:
        b["w"] = b.get("w", 0.0) + wait
        b["wn"] = b.get("wn", 0) + 1


def _probe_network(host_url, every=5):
    """Network round trip to the resource server: time to open a TCP connection, every few seconds."""
    import socket
    from urllib.parse import urlsplit
    u = urlsplit(host_url or "")
    if not u.hostname:
        return
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        ip = socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)[0][4][0]
    except OSError:
        return
    while True:
        t = time.perf_counter()
        try:
            s = socket.create_connection((ip, port), timeout=5)
            ms = (time.perf_counter() - t) * 1000
            s.close()
        except OSError:
            ms = None
        timeline["rtt"].append([int(time.time()), None if ms is None else round(ms, 2)])
        gevent.sleep(every)


def _write_timeline(prefix):
    secs = [[s, b["n"], b["codes"], {k: [v[0], round(v[1], 1)] for k, v in b["ep"].items()},
             [b.get("wn", 0), round(b.get("w", 0.0), 1)]]
            for s, b in sorted(timeline["secs"].items())]
    phases = {k: {f: (round(v, 1) if isinstance(v, float) else v) for f, v in p.items()}
              for k, p in timeline["phases"].items()}
    with open(f"{prefix}_timeline.json", "w") as fh:
        json.dump({"version": 2, "secs": secs, "errors": timeline["errors"], "phases": phases,
                   "rtt": timeline["rtt"]}, fh, separators=(",", ":"))


def _is_worker(environment):
    return isinstance(environment.runner, WorkerRunner)


def _sample(environment):
    """One aggregated row per second: users, req/s, failures/s and rolling percentiles."""
    total = environment.stats.total
    while True:
        def pct(p):
            v = total.get_current_response_time_percentile(p) if total.use_response_times_cache else None
            return v if v is not None else (total.get_response_time_percentile(p) if total.num_requests else "")
        run["history"].append([int(time.time()), environment.runner.user_count, "", "Aggregated",
                               round(total.current_rps or 0, 2), round(total.current_fail_per_sec or 0, 2),
                               pct(0.5), pct(0.95), pct(0.99)])
        gevent.sleep(1)


def _snapshot(environment):
    """Write the current numbers as Locust-format CSVs into this run's folder; return the prefix."""
    if not run["dir"]:
        return None
    os.makedirs(run["dir"], exist_ok=True)
    prefix = os.path.join(run["dir"], "run")
    writer = StatsCSV(environment, PERCENTILES_TO_REPORT)
    for suffix, fn in (("stats", writer.requests_csv), ("failures", writer.failures_csv),
                       ("exceptions", writer.exceptions_csv)):
        with open(f"{prefix}_{suffix}.csv", "w", newline="") as fh:
            fn(_csv.writer(fh))
    with open(f"{prefix}_stats_history.csv", "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["Timestamp", "User Count", "Type", "Name", "Requests/s", "Failures/s", "50%", "95%", "99%"])
        w.writerows(run["history"])
    _write_timeline(prefix)
    return prefix


def _label(environment):
    if not cfg.ready:
        return environment.host or ""
    return f"{cfg.t.get('name')} – UK OB v{cfg.t.get('version')} – {cfg.host}"


def _build(environment, kind):
    """Create the requested report from the latest numbers. Returns (path, error)."""
    prefix = _snapshot(environment)
    if not prefix:
        return None, "No test has been run yet. Start a test first."
    if kind in ("report", "summary"):
        cmd = [sys.executable, os.path.join(HERE, "report.py"), prefix, "--env", _label(environment)]
        out = f"{prefix}_report.html" if kind == "report" else f"{prefix}_summary.md"
    else:
        if not os.path.exists(EXEMPTION_CONFIG):
            return None, f"Exemption config not found: {EXEMPTION_CONFIG}. Copy exemption_config.yaml there."
        cmd = [sys.executable, os.path.join(HERE, "exemption_report.py"), prefix, "--config", EXEMPTION_CONFIG]
        if kind == "exemption-pdf":
            cmd.append("--pdf")
        out = f"{prefix}_exemption_report." + ("pdf" if kind == "exemption-pdf" else "html")
    proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)  # exit code 1 = failed verdict, still fine
    if not os.path.exists(out) or os.path.getmtime(out) < time.time() - 120:
        return None, f"Report could not be created:\n{proc.stdout}\n{proc.stderr}"
    return out, None


@events.init.add_listener
def add_report_links(environment, **_):
    if environment.web_ui is None:
        return
    app = environment.web_ui.app

    @app.route("/ob-reports/<kind>")
    def ob_report(kind):
        if kind not in ("report", "summary", "exemption", "exemption-pdf"):
            return Response("Unknown report", status=404, mimetype="text/plain")
        path, error = _build(environment, kind)
        if error:
            return Response(error, status=200, mimetype="text/plain")
        return send_file(path, as_attachment=kind in ("summary", "exemption-pdf"),
                         download_name=os.path.basename(path))

    links = [{"href": "/ob-reports/report", "title": "Open Banking stress test report (HTML)"},
             {"href": "/ob-reports/summary", "title": "Open Banking summary (Markdown)"}]
    environment.web_ui.template_args["extendedCsvFiles"] = links


@events.test_start.add_listener
def start_run_record(environment, **_):
    if _is_worker(environment):
        return
    run["dir"] = os.path.join(RESULTS, datetime.now().strftime("%Y-%m-%d_%H%M%S"))
    run["history"] = []
    timeline["secs"], timeline["errors"], timeline["phases"], timeline["rtt"] = {}, {}, {}, []
    _conn_log.clear()
    if run.get("probe"):
        run["probe"].kill(block=False)
    run["probe"] = gevent.spawn(_probe_network, cfg.host if cfg.ready else environment.host)
    if cfg.ready:
        try:
            os.makedirs(run["dir"], exist_ok=True)
            with open(os.path.join(run["dir"], "context.json"), "w") as fh:
                json.dump(core.run_context(cfg.t, os.path.basename(run["dir"])), fh, indent=2)
        except OSError as e:
            log.warning("Could not save the run context: %s", e)
    if run["sampler"]:
        run["sampler"].kill(block=False)
    run["sampler"] = gevent.spawn(_sample, environment)


@events.test_stop.add_listener
def finish_run_record(environment, **_):
    if _is_worker(environment) or not run["dir"]:
        return
    if run["sampler"]:
        run["sampler"].kill(block=False)
        run["sampler"] = None
    if run.get("probe"):
        run["probe"].kill(block=False)
        run["probe"] = None
    path, error = _build(environment, "report")
    if cfg.ready:
        core.record_run(os.path.basename(run["dir"]), {
            "profile_id": cfg.tid, "profile_name": cfg.t.get("profile_name"), "tenant_name": cfg.t.get("tenant_name"),
            "areas": cfg.areas, "user_tps": getattr(cfg, "tps", None),
            "started_by": "web" if environment.web_ui else "headless"})
    log.info("Run saved to %s%s", run["dir"], "" if not error else f" (report not created: {error.splitlines()[0]})")
