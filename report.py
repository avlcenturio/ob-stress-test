#!/usr/bin/env python3
"""
Build a stress-test report from Locust CSV output.

Run Locust with --csv, e.g.
    locust -f locustfile.py --headless -u 300 -r 5 -t 30m --csv results/run1
then
    python report.py results/run1 --env "Sandbox" --p95-ms 1000 --max-error-pct 1

Reads   <prefix>_stats.csv, <prefix>_stats_history.csv,
        <prefix>_failures.csv, <prefix>_exceptions.csv
Writes  <prefix>_report.html (self-contained) and <prefix>_summary.md
"""
import argparse
import csv
import html
import io
import os
import re
import sys
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# ------------------------------------------------------------- parsing ----

def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_stats(prefix):
    rows = read_csv(f"{prefix}_stats.csv")
    if not rows:
        sys.exit(f"Missing or empty {prefix}_stats.csv - did you run Locust with --csv {prefix}?")
    endpoints, total = [], None
    for r in rows:
        count = int(num(r.get("Request Count")) or 0)
        fails = int(num(r.get("Failure Count")) or 0)
        e = {
            "method": r.get("Type", ""), "name": r.get("Name", ""),
            "count": count, "fails": fails,
            "err_pct": 100.0 * fails / count if count else 0.0,
            "avg": num(r.get("Average Response Time")), "min": num(r.get("Min Response Time")),
            "max": num(r.get("Max Response Time")), "rps": num(r.get("Requests/s")),
            "p50": num(r.get("50%")), "p90": num(r.get("90%")),
            "p95": num(r.get("95%")), "p99": num(r.get("99%")),
        }
        if e["name"] == "Aggregated":
            total = e
        else:
            endpoints.append(e)
    return endpoints, total


def load_history(prefix):
    hist = []
    for r in read_csv(f"{prefix}_stats_history.csv"):
        if r.get("Name") != "Aggregated":
            continue
        ts = num(r.get("Timestamp"))
        if ts is None:
            continue
        rps, fps = num(r.get("Requests/s")) or 0.0, num(r.get("Failures/s")) or 0.0
        hist.append({
            "ts": ts, "users": num(r.get("User Count")) or 0, "rps": rps, "fps": fps,
            "err_pct": 100.0 * fps / rps if rps else 0.0,
            "p50": num(r.get("50%")), "p95": num(r.get("95%")), "p99": num(r.get("99%")),
        })
    hist.sort(key=lambda h: h["ts"])
    return hist


# ------------------------------------------------------------- analysis ----

def find_breaking_point(hist, p95_slo, err_slo, sustain_s=30, warmup_s=60, key="p95"):
    """First moment the SLO is breached continuously for `sustain_s` seconds,
    ignoring the first `warmup_s` seconds (connection set-up, cold caches)."""
    if not hist:
        return None, []
    start_after = hist[0]["ts"] + warmup_s
    streak_start, reasons_seen = None, set()
    for h in hist:
        if h["ts"] < start_after or h["rps"] <= 0:
            streak_start, reasons_seen = None, set()
            continue
        reasons = set()
        if h[key] is not None and h[key] > p95_slo:
            reasons.add("latency")
        if h["err_pct"] > err_slo:
            reasons.add("errors")
        if reasons:
            if streak_start is None:
                streak_start = h
            reasons_seen |= reasons
            if h["ts"] - streak_start["ts"] >= sustain_s:
                return streak_start, sorted(reasons_seen)
        else:
            streak_start, reasons_seen = None, set()
    return None, []


def endpoint_ok(e, args):
    ok = (e["p95"] or 0) <= args.p95_ms and e["err_pct"] <= args.max_error_pct
    if args.p99_ms:
        ok = ok and (e["p99"] or 0) <= args.p99_ms
    return ok


