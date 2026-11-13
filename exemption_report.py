#!/usr/bin/env python3
"""
Stress test report for a PSD2 fallback (contingency mechanism) exemption application:
Art. 33(6) RTS on SCA & CSC (EU 2018/389) and EBA/GL/2018/07 Guideline 4.

Combines Locust CSV results with a YAML file holding the facts the test data
cannot provide (bank details, load basis, TPPs, environment, monitoring,
issues, PSU-interface figures, sign-off).

    python exemption_report.py results/run1 --config exemption_config.yaml --pdf

Writes <prefix>_exemption_report.html (and .pdf with --pdf, needs Playwright).
"""
import argparse
import base64
import html
import mimetypes
import os
import re
import sys
from datetime import datetime, timezone

import yaml

import report as rpt  # charts and helpers from report.py

PCT_COLS = [(50, "50%"), (66, "66%"), (75, "75%"), (80, "80%"), (90, "90%"), (95, "95%"),
            (98, "98%"), (99, "99%"), (99.9, "99.9%"), (99.99, "99.99%"), (100, "100%")]
SERVICE_NAMES = {"AIS": "Account information (AIS)", "PIS": "Payment initiation (PIS)",
                 "CoF": "Confirmation of funds (CoF)", "Security": "Authorisation and security"}
e = html.escape


# ------------------------------------------------------------------ data ----

def service_of(name):
    n = name.lower()
    if "funds-confirmations" in n or "cbpii" in n:  # CBPII; PIS consent funds checks stay PIS
        return "CoF"
    if re.search(r"token|/par\b|par end-point|well-?known|authori[sz]e|jwt|jwks", n):
        return "Security"
    if re.search(r"payment", n) and not re.search(r"scheduled-?payments", n):
        return "PIS"
    return "AIS"


def load_endpoints(prefix):
    rows = rpt.read_csv(f"{prefix}_stats.csv")
    if not rows:
        sys.exit(f"Missing or empty {prefix}_stats.csv - run Locust with --csv {prefix}")
    eps, total = [], None
    for r in rows:
        n = rpt.num
        count, fails = int(n(r.get("Request Count")) or 0), int(n(r.get("Failure Count")) or 0)
        pts, hi = [(0.0, n(r.get("Min Response Time")) or 0.0)], 0.0
        for p, col in PCT_COLS:
            v = n(r.get(col))
            if v is not None:
                hi = max(hi, v)
                pts.append((p, hi))
        ep = {"method": r.get("Type", ""), "name": r.get("Name", ""), "count": count, "fails": fails,
              "err_pct": 100.0 * fails / count if count else 0.0,
              "avg": n(r.get("Average Response Time")), "min": n(r.get("Min Response Time")),
              "max": n(r.get("Max Response Time")), "p50": n(r.get("50%")), "p90": n(r.get("90%")),
              "p95": n(r.get("95%")), "p99": n(r.get("99%")), "rps": n(r.get("Requests/s")) or 0.0,
              "size": n(r.get("Average Content Size")) or 0.0, "pts": pts}
        if ep["name"] == "Aggregated":
            total = ep
        else:
            ep["service"] = service_of(ep["name"])
            eps.append(ep)
    return eps, total


def frac_within(pts, t):
    """Share of requests at or under t ms, interpolated from Locust's percentiles."""
    if not pts or t < pts[0][1]:
        return 0.0
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        if t <= v2:
            return (p2 if v2 == v1 else p1 + (p2 - p1) * (t - v1) / (v2 - v1)) / 100
    return 1.0


def apdex(ep, t, f):
    s, tol = frac_within(ep["pts"], t), frac_within(ep["pts"], f)
    return s + (tol - s) / 2


def apdex_rating(a):
    return next(lbl for lim, lbl in ((0.94, "Excellent"), (0.85, "Good"), (0.70, "Fair"),
                                     (0.50, "Poor"), (0, "Unacceptable")) if a >= lim)


def get(cfg, path, default=None):
    cur = cfg
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur or cur[key] in (None, ""):
            return default
        cur = cur[key]
    return cur


def fmt_size(b):
    return f"{b / 1024:,.1f} KB" if b >= 1024 else f"{b:,.0f} B"