def observations(endpoints, total, hist, args, bp):
    notes = []
    busy = [e for e in endpoints if e["count"] and e["method"] not in ("AUTH", "CONFIG")
            and not e["name"].endswith("(token expired)")]
    if busy:
        slow = max(busy, key=lambda e: e["p95"] or 0)
        notes.append(f"Slowest endpoint was {label(slow)} "
                     f"with a p95 of {fmt_ms(slow['p95'])}.")
        worst = max(busy, key=lambda e: e["err_pct"])
        if worst["fails"]:
            notes.append(f"Most failures came from {label(worst)}: "
                         f"{worst['fails']:,} of {worst['count']:,} requests ({worst['err_pct']:.2f}%).")
        token = [e for e in busy if "/token" in e["name"]]
        if token and (token[0]["p95"] or 0) > args.p95_ms:
            notes.append("The token endpoint was over the latency target, so the authorization "
                         "server is a likely bottleneck, not just the resource server.")
    if hist:
        peak = max(hist, key=lambda h: h["rps"])
        max_users = max(h["users"] for h in hist)
        later = [h for h in hist if h["ts"] > peak["ts"] and h["users"] > peak["users"]]
        if later and max(h["rps"] for h in later) < peak["rps"] * 1.05:
            notes.append(f"Throughput levelled off at about {peak['rps']:.0f} requests/s with "
                         f"{peak['users']:.0f} users. Adding users up to {max_users:.0f} did not "
                         "increase it, which points to saturation.")
    if bp:
        notes.append(f"The breaking point was reached at {bp['users']:.0f} concurrent users. "
                     "Treat roughly 70% of that as safe sustained capacity.")
    if total and total["max"] and total["p99"] and total["max"] > 10 * total["p99"]:
        notes.append(f"Maximum response time ({fmt_ms(total['max'])}) was over 10× the p99, "
                     "so a few outliers (timeouts or retries) are worth checking in server logs "
                     "via x-fapi-interaction-id.")
    return notes


# ------------------------------------------------------------- charts ----

try:  # brand colours (brand/brand.json), if the brand module is next to this script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ob_brand
    BRAND = ob_brand.load()
    BRAND_LOGO = ob_brand.logo_data_uri(BRAND, on_dark=True)  # the report header bar is navy
except Exception:  # noqa: BLE001
    BRAND, BRAND_LOGO = {"name": "", "product": "", "navy": "#0B1E3F", "accent": "#3D5AFE",
                         "accent_on_dark": "#8C9EFF", "font": "system-ui, sans-serif"}, None

INK, GRID = "#7d8893", "#7d889333"
C_P50, C_P95, C_P99 = BRAND.get("orange", "#9db0d8"), BRAND["accent"], BRAND.get("coral", "#8c6bc2")
C_RPS, C_USERS, C_BAD = BRAND["accent"], "#9aa4ae", "#c8463d"

plt.rcParams.update({
    "svg.fonttype": "none", "font.family": "sans-serif", "font.size": 10,
    "text.color": INK, "axes.labelcolor": INK, "xtick.color": INK, "ytick.color": INK,
    "axes.edgecolor": GRID, "axes.grid": True, "grid.color": GRID,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
})


def to_svg(fig):
    buf = io.StringIO()
    fig.savefig(buf, format="svg", transparent=True, bbox_inches="tight")
    plt.close(fig)
    svg = buf.getvalue()
    svg = svg[svg.index("<svg"):]
    svg = re.sub(r'(<svg[^>]*?)\s(width|height)="[^"]*"', r"\1", svg, count=1)
    svg = re.sub(r'(<svg[^>]*?)\s(width|height)="[^"]*"', r"\1", svg, count=1)
    return svg.replace("<svg", '<svg role="img" class="chart"', 1)


def elapsed_min(hist):
    t0 = hist[0]["ts"]
    return [(h["ts"] - t0) / 60 for h in hist]


def mark_bp(ax, hist, bp):
    if bp:
        x = (bp["ts"] - hist[0]["ts"]) / 60
        ax.axvline(x, color=C_BAD, lw=1, ls=":")
        ax.annotate("breaking point", (x, 0), xycoords=("data", "axes fraction"),
                    xytext=(4, 4), textcoords="offset points", color=C_BAD, fontsize=9)


def chart_throughput(hist, bp):
    x = elapsed_min(hist)
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.plot(x, [h["rps"] for h in hist], color=C_RPS, lw=1.8, label="Requests/s")
    ax.set_ylabel("Requests/s")
    ax.set_xlabel("Minutes into test")
    ax2 = ax.twinx()
    ax2.plot(x, [h["users"] for h in hist], color=C_USERS, lw=1.2, ls="--", label="Users")
    ax2.set_ylabel("Concurrent users")
    ax2.grid(False)
    ax2.spines["right"].set_visible(True)
    mark_bp(ax, hist, bp)
    lines = ax.get_lines()[:1] + ax2.get_lines()
    ax.legend(lines, [l.get_label() for l in lines], loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2)
    return to_svg(fig)