def fmt_date(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%-d %B %Y")
    return str(value)


# ------------------------------------------------------------ assessment ----

def assess(cfg, eps, total, hist, bp, sl):
    """Evidence for each limb of EBA GL 4.2 and the overall verdict."""
    peak_users = max((h["users"] for h in hist), default=get(cfg, "test.load_model.test_concurrent", 0))
    psu_peak = get(cfg, "test.load_model.psu_peak_concurrent")
    planned = get(cfg, "test.load_model.test_concurrent", peak_users)
    tpps = get(cfg, "test.tpps", [])
    offered = set(get(cfg, "test.services_offered", ["AIS"]))
    tested = {ep["service"] for ep in eps if ep["count"]} - {"Security"}
    within_sla = total["err_pct"] <= sl["max_error_pct"] and (total["p95"] or 0) <= sl["p95_ms"]
    rows = []

    # (a) multiple TPPs
    roles = sorted({r for t in tpps for r in t.get("roles", [])})
    rows.append({
        "ref": "4.2(a)", "req": "Access by multiple PISPs, CBPIIs and AISPs",
        "evidence": (f"{len(tpps)} TPP identities used concurrently "
                     f"({', '.join(t['name'] for t in tpps)}), covering the roles {', '.join(roles)}."
                     if tpps else "No TPP identities were recorded in the configuration."),
        "status": "Demonstrated" if len(tpps) >= 2 else "Not demonstrated"})

    # (b) extremely high number of requests in a short time
    dur = (hist[-1]["ts"] - hist[0]["ts"]) if len(hist) > 1 else get(cfg, "test.duration_s", 0)
    peak_rps = max((h["rps"] for h in hist), default=total["rps"])
    ok_b = total["err_pct"] <= sl["max_error_pct"] and not bp
    rows.append({
        "ref": "4.2(b)", "req": "An extremely high number of requests in a short period without failing",
        "evidence": (f"{total['count']:,} requests in {rpt.fmt_duration(dur)}, peaking at {peak_rps:,.0f} "
                     f"requests/s, with {100 - total['err_pct']:.2f}% answered successfully."
                     + ("" if not bp else f" Service degraded beyond targets at {bp['users']:,.0f} "
                                          "concurrent users.")),
        "status": "Demonstrated" if ok_b else "Not demonstrated"})

    # (c) extremely high number of concurrent sessions per service
    missing = offered - tested
    uplift = (f" This is {peak_users / psu_peak:.1f}× the PSU-interface peak of {psu_peak:,} "
              f"concurrent users ({(peak_users / psu_peak - 1) * 100:.0f}% above it)."
              if psu_peak else "")
    status_c = ("Not demonstrated" if peak_users < 0.95 * planned or (psu_peak and peak_users <= psu_peak)
                else "Partially demonstrated" if missing else "Demonstrated")
    rows.append({
        "ref": "4.2(c)", "req": "An extremely high number of concurrent sessions for payment initiation, "
                                "confirmation of funds and account information",
        "evidence": (f"{peak_users:,.0f} concurrent sessions sustained across "
                     f"{', '.join(SERVICE_NAMES[s] for s in sorted(tested)) or 'no services'}.{uplift}"
                     + (f" Not exercised: {', '.join(SERVICE_NAMES.get(s, s) for s in sorted(missing))}."
                        if missing else "")),
        "status": status_c})

    # (d) large volumes of data
    big = max((x for x in eps if x["count"]), key=lambda x: x["size"], default=None)
    desc = get(cfg, "test.large_data")
    ev_d = (desc + " " if desc else "") + (
        f"Largest average response was {fmt_size(big['size'])} on {rpt.label(big)} "
        f"(p95 {rpt.fmt_ms(big['p95'])}, {big['err_pct']:.2f}% errors)." if big else "")
    rows.append({
        "ref": "4.2(d)", "req": "Requests for large volumes of data",
        "evidence": ev_d or "No large-data scenario was described.",
        "status": ("Demonstrated" if desc and big and big["err_pct"] <= sl["max_error_pct"]
                   else "Evidence required")})

    passed = within_sla and all(r["status"] == "Demonstrated" for r in rows)
    return {"rows": rows, "passed": passed, "within_sla": within_sla, "peak_users": peak_users,
            "peak_rps": peak_rps, "duration": dur, "tested": tested, "psu_peak": psu_peak}


# ---------------------------------------------------------------- render ----

CSS = """
:root{--accent:%(accent)s;--ink:#1d232b;--muted:#5c6773;--rule:#d9dee4;--soft:#f3f5f8;
--pass:#1e6b47;--pass-bg:#e6f2eb;--fail:#a4251c;--fail-bg:#fbeceb;--warn:#8a5a00;color-scheme:light}
*{box-sizing:border-box}
html{background:#fff}
body{margin:0;color:var(--ink);font:10.5pt/1.55 Charter,"Bitstream Charter","Iowan Old Style",Georgia,serif;
font-variant-numeric:tabular-nums lining-nums}
.page{max-width:820px;margin:0 auto;padding:48px 40px}
.cover{min-height:96vh;display:flex;flex-direction:column;justify-content:space-between;
border-top:10px solid var(--accent);padding-top:56px}
.cover img{max-height:72px;max-width:260px;margin-bottom:48px}
.cover .bank{font:600 12pt/1.3 Inter,"Segoe UI",Arial,sans-serif;color:var(--accent);margin:0 0 18px}
.cover h1{font-size:27pt;line-height:1.15;font-weight:600;margin:0 0 18px;max-width:20ch}
.cover .lede{font-size:12.5pt;color:var(--muted);max-width:52ch;margin:0}
.cover dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 24px;margin:0;
font:9.5pt/1.4 Inter,"Segoe UI",Arial,sans-serif}
.cover dt{color:var(--muted)} .cover dd{margin:0}
.classification{font:600 9pt Inter,"Segoe UI",Arial,sans-serif;color:var(--fail);margin-top:24px}
h2{font-size:16pt;font-weight:600;color:var(--accent);margin:40px 0 10px;break-after:avoid}
h3{font-size:12pt;font-weight:600;margin:24px 0 6px;break-after:avoid}
p{margin:0 0 10px;max-width:76ch}
ol.alpha{list-style:lower-alpha;margin:0 0 12px 1.2em;padding-left:1em;max-width:72ch}
ol.alpha li{margin-bottom:3px}
table{border-collapse:collapse;width:100%%;margin:6px 0 16px;
font:8.6pt/1.35 Inter,"Segoe UI",Arial,sans-serif;break-inside:auto}
tr{break-inside:avoid}
th,td{padding:5px 7px;border-bottom:1px solid var(--rule);text-align:right;vertical-align:top}
th{background:var(--soft);font-weight:600;color:var(--ink)}
th.l,td.l{text-align:left}
tr.total td{font-weight:700;border-top:1.5px solid var(--ink)}
table.kv td:first-child{color:var(--muted);text-align:left;width:46%%}
table.kv td{text-align:left}
.verdict{padding:16px 20px;margin:14px 0 18px;border-radius:4px;break-inside:avoid}
.verdict.pass{background:var(--pass-bg)} .verdict.fail{background:var(--fail-bg)}
.verdict b{display:block;font-size:14pt;margin-bottom:4px}
.verdict.pass b{color:var(--pass)} .verdict.fail b{color:var(--fail)}
.verdict p{margin:0}
.st{font-weight:600;white-space:nowrap}
.st.ok{color:var(--pass)} .st.no{color:var(--fail)} .st.part{color:var(--warn)}
.chart{width:100%%;height:auto;display:block;margin:4px 0 14px;break-inside:avoid}
figure{margin:0 0 8px;break-inside:avoid} figcaption{font:9pt Inter,"Segoe UI",Arial,sans-serif;color:var(--muted)}
.toc{list-style:none;padding:0;margin:0 0 8px;font-size:11pt}
.toc li{margin:3px 0} .toc a{color:var(--ink);text-decoration:none}
.toc .sub{padding-left:1.4em;color:var(--muted)}
.note{font-size:9.5pt;color:var(--muted)}
.sig td{height:42px}
.break{break-before:page}
@media screen{.page+.page{border-top:1px dashed var(--rule)}}
@media print{.page{padding:0;max-width:none}.cover{min-height:245mm;break-after:page}.page+.page{break-before:page}.page+.page+.page{break-before:page}}
@page{size:A4;margin:20mm 18mm 20mm 18mm}
"""


def st_class(s):
    return {"Demonstrated": "ok", "Within target": "ok", "Pass": "ok", "Met": "ok"}.get(
        s, "part" if s.startswith(("Partially", "Evidence")) else "no")


def kv_table(pairs):
    return "<table class='kv'>" + "".join(
        f"<tr><td>{e(k)}</td><td>{e(str(v))}</td></tr>" for k, v in pairs if v not in (None, "")) + "</table>"


def logo_tag(path):
    if not path or not os.path.exists(path):
        return ""
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        return f"<img alt='' src='data:{mime};base64,{base64.b64encode(f.read()).decode()}'>"


def build(cfg, prefix, eps, total, hist, failures, bp, sl, a):
    bank = get(cfg, "aspsp.name", "The ASPSP")
    title = get(cfg, "document.title", "Stress test results of the PSD2 dedicated interface")
    test_date = fmt_date(get(cfg, "test.date")) or (
        datetime.fromtimestamp(hist[0]["ts"], tz=timezone.utc).strftime("%-d %B %Y") if hist else "")
    out = []

    # Cover
    out.append(f"""<section class="page cover"><div>{logo_tag(get(cfg, 'aspsp.logo'))}
<p class="bank">{e(bank)}</p><h1>{e(title)}</h1>
<p class="lede">Overview of the stress testing procedure and results, prepared in support of an application for
exemption from the contingency mechanism under Article 33(6) of Commission Delegated Regulation (EU) 2018/389.</p></div>
<div><dl>
<dt>Competent authority</dt><dd>{e(get(cfg, 'aspsp.competent_authority', ''))}</dd>
<dt>Interface</dt><dd>{e(get(cfg, 'test.interface', 'PSD2 dedicated interface'))}</dd>
<dt>Test date</dt><dd>{e(test_date)}</dd>
<dt>Document version</dt><dd>{e(str(get(cfg, 'document.version', '1.0')))}</dd>
<dt>Issued</dt><dd>{e(fmt_date(get(cfg, 'document.date')) or datetime.now().strftime('%-d %B %Y'))}</dd>
</dl><p class="classification">{e(get(cfg, 'document.classification', 'Confidential'))}</p></div></section>""")

    # Document control and contents
    sections = ["Referenced documents", "Summary", "Stress testing approach", "Stress testing environment",
                "Environment monitoring", "Results", "Service level comparison",
                "Compliance with EBA Guideline 4.2", "Issues discovered", "Resource behaviour and memory leak",
                "Conclusion", "Approval"]
    toc = "".join(f"<li><a href='#s{i}'>{i}. {e(s)}</a></li>" for i, s in enumerate(sections, 1))
    dc = get(cfg, "document", {})
    out.append(f"""<section class="page"><h2>Document control</h2>{kv_table([
        ('Prepared by', dc.get('prepared_by')), ('Reviewed by', dc.get('reviewed_by')),
        ('Approved by', dc.get('approved_by')), ('Test performed by', get(cfg, 'test.performed_by')),
        ('Distribution', dc.get('distribution'))])}
<h2>Contents</h2><ul class="toc">{toc}</ul></section>""")

    body = []
    H = lambda i: f"<h2 id='s{i}'>{i}. {e(sections[i - 1])}</h2>"

    # 1 Referenced documents
    refs = get(cfg, "document.referenced_documents", []) + [
        f"Raw results: {os.path.basename(prefix)}_stats.csv, _stats_history.csv, _failures.csv",
        "Commission Delegated Regulation (EU) 2018/389 (RTS on SCA & CSC), Articles 32 and 33",
        "EBA Guidelines on the conditions to benefit from an exemption from the contingency mechanism "
        "under Article 33(6) of Regulation (EU) 2018/389 (EBA/GL/2018/07)"]
    body.append(H(1) + "<ol class='alpha'>" + "".join(f"<li>{e(r)}</li>" for r in refs) + "</ol>")

    # 2 Summary
    verdict = ("The dedicated interface met the stress testing requirements", "pass") if a["passed"] else \
              ("The stress test did not yet demonstrate every requirement", "fail")
    psu = a["psu_peak"]
    load_line = (f" The test sustained {a['peak_users']:,.0f} concurrent users, {a['peak_users'] / psu:.1f}× the "
                 f"current peak of {psu:,} concurrent users on the PSU interface." if psu else "")
    body.append(H(2) + f"""<p>In accordance with Article 32(2) of the RTS on SCA &amp; CSC and EBA Guideline 4, {e(bank)}
stress tested its dedicated interface on {e(test_date)} to assess the impact of an extremely high number of requests on
the availability and performance of the interface and on its defined service level targets.</p>
<div class="verdict {verdict[1]}"><b>{verdict[0]}</b><p>{total['count']:,} requests over
{rpt.fmt_duration(a['duration'])} with {100 - total['err_pct']:.2f}% success, an average response time of
{rpt.fmt_ms(total['avg'])} and 95% of responses within {rpt.fmt_ms(total['p95'])}.{e(load_line)}</p></div>""")

    # 3 Approach
    lm = get(cfg, "test.load_model", {})
    scen = get(cfg, "test.scenarios", [])
    tpps = get(cfg, "test.tpps", [])
    body.append(H(3) + f"""<p>The stress test was performed by {e(get(cfg, 'test.performed_by', 'the test team'))}
following an established procedure. In line with the EBA guide on the exemption conditions, it covered:</p>
<ol class="alpha"><li>access by multiple PISPs, CBPIIs and AISPs;</li>
<li>an extremely high number of requests from TPPs in a short period without failing;</li>
<li>an extremely high number of concurrent sessions for payment initiation, confirmation of funds and account information;</li>
<li>requests for large volumes of data.</li></ol>
<h3>Load basis</h3><p>{e(lm.get('basis', 'The basis for the load model was the peak number of concurrent users of the PSU interface.'))}</p>
{kv_table([('Peak concurrent users, PSU interface', f"{psu:,}" if psu else None),
           ('Concurrent users, stress test', f"{lm.get('test_concurrent', a['peak_users']):,.0f}"),
           ('Load relative to PSU-interface peak', f"{a['peak_users'] / psu:.1f}× ({(a['peak_users'] / psu - 1) * 100:.0f}% above)" if psu else None),
           ('Ramp-up', lm.get('ramp_up')), ('Test standard', get(cfg, 'test.standard')),
           ('Tool', get(cfg, 'test.tool', 'Locust'))])}""")
    if scen:
        body.append("<h3>Scenarios</h3><table><tr><th class='l'>Scenario</th><th class='l'>Description</th></tr>" +
                    "".join(f"<tr><td class='l'>{e(s['name'])}</td><td class='l'>{e(s.get('description', ''))}</td></tr>"
                            for s in scen) + "</table>")
    if tpps:
        body.append("<h3>TPPs and services</h3><table><tr><th class='l'>TPP identity</th><th class='l'>Roles</th>"
                    "<th class='l'>Notes</th></tr>" +
                    "".join(f"<tr><td class='l'>{e(t['name'])}</td><td class='l'>{e(', '.join(t.get('roles', [])))}</td>"
                            f"<td class='l'>{e(t.get('notes', ''))}</td></tr>" for t in tpps) + "</table>")

    # 4 Environment
    env = get(cfg, "test.environment", {})
    body.append(H(4) + f"<p>{e(env.get('description', ''))}</p>" + kv_table([
        ("Environment", env.get("name")), ("Differences from production", env.get("differences_from_production")),
        ("Load generator", env.get("load_generator"))]))

    # 5 Monitoring
    mon = get(cfg, "test.monitoring", {})
    body.append(H(5) + f"<p>{e(mon.get('description', 'During the test the environment was monitored for availability, latency, errors and resource usage.'))}</p>"
                + kv_table([("Tools", ", ".join(mon.get("tools", []))), ("Monitored by", mon.get("observed_by"))]))

    # 6 Results
    agg = kv_table([("Total test duration", rpt.fmt_duration(a["duration"])),
                    ("Total requests sent", f"{total['count']:,}"),
                    ("Successful", f"{100 - total['err_pct']:.2f}%"), ("Errors", f"{total['err_pct']:.2f}%"),
                    ("Peak concurrent users", f"{a['peak_users']:,.0f}"),
                    ("Peak throughput", f"{a['peak_rps']:,.1f} requests/s"),
                    ("Average throughput", f"{total['rps']:,.1f} requests/s"),
                    ("Average response time", rpt.fmt_ms(total["avg"])),
                    ("Median / 95th / 99th percentile", f"{rpt.fmt_ms(total['p50'])} / {rpt.fmt_ms(total['p95'])} / {rpt.fmt_ms(total['p99'])}"),
                    ("Apdex (whole test)", f"{apdex(total, sl['apdex_t_ms'], sl['apdex_f_ms']):.3f} ({apdex_rating(apdex(total, sl['apdex_t_ms'], sl['apdex_f_ms']))})")])
    charts = ""
    if len(hist) > 1:
        charts = (f"<h3>Load profile</h3><figure>{rpt.chart_throughput(hist, bp)}<figcaption>Concurrent users and requests per second.</figcaption></figure>"
                  f"<figure>{rpt.chart_latency(hist, bp, sl['p95_ms'])}<figcaption>Response time percentiles against the 95th-percentile target.</figcaption></figure>"
                  f"<figure>{rpt.chart_errors(hist, bp, sl['max_error_pct'])}<figcaption>Error rate during the test.</figcaption></figure>")
    T, F = sl["apdex_t_ms"], sl["apdex_f_ms"]
    rows = "".join(
        f"<tr><td class='l'>{e(rpt.label(x))}</td><td class='l'>{x['service']}</td><td>{x['count']:,}</td><td>{x['fails']:,}</td>"
        f"<td>{x['err_pct']:.2f}%</td><td>{(x['avg'] or 0):,.0f}</td><td>{(x['min'] or 0):,.0f}</td><td>{(x['max'] or 0):,.0f}</td>"
        f"<td>{(x['p50'] or 0):,.0f}</td><td>{(x['p90'] or 0):,.0f}</td><td>{(x['p95'] or 0):,.0f}</td><td>{(x['p99'] or 0):,.0f}</td>"
        f"<td>{x['rps']:,.2f}</td><td>{apdex(x, T, F):.3f}</td></tr>"
        for x in sorted(eps, key=lambda x: (x["service"], rpt.label(x))))
    rows += (f"<tr class='total'><td class='l'>Total</td><td></td><td>{total['count']:,}</td><td>{total['fails']:,}</td>"
             f"<td>{total['err_pct']:.2f}%</td><td>{(total['avg'] or 0):,.0f}</td><td>{(total['min'] or 0):,.0f}</td>"
             f"<td>{(total['max'] or 0):,.0f}</td><td>{(total['p50'] or 0):,.0f}</td><td>{(total['p90'] or 0):,.0f}</td>"
             f"<td>{(total['p95'] or 0):,.0f}</td><td>{(total['p99'] or 0):,.0f}</td><td>{total['rps']:,.2f}</td>"
             f"<td>{apdex(total, T, F):.3f}</td></tr>")
    stats = ("<h3>Statistics per endpoint</h3><p class='note'>Response times in milliseconds. Apdex uses a "
             f"satisfied threshold of {T:,.0f} ms and a frustrated threshold of {F:,.0f} ms, estimated from the "
             "recorded percentiles.</p><table><tr><th class='l'>Endpoint</th><th class='l'>Service</th><th>Samples</th>"
             "<th>Fail</th><th>Error</th><th>Avg</th><th>Min</th><th>Max</th><th>Median</th><th>90th</th><th>95th</th>"
             f"<th>99th</th><th>Req/s</th><th>Apdex</th></tr>{rows}</table>")
    if failures:
        errs = ("<h3>Errors</h3><table><tr><th class='l'>Endpoint</th><th class='l'>Error</th><th>Count</th><th>Share of all requests</th></tr>"
                + "".join(f"<tr><td class='l'>{e(rpt.label(f))}</td><td class='l'>{e((f.get('Error') or '')[:300])}</td>"
                          f"<td>{int(rpt.num(f.get('Occurrences')) or 0):,}</td>"
                          f"<td>{100 * (rpt.num(f.get('Occurrences')) or 0) / max(total['count'], 1):.3f}%</td></tr>"
                          for f in failures[:10]) + "</table>")
    else:
        errs = "<h3>Errors</h3><p>No errors were recorded during the test.</p>"
    body.append(H(6) + "<h3>Aggregated indicators</h3>" + agg + charts + stats + errs)

    # 7 Service level comparison
    p = get(cfg, "service_levels.psu_interface", {})
    succ = 100 - total["err_pct"]

    def cmp_row(label, target, measured, psu_val, ok, fmt):
        return (f"<tr><td class='l'>{label}</td><td>{fmt(target) if target is not None else '–'}</td>"
                f"<td>{fmt(measured)}</td><td>{fmt(psu_val) if psu_val is not None else '–'}</td>"
                f"<td class='l'><span class='st {'ok' if ok else 'no'}'>{'Met' if ok else 'Not met'}</span></td></tr>")
    ms = rpt.fmt_ms
    pc = lambda v: f"{v:.2f}%"
    avg_t = get(cfg, "service_levels.avg_response_ms")
    cmp_rows = [
        cmp_row("Average response time", avg_t, total["avg"], p.get("avg_response_ms"),
                (avg_t is None or total["avg"] <= avg_t) and (p.get("avg_response_ms") is None or total["avg"] <= p["avg_response_ms"]), ms),
        cmp_row("95th percentile response time", sl["p95_ms"], total["p95"], p.get("p95_ms"),
                total["p95"] <= sl["p95_ms"] and (p.get("p95_ms") is None or total["p95"] <= p["p95_ms"]), ms),
        cmp_row("Successful responses", 100 - sl["max_error_pct"], succ, p.get("success_pct"),
                succ >= 100 - sl["max_error_pct"] and (p.get("success_pct") is None or succ >= p["success_pct"]), pc)]
    body.append(H(7) + f"""<p>Article 32(2) of the RTS requires the dedicated interface to offer at all times the same level of
availability and performance as the interfaces available to PSUs. The table compares the dedicated interface under stress
with {e(bank)}'s service level targets and with the PSU interface under normal load
{('(' + e(p['source']) + ')') if p.get('source') else ''}. Comparing stressed results with normal PSU-interface load is a conservative test.</p>
<table><tr><th class='l'>Indicator</th><th>Target</th><th>Dedicated interface under stress</th><th>PSU interface</th><th class='l'>Result</th></tr>
{''.join(cmp_rows)}</table>""")

    # 8 GL 4.2 matrix
    body.append(H(8) + "<table><tr><th class='l'>Guideline</th><th class='l'>Requirement</th><th class='l'>Evidence from this test</th>"
                "<th class='l'>Status</th></tr>" + "".join(
                    f"<tr><td class='l'>{r['ref']}</td><td class='l'>{e(r['req'])}</td><td class='l'>{e(r['evidence'])}</td>"
                    f"<td class='l'><span class='st {st_class(r['status'])}'>{r['status']}</span></td></tr>" for r in a["rows"]) + "</table>")

    # 9 Issues
    issues = get(cfg, "test.issues", [])
    body.append(H(9) + ("<table><tr><th class='l'>ID</th><th class='l'>Severity</th><th class='l'>Description</th>"
                        "<th class='l'>Resolution</th><th class='l'>Status</th></tr>" + "".join(
                            f"<tr><td class='l'>{e(str(i.get('id', '')))}</td><td class='l'>{e(i.get('severity', ''))}</td>"
                            f"<td class='l'>{e(i.get('description', ''))}</td><td class='l'>{e(i.get('resolution', ''))}</td>"
                            f"<td class='l'>{e(i.get('status', ''))}</td></tr>" for i in issues) + "</table>"
                        if issues else "<p>None.</p>"))

    # 10 Resources
    res = get(cfg, "test.resource_observations", {})
    body.append(H(10) + kv_table([("Memory leak", res.get("memory_leak", "Not assessed")),
                                  ("Peak CPU", res.get("cpu_peak")), ("Peak memory", res.get("memory_peak")),
                                  ("Database and core banking", res.get("backend")),
                                  ("Other observations", res.get("other"))]))

    # 11 Conclusion
    if a["passed"]:
        concl = (f"<p>The stress testing results are accepted as successful. The dedicated interface of {e(bank)} handled "
                 "every requirement in EBA Guideline 4.2(a) to (d) within its service level targets, and its performance "
                 "under stress was at least equal to that of the PSU interface.</p>")
    else:
        gaps = [f"{r['ref']}: {r['status'].lower()}" for r in a["rows"] if r["status"] != "Demonstrated"]
        if not a["within_sla"]:
            gaps.append("overall latency or error rate outside the service level targets")
        concl = ("<p>The test does not yet provide sufficient evidence for an exemption application. The following "
                 "need to be addressed and the test repeated:</p><ol class='alpha'>" +
                 "".join(f"<li>{e(g)}</li>" for g in gaps) + "</ol>")
    body.append(H(11) + concl)

    # 12 Approval
    signers = get(cfg, "document.sign_off", [{"role": "Head of Open Banking"}, {"role": "Chief Technology Officer"}])
    body.append(H(12) + "<table class='sig'><tr><th class='l'>Role</th><th class='l'>Name</th><th class='l'>Signature</th>"
                "<th class='l'>Date</th></tr>" + "".join(
                    f"<tr><td class='l'>{e(s.get('role', ''))}</td><td class='l'>{e(s.get('name', ''))}</td><td></td><td></td></tr>"
                    for s in signers) + "</table>")

    out.append("<section class='page'>" + "".join(body) + "</section>")
    brand_navy = getattr(rpt, "BRAND", {}).get("navy", "#1f4e79")
    css = CSS % {"accent": get(cfg, "aspsp.accent", brand_navy)}
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'><title>{e(bank)} – {e(title)}</title>"
            f"<style>{css}</style></head><body>{''.join(out)}</body></html>")