def chart_latency(hist, bp, slo):
    x = elapsed_min(hist)
    fig, ax = plt.subplots(figsize=(9, 3.2))
    for key, color, label in (("p50", C_P50, "p50"), ("p95", C_P95, "p95"), ("p99", C_P99, "p99")):
        ax.plot(x, [h[key] for h in hist], color=color, lw=1.6, label=label)
    ax.axhline(slo, color=C_BAD, lw=1, ls="--", label=f"OBL benchmark ({slo:,.0f} ms, average)")
    mark_bp(ax, hist, bp)
    ax.set_ylabel("Response time (ms)")
    ax.set_xlabel("Minutes into test")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=4)
    return to_svg(fig)


def chart_errors(hist, bp, slo):
    x = elapsed_min(hist)
    fig, ax = plt.subplots(figsize=(9, 2.6))
    ax.fill_between(x, [h["err_pct"] for h in hist], color=C_BAD, alpha=0.25, lw=0)
    ax.plot(x, [h["err_pct"] for h in hist], color=C_BAD, lw=1.4)
    ax.axhline(slo, color=INK, lw=1, ls="--")
    ax.annotate(f"OBL benchmark {slo:g}% (5xx)", (0, slo), xytext=(2, 3), textcoords="offset points", fontsize=9)
    mark_bp(ax, hist, bp)
    ax.set_ylabel("Error rate (%)")
    ax.set_xlabel("Minutes into test")
    ax.set_ylim(bottom=0)
    return to_svg(fig)


def chart_endpoints(endpoints, slo):
    eps = sorted([e for e in endpoints if e["count"]], key=lambda e: e["avg"] or 0)
    fig, ax = plt.subplots(figsize=(9, 0.45 * len(eps) + 1))
    labels = [label(e) for e in eps]
    vals = [e["avg"] or 0 for e in eps]
    ax.barh(labels, vals, color=[C_BAD if v > slo else C_P95 for v in vals], height=0.6)
    ax.axvline(slo, color=C_BAD, lw=1, ls="--")
    ax.set_xlabel("Average response time (ms)")
    ax.grid(axis="y", visible=False)
    return to_svg(fig)


# ------------------------------------------------------------- output ----

def label(e):
    """Endpoint label; avoids 'GET GET /x' when the request name already has the method."""
    name, method = e.get("name") or e.get("Name", ""), e.get("method") or e.get("Method", "")
    return name if name.upper().startswith(method.upper() + " ") else f"{method} {name}".strip()


def fmt_ms(v):
    if v is None:
        return "–"
    return f"{v / 1000:.2f} s" if v >= 10000 else f"{v:,.0f} ms"


def fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def fmt_duration(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if h else f"{m} min {s} s"


# Open Banking Limited (OBL) Operational Guidelines v4.0.1 (March 2026), recommended benchmarks:
#   availability  quarterly uptime of 99.5%: not in the scope of a stress test (needs a dedicated, ~3-month report)
#   performance   average TTLB of 750 ms per response (AIS: per page of up to 100 records; PIS: per endpoint; CBPII: per response)
#   error rate    5xx responses / all requests, average 0.5% across all endpoints (4xx are attributable to the TPP)
OBL_REF = "OBL Operational Guidelines v4.0.1, Availability and performance"
NO_REPLY = re.compile(r"Timeout|timed out|ConnectionError|Connection aborted|RemoteDisconnected|ConnectionRefused|"
                      r"ConnectTimeout|ReadTimeout|Max retries", re.I)
STATUS = re.compile(r"(?<![0-9])([1-5][0-9][0-9])(?![0-9])\s*:")


def area_of(e):
    n = e["name"].lower()
    if "domestic-payment" in n:
        return "PIS"
    if "funds-confirmation" in n:
        return "CoF"
    return "AIS"


def is_api(e):
    return e["method"] not in ("AUTH", "CONFIG") and not e["name"].endswith(("(token expired)", "(token renewed)"))


def classify_failures(failures):
    """Per endpoint: number of 5xx responses and of requests that got no reply."""
    out = {}
    for f in failures:
        key = (f.get("Method", ""), f.get("Name", ""))
        n = int(num(f.get("Occurrences")) or 0)
        err = f.get("Error") or ""
        m = STATUS.search(err)
        c = out.setdefault(key, {"5xx": 0, "no_reply": 0, "4xx": 0})
        if m and m.group(1).startswith("5"):
            c["5xx"] += n
        elif m and m.group(1).startswith("4"):
            c["4xx"] += n
        elif NO_REPLY.search(err):
            c["no_reply"] += n
    return out


def obl_assessment(endpoints, total, hist, failures, args):
    kinds = classify_failures(failures)
    api = [e for e in endpoints if is_api(e) and e["count"]]
    for e in endpoints:
        k = kinds.get((e["method"], e["name"]), {"5xx": 0, "no_reply": 0, "4xx": 0})
        e["n5xx"], e["no_reply"] = k["5xx"], k["no_reply"]
        e["pct5xx"] = 100.0 * e["n5xx"] / e["count"] if e["count"] else 0.0
        e["area"] = area_of(e) if is_api(e) else ("Token" if e["method"] == "AUTH" else "Settings")
        e["ok"] = (not is_api(e)) or ((e["avg"] or 0) <= args.avg_ms and e["pct5xx"] <= args.max_5xx_pct)
    n5xx = sum(e["n5xx"] for e in api)
    no_reply = sum(e["no_reply"] for e in api)
    requests_ = sum(e["count"] for e in api) or 1
    pct5xx = 100.0 * n5xx / requests_
    slow = sorted([e for e in api if (e["avg"] or 0) > args.avg_ms], key=lambda e: -(e["avg"] or 0))
    worst = max(api, key=lambda e: e["avg"] or 0, default=None)
    checks = [
        {"name": "Availability", "target": f"{args.availability_pct:g}% uptime per quarter", "ok": None,
         "measured": "Not in the scope of a stress test"},
        {"name": "Performance", "target": f"average ≤ {args.avg_ms:,.0f} ms per endpoint response", "ok": not slow,
         "measured": (f"slowest endpoint averages {fmt_ms(worst['avg'])} ({label(worst)})" if worst else "no API calls")
         + (f"; {len(slow)} endpoint(s) over" if slow else "")},
        {"name": "Error rate", "target": f"≤ {args.max_5xx_pct:g}% 5xx responses", "ok": pct5xx <= args.max_5xx_pct,
         "measured": f"{pct5xx:.2f}% ({n5xx:,} of {requests_:,} requests)"
         + (f"; {no_reply:,} got no reply" if no_reply else "")},
    ]
    findings = []
    for e in slow:
        findings.append(f"Performance: {label(e)} averaged {fmt_ms(e['avg'])} against {args.avg_ms:,.0f} ms "
                        f"(p95 {fmt_ms(e['p95'])}, {e['count']:,} requests).")
    if not checks[2]["ok"]:
        for e in sorted([e for e in api if e["n5xx"]], key=lambda e: -e["n5xx"])[:5]:
            findings.append(f"Error rate: {label(e)} returned {e['n5xx']:,} 5xx responses ({e['pct5xx']:.2f}%).")
    return {"checks": checks, "findings": findings, "passed": all(c["ok"] for c in checks if c["ok"] is not None),
            "pct5xx": pct5xx, "worst": worst}


def verdict_text(assess, total, hist, bp, args):
    peak_users = max((h["users"] for h in hist), default=0)
    c = {x["name"]: x for x in assess["checks"]}
    if assess["passed"]:
        return ("Passed", f"Meets the Open Banking benchmarks for performance (every endpoint averages within "
                f"{args.avg_ms:,.0f} ms) and error rate "
                f"({assess['pct5xx']:.2f}% 5xx), with up to {peak_users:.0f} concurrent users.")
    missed = [x["name"].lower() for x in assess["checks"] if x["ok"] is False]
    listed = missed[0] if len(missed) == 1 else ", ".join(missed[:-1]) + " and " + missed[-1]
    return ("Failed", "Misses the Open Banking benchmark for " + listed +
            f", with up to {peak_users:.0f} concurrent users. See What we found.")


CSS = """
:root{--paper:#fbfcfd;--ink:#1b2430;--muted:#5b6672;--rule:#dfe4ea;--steel:%(accent)s;--navy:%(navy)s;
--pass:#2e7d5b;--pass-bg:#e7f3ed;--fail:#b3261e;--fail-bg:#fbeceb;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--paper:#161b22;--ink:#e3e8ee;--muted:#9aa5b1;
--rule:#2c3440;--steel:%(accent_dark)s;--pass:#6fcf9f;--pass-bg:#16301f;--fail:#f08c84;--fail-bg:#3a1a18;
color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.55 %(font)s;
font-variant-numeric:tabular-nums}
main{max-width:980px;margin:0 auto;padding:40px 24px 64px}
h1{font-size:1.5rem;font-weight:600;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 28px}
.verdict{padding:22px 26px;border-radius:6px;margin-bottom:32px}
.verdict.pass{background:var(--pass-bg)} .verdict.fail{background:var(--fail-bg)}
.verdict strong{display:block;font-size:2.1rem;font-weight:700;line-height:1.1;margin-bottom:6px}
.verdict.pass strong{color:var(--pass)} .verdict.fail strong{color:var(--fail)}
.verdict p{margin:0;font-size:1.1rem;max-width:68ch}
dl.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:18px 28px;
margin:0 0 40px}
dl.facts dt{color:var(--muted);font-size:.85rem} dl.facts dd{margin:2px 0 0;font-size:1.15rem;font-weight:600}
h2{font-size:1.15rem;font-weight:600;margin:44px 0 6px;padding-top:18px;border-top:1px solid var(--rule)}
h2+p.note{color:var(--muted);margin:0 0 12px;max-width:72ch}
.chart{width:100%%;height:auto;display:block}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%%;font-size:.92rem}
th,td{text-align:right;padding:8px 10px;border-bottom:1px solid var(--rule);white-space:nowrap}
th{color:var(--muted);font-weight:500}
th:first-child,td:first-child{text-align:left}
td.err{white-space:normal;text-align:left;max-width:520px;font-family:ui-monospace,Consolas,monospace;font-size:.82rem}
.ok{color:var(--pass);font-weight:600} .bad{color:var(--fail);font-weight:600} .na{color:var(--muted);font-weight:600}
ul.obs{padding-left:20px;max-width:75ch} ul.obs li{margin-bottom:8px}
footer{margin-top:48px;color:var(--muted);font-size:.85rem}
.brandbar{background:var(--navy);color:#fff;box-shadow:inset 0 -3px 0 var(--steel)}
.brandbar div{max-width:980px;margin:0 auto;padding:12px 24px;display:flex;align-items:center;gap:12px}
.brandbar img{height:24px;display:block} .brandbar b{font-size:16px;letter-spacing:.2px;margin-left:-2px}
.brandbar span{opacity:.8;font-size:13px;padding-left:12px;border-left:1px solid rgba(255,255,255,.3)}
.titlebar{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
button.pdf{font:600 13.5px/1 %(font)s;height:36px;padding:0 16px;border-radius:8px;border:1px solid var(--steel);
background:var(--steel);color:#fff;cursor:pointer;flex:none;margin-top:4px}
button.pdf:hover{filter:brightness(1.08)}
.details{border:1px solid var(--rule);border-radius:10px;padding:14px 18px;margin:0 0 24px}
dl.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px 24px;margin:0 0 12px}
dl.meta dt{color:var(--muted);font-size:.8rem} dl.meta dd{margin:2px 0 0;font-weight:600}
table.bench{table-layout:fixed}
table.bench th,table.bench td{text-align:left!important;white-space:normal!important;vertical-align:top}
table.bench th:nth-child(1){width:18%%} table.bench th:nth-child(2){width:30%%} table.bench th:nth-child(4){width:14%%}
table.urls{width:100%%;border-collapse:collapse;font-size:.88rem;table-layout:fixed}
table.urls caption{text-align:left;color:var(--muted);font-size:.8rem;padding:4px 0 6px}
table.urls th{text-align:left!important;font-weight:500;color:var(--muted);width:24%%;padding:6px 12px 6px 0;vertical-align:top;border-top:1px solid var(--rule);white-space:normal!important}
table.urls td{text-align:left;white-space:normal!important;word-break:break-all;padding:6px 0;border-top:1px solid var(--rule);font-family:ui-monospace,Consolas,monospace;font-size:.82rem;overflow-wrap:anywhere}
table.urls a{color:inherit;text-decoration:none}
@media (max-width:640px){table.urls th{width:auto;display:block;border-top:1px solid var(--rule);padding-bottom:0}
table.urls td{display:block;border-top:0}}
@media print{button.pdf{display:none}.brandbar{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{background:#fff!important;color:#000}main{max-width:none;padding:0 4mm}
section,table,.verdict,svg{break-inside:avoid}h2{break-after:avoid}}
@page{size:A4;margin:14mm 12mm}
"""


CSS_VARS = {"accent": BRAND.get("accent_text", BRAND["accent"]), "accent_dark": BRAND.get("accent_on_dark", BRAND["accent"]),
            "navy": BRAND["navy"], "font": BRAND["font"]}
_mark = ((f'<img src="{BRAND_LOGO}" alt="">' if BRAND_LOGO else "") +
         (f'<b>{html.escape(BRAND["name"])}</b>' if not BRAND_LOGO or BRAND.get("show_name_with_logo", True) else "")
         ) if BRAND.get("name") else ""
BRAND_BAR = (f'<header class="brandbar"><div>{_mark}<span>{html.escape(BRAND.get("product", ""))}</span></div></header>'
             if _mark else "")


def details_html(ctx):
    """Tested-against block: tenant profile, test profile, standard and every URL from discovery."""
    rc = ctx.get("run_ctx") or {}
    if not rc:
        return ""
    e = html.escape
    meta = [("Tenant profile", rc.get("tenant")), ("Test profile", rc.get("profile")),
            ("Standard", rc.get("standard")), ("Areas tested", ", ".join(rc.get("areas") or []))]
    meta_html = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in meta if v)
    urls = "".join(f"<tr><th>{e(u['label'])}</th><td><a href=\"{e(u['url'])}\">{e(u['url'])}</a></td></tr>"
                   for u in rc.get("urls") or [])
    return (f"<section class=\"details\"><dl class=\"meta\">{meta_html}</dl>"
            + (f"<table class=\"urls\"><caption>Tenant profile URLs</caption><tbody>{urls}</tbody></table>" if urls else "")
            + "</section>")