def to_pdf(html_path, pdf_path, footer):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("PDF skipped: pip install playwright && playwright install chromium "
              "(or open the HTML and print to PDF).")
        return False
    tpl = ("<div style='font:7.5pt Arial,sans-serif;color:#5c6773;width:100%;padding:0 18mm;"
           "display:flex;justify-content:space-between'><span>{}</span>"
           "<span>Page <span class='pageNumber'></span> of <span class='totalPages'></span></span></div>")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto("file://" + os.path.abspath(html_path))
        page.pdf(path=pdf_path, format="A4", print_background=True, display_header_footer=True,
                 header_template="<span></span>", footer_template=tpl.format(html.escape(footer)),
                 margin={"top": "18mm", "bottom": "20mm", "left": "18mm", "right": "18mm"})
        browser.close()
    return True


def main():
    ap = argparse.ArgumentParser(description="Stress test report for a PSD2 fallback exemption application.")
    ap.add_argument("prefix", help="value passed to locust --csv, e.g. results/run1")
    ap.add_argument("--config", required=True, help="YAML with bank, test and sign-off details")
    ap.add_argument("--out", help="output HTML path (default <prefix>_exemption_report.html)")
    ap.add_argument("--pdf", action="store_true", help="also write a PDF (needs Playwright + Chromium)")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    sl = {"p95_ms": float(get(cfg, "service_levels.p95_ms", 1000)),
          "max_error_pct": float(get(cfg, "service_levels.max_error_pct", 1.0)),
          "apdex_t_ms": float(get(cfg, "service_levels.apdex_t_ms", 500)),
          "apdex_f_ms": float(get(cfg, "service_levels.apdex_f_ms", 1500))}

    eps, total = load_endpoints(args.prefix)
    hist = rpt.load_history(args.prefix)
    failures = sorted(rpt.read_csv(f"{args.prefix}_failures.csv"),
                      key=lambda x: rpt.num(x.get("Occurrences")) or 0, reverse=True)
    bp, _ = rpt.find_breaking_point(hist, sl["p95_ms"], sl["max_error_pct"],
                                    int(get(cfg, "service_levels.sustain_seconds", 30)),
                                    int(get(cfg, "service_levels.warmup_seconds", 60)))
    a = assess(cfg, eps, total, hist, bp, sl)

    out = args.out or f"{args.prefix}_exemption_report.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(build(cfg, args.prefix, eps, total, hist, failures, bp, sl, a))
    print(f"HTML: {out}")
    if args.pdf:
        pdf = os.path.splitext(out)[0] + ".pdf"
        footer = f"{get(cfg, 'aspsp.name', '')} – {get(cfg, 'document.title', 'Stress test results')} – " \
                 f"{get(cfg, 'document.classification', 'Confidential')}"
        if to_pdf(out, pdf, footer):
            print(f"PDF:  {pdf}")
    for r in a["rows"]:
        print(f"  GL {r['ref']}: {r['status']}")
    print("Verdict:", "requirements met" if a["passed"] else "gaps remain - see Conclusion")
    sys.exit(0 if a["passed"] else 1)


if __name__ == "__main__":
    main()