PDF_JS = """
<script>
document.getElementById("pdfBtn").addEventListener("click", async function () {
  var run = this.dataset.run;
  if (run && location.protocol.indexOf("http") === 0) {
    try {   // the app renders the PDF when it can; otherwise the browser's own Save as PDF
      var r = await fetch("/ob-runs/" + run + "/report-pdf", {method: "HEAD"});
      if (r.ok) { location.href = "/ob-runs/" + run + "/report-pdf"; return; }
    } catch (e) {}
  }
  window.print();
});
</script>
"""


def build_html(ctx):
    e = html.escape
    run_id = str((ctx.get("run_ctx") or {}).get("run_id", ""))
    rows = "".join(
        f"<tr><td>{e(label(x))}</td><td>{e(x.get('area', ''))}</td><td>{x['count']:,}</td>"
        f"<td>{fmt_ms(x['avg'])}</td><td>{fmt_ms(x['p95'])}</td><td>{fmt_ms(x['max'])}</td>"
        f"<td>{x.get('n5xx', 0):,}</td><td>{x['err_pct']:.2f}%</td><td>{(x['rps'] or 0):.1f}</td>"
        f"<td class=\"{'ok' if x['ok'] else 'bad'}\">{('Pass' if x['ok'] else 'Fail') if is_api(x) else '–'}</td></tr>"
        for x in ctx["endpoints"])
    bench = "".join(
        f"<tr><td>{e(c['name'])}</td><td>{e(c['target'])}</td><td>{e(c['measured'])}</td>"
        f"<td class=\"{'na' if c['ok'] is None else 'ok' if c['ok'] else 'bad'}\">"
        f"{'Not in scope' if c['ok'] is None else 'Met' if c['ok'] else 'Not met'}</td></tr>" for c in ctx["checks"])
    fails = "".join(
        f"<tr><td>{e(label(f))}</td>"
        f"<td class=\"err\">{e((f.get('Error') or '')[:400])}</td><td>{int(num(f.get('Occurrences')) or 0):,}</td></tr>"
        for f in ctx["failures"])
    facts = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in ctx["facts"])
    obs = "".join(f"<li>{e(n)}</li>" for n in ctx["notes"])
    charts = ctx["charts"]
    status = "pass" if ctx["verdict"][0] == "Passed" else "fail"

    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(ctx['title'])}</title><style>{CSS % CSS_VARS}</style></head><body>{BRAND_BAR}<main>
<div class="titlebar"><div><h1>{e(ctx['title'])}</h1><p class="sub">{e(ctx['subtitle'])}</p></div>
<button class="pdf" type="button" id="pdfBtn" data-run="{e(run_id)}">Download PDF</button></div>
{details_html(ctx)}
<section class="verdict {status}"><strong>{ctx['verdict'][0]}</strong><p>{e(ctx['verdict'][1])}</p></section>
<dl class="facts">{facts}</dl>"""]
    parts.append(f"<h2>Open Banking benchmarks</h2><p class=\"note\">Recommended benchmarks from the {OBL_REF}. "
                 "Only 5xx responses count as errors, as 4xx are attributable to the TPP. Availability is not in the "
                 "scope of a stress test: it is measured over a longer period and should be reported in a dedicated "
                 "availability report covering at least three months (OBL measures it per quarter).</p>"
                 "<div class=\"scroll\"><table class=\"bench\"><thead><tr><th>Requirement</th><th>Benchmark</th><th>This run</th>"
                 f"<th>Result</th></tr></thead><tbody>{bench}</tbody></table></div>")
    if obs and ctx["verdict"][0] != "Passed":
        parts.append(f"<h2>What we found</h2><ul class=\"obs\">{obs}</ul>")
    if charts.get("latency"):
        parts.append(f"<h2>Throughput and users</h2>{charts['throughput']}")
        parts.append("<h2>Response time under load</h2><p class=\"note\">Rolling percentiles across all "
                     f"endpoints. The {ctx['args'].avg_ms:,.0f} ms line is the OBL benchmark, which applies to averages."
                     f"</p>{charts['latency']}")
        parts.append(f"<h2>Error rate</h2>{charts['errors']}")
    a = ctx["args"]
    parts.append(f"<h2>Endpoints</h2><p class=\"note\">Sorted by average response time. An API endpoint passes when "
                 f"its average is ≤ {a.avg_ms:,.0f} ms and 5xx responses are ≤ {a.max_5xx_pct:g}%. Token and settings "
                 "lines are shown for information only.</p>"
                 f"{charts['endpoints']}<div class=\"scroll\"><table><thead><tr><th>Endpoint</th><th>Area</th>"
                 "<th>Requests</th><th>Average</th><th>p95</th><th>Max</th><th>5xx</th><th>All errors</th><th>Req/s</th>"
                 f"<th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>")
    if fails:
        parts.append("<h2>Failures</h2><div class=\"scroll\"><table><thead><tr><th>Endpoint</th>"
                     f"<th>Error</th><th>Count</th></tr></thead><tbody>{fails}</tbody></table></div>")
    parts.append(f"<footer>Generated {e(fmt_time(datetime.now(timezone.utc).timestamp()))} from "
                 f"{e(os.path.basename(ctx['prefix']))}_*.csv</footer></main>{PDF_JS}</body></html>")
    return "".join(parts)


def build_md(ctx):
    a = ctx["args"]
    lines = [f"# {ctx['title']}", "", f"**{ctx['verdict'][0]}.** {ctx['verdict'][1]}", ""]
    lines += [f"- {k}: {v}" for k, v in ctx["facts"]] + [""]
    lines += [f"## Open Banking benchmarks ({OBL_REF})", "", "| Requirement | Benchmark | This run | Result |", "|---|---|---|---|"]
    lines += [f"| {c['name']} | {c['target']} | {c['measured']} | "
              f"{'➖ Not in scope' if c['ok'] is None else '✅ Met' if c['ok'] else '❌ Not met'} |" for c in ctx["checks"]]
    lines += ["", "Availability is not in the scope of a stress test: it is measured over a longer period and should be "
              "reported in a dedicated availability report covering at least three months (OBL measures it per quarter).", ""]
    if ctx["notes"] and ctx["verdict"][0] != "Passed":
        lines += ["## What we found", ""] + [f"- {n}" for n in ctx["notes"]] + [""]
    lines += [f"## Endpoints (average ≤ {a.avg_ms:,.0f} ms, 5xx ≤ {a.max_5xx_pct:g}%)", "",
              "| Endpoint | Area | Requests | Average | p95 | 5xx | Status |",
              "|---|---|---:|---:|---:|---:|---|"]
    lines += [f"| {label(x)} | {x.get('area', '')} | {x['count']:,} | {fmt_ms(x['avg'])} | {fmt_ms(x['p95'])} | "
              f"{x.get('n5xx', 0):,} | {('✅' if x['ok'] else '❌') if is_api(x) else '–'} |"
              for x in ctx["endpoints"]]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------- main ----

def main():
    p = argparse.ArgumentParser(description="Generate a stress-test report from Locust CSV output.")
    p.add_argument("prefix", help="the value you passed to locust --csv, e.g. results/run1")
    p.add_argument("--title", default="Open Banking API Stress Test")
    p.add_argument("--context", help="run details JSON (default: context.json next to the CSVs)")
    p.add_argument("--env", default=os.getenv("OB_ENV_NAME", ""), help="environment label, e.g. Sandbox")
    p.add_argument("--target", default=os.getenv("OB_BASE_URL", ""), help="API base URL tested")
    p.add_argument("--avg-ms", type=float, default=750, help="OBL benchmark: average response per endpoint (default 750 ms)")
    p.add_argument("--max-5xx-pct", type=float, default=0.5, help="OBL benchmark: 5xx error rate (default 0.5%%)")
    p.add_argument("--availability-pct", type=float, default=99.5, help="OBL benchmark: uptime (default 99.5%%)")
    p.add_argument("--sustain", type=int, default=30,
                   help="seconds the target must be missed continuously to call a breaking point (default 30)")
    p.add_argument("--warmup", type=int, default=60,
                   help="seconds at the start to ignore for the breaking point (default 60)")
    p.add_argument("--out", help="output HTML path (default <prefix>_report.html)")
    args = p.parse_args()

    endpoints, total = load_stats(args.prefix)
    hist = load_history(args.prefix)
    failures = read_csv(f"{args.prefix}_failures.csv")
    exceptions = read_csv(f"{args.prefix}_exceptions.csv")

    ctx_path = args.context or os.path.join(os.path.dirname(os.path.abspath(args.prefix)), "context.json")
    run_ctx = {}
    if os.path.exists(ctx_path):
        try:
            import json
            with open(ctx_path) as fh:
                run_ctx = json.load(fh)
        except ValueError:
            run_ctx = {}
    assess = obl_assessment(endpoints, total, hist, failures, args)
    endpoints.sort(key=lambda e: (not is_api(e), -(e["avg"] or 0)))
    failures.sort(key=lambda f: num(f.get("Occurrences")) or 0, reverse=True)
    # informational: load at which the median went over the benchmark or errors climbed, for 30 s or more
    bp, _ = find_breaking_point(hist, args.avg_ms, args.max_5xx_pct, args.sustain, args.warmup, key="p50")

    facts = []
    if hist:
        facts += [("Started", fmt_time(hist[0]["ts"])),
                  ("Duration", fmt_duration(hist[-1]["ts"] - hist[0]["ts"])),
                  ("Peak users", f"{max(h['users'] for h in hist):,.0f}"),
                  ("Peak throughput", f"{max(h['rps'] for h in hist):,.1f} req/s")]
    facts += [("Total requests", f"{total['count']:,}"),
              ("Average throughput", f"{(total['rps'] or 0):,.1f} req/s"),
              ("Average response", fmt_ms(total["avg"])),
              ("5xx error rate", f"{assess['pct5xx']:.2f}%")]
    if bp:
        facts.append(("Degradation point", f"{bp['users']:,.0f} users"))
    if exceptions:
        facts.append(("Script exceptions", f"{sum(int(num(x.get('Count')) or 0) for x in exceptions):,}"))

    charts = {"endpoints": chart_endpoints([e for e in endpoints if is_api(e)], args.avg_ms)}
    if len(hist) > 1:
        charts.update(latency=chart_latency(hist, bp, args.avg_ms),
                      throughput=chart_throughput(hist, bp),
                      errors=chart_errors(hist, bp, args.max_5xx_pct))
    else:
        print("Note: no usable _stats_history.csv, so time-series charts are skipped.")

    ctx = {
        "args": args, "prefix": args.prefix, "title": args.title,
        "subtitle": (" · ".join(x for x in (run_ctx.get("tenant"), run_ctx.get("profile")) if x) if run_ctx else
                     " – ".join(x for x in (args.env, args.target) if x) or "Locust run"),
        "run_ctx": run_ctx,
        "verdict": verdict_text(assess, total, hist, bp, args), "checks": assess["checks"],
        "facts": facts, "notes": assess["findings"] + ([f"Performance started to degrade at about {bp['users']:,.0f} concurrent users."] if bp else []),
        "endpoints": endpoints, "failures": failures, "charts": charts,
    }
    out_html = args.out or f"{args.prefix}_report.html"
    out_md = os.path.splitext(out_html)[0].removesuffix("_report") + "_summary.md"
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(build_html(ctx))
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(build_md(ctx))
    print(f"{ctx['verdict'][0]}: {ctx['verdict'][1]}\nReport:  {out_html}\nSummary: {out_md}")
    sys.exit(0 if ctx["verdict"][0] == "Passed" else 1)


if __name__ == "__main__":
    main()
