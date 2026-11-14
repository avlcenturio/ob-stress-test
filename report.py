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
        ax.annotate("degradation point", (x, 0), xycoords=("data", "axes fraction"),
                    xytext=(4, 4), textcoords="offset points", color=C_BAD, fontsize=9)


def chart_throughput(hist, bp, target=None):
    x = elapsed_min(hist)
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.plot(x, [h["rps"] for h in hist], color=C_RPS, lw=1.8, label="Requests/s")
    if target:
        ax.axhline(target["value"], color=C_BAD, lw=1, ls="--",
                   label=f"Target {target['value']:g} req/s ({target['factor']:g} × peak {target['peak']:g} req/s)")
    ax.set_ylabel("Requests/s")
    ax.set_xlabel("Minutes into test")
    ax.set_ylim(bottom=0)
    ax2 = ax.twinx()
    ax2.plot(x, [h["users"] for h in hist], color=C_USERS, lw=1.2, ls="--", label="Users")
    ax2.set_ylabel("Concurrent users")
    ax2.set_ylim(bottom=0)
    ax2.grid(False)
    ax2.spines["right"].set_visible(True)
    mark_bp(ax, hist, bp)
    lines = [l for l in ax.get_lines() if not l.get_label().startswith("_")] + ax2.get_lines()
    ax.legend(lines, [l.get_label() for l in lines], loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3)
    return to_svg(fig)


def chart_latency(hist, bp, slo=None):
    """Rolling percentiles across all endpoints (informational)."""
    x = elapsed_min(hist)
    fig, ax = plt.subplots(figsize=(9, 3.2))
    for key, color, lab in (("p50", C_P50, "p50"), ("p95", C_P95, "p95"), ("p99", C_P99, "p99")):
        ax.plot(x, [h[key] for h in hist], color=color, lw=1.6, label=lab)
    if slo:
        ax.axhline(slo, color=C_BAD, lw=1, ls="--", label=f"OBL benchmark ({slo:,.0f} ms, average)")
    mark_bp(ax, hist, bp)
    ax.set_ylabel("Response time (ms)")
    ax.set_xlabel("Minutes into test")
    ax.set_ylim(bottom=0)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=4)
    return to_svg(fig)


def chart_errors(hist, bp, slo):
    """All errors together: used for runs recorded before errors were split by type."""
    x = elapsed_min(hist)
    fig, ax = plt.subplots(figsize=(9, 2.6))
    ax.fill_between(x, [h["err_pct"] for h in hist], color=C_BAD, alpha=0.25, lw=0)
    ax.plot(x, [h["err_pct"] for h in hist], color=C_BAD, lw=1.4, label="All errors, incl. 4xx (% of requests)")
    mark_bp(ax, hist, bp)
    ax.set_ylabel("% of requests")
    ax.set_xlabel("Minutes into test")
    ax.set_ylim(bottom=0)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0))
    return to_svg(fig)


def chart_endpoints(endpoints, slo):
    eps = sorted([e for e in endpoints if e["count"]], key=lambda e: e["avg"] or 0)
    fig, ax = plt.subplots(figsize=(9, 0.42 * len(eps) + 1.1))
    labels = [label(e) for e in eps]
    vals = [e["avg"] or 0 for e in eps]
    ax.barh(labels, vals, color=[C_BAD if v > slo else C_P95 for v in vals], height=0.6)
    ax.axvline(slo, color=C_BAD, lw=1, ls="--", label=f"OBL benchmark {slo:,.0f} ms (average)")
    for y, v in enumerate(vals):
        ax.annotate(f"{v:,.0f} ms", (v, y), xytext=(4, 0), textcoords="offset points", va="center", fontsize=8.5)
    ax.set_xlim(0, max(vals + [slo]) * 1.12)
    ax.set_xlabel("Average response time (ms)")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0))
    return to_svg(fig)


# ---------------------------------------------------------- run timeline ----
# Written by the load test (run_timeline.json): per second the API requests, errors by status code and
# per-endpoint count and total response time.

def load_timeline(prefix):
    path = f"{prefix}_timeline.json"
    if not os.path.exists(path):
        return None
    try:
        import json
        with open(path) as fh:
            data = json.load(fh)
    except ValueError:
        return None
    secs = data.get("secs") or []
    if len(secs) < 2:
        return None
    t0, t1 = secs[0][0], secs[-1][0]
    span = list(range(t0, t1 + 1))
    by = {s[0]: s for s in secs}
    n = [by[s][1] if s in by else 0 for s in span]
    codes = sorted({c for s in secs for c in s[2]})
    err = {c: [by[s][2].get(c, 0) if s in by else 0 for s in span] for c in codes}
    names = sorted({k for s in secs for k in s[3]})
    ep = {k: ([by[s][3].get(k, [0, 0])[0] if s in by else 0 for s in span],
              [by[s][3].get(k, [0, 0])[1] if s in by else 0 for s in span]) for k in names}
    wait_n = [by[s][4][0] if s in by and len(by[s]) > 4 else 0 for s in span]
    wait_ms = [by[s][4][1] if s in by and len(by[s]) > 4 else 0 for s in span]
    rtt = [x for x in (data.get("rtt") or []) if x[1] is not None]
    return {"t0": t0, "ts": span, "n": n, "err": err, "ep": ep, "errors": data.get("errors") or {},
            "phases": data.get("phases") or {}, "rtt": rtt, "wait_n": wait_n, "wait_ms": wait_ms}


def median_rtt(tl):
    vals = sorted(x[1] for x in (tl or {}).get("rtt") or [])
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def time_split(tl):
    """Per endpoint, average milliseconds per request: connection setup, network, server (estimated), download."""
    rtt = median_rtt(tl) or 0.0
    out = {}
    for name, p in (tl or {}).get("phases", {}).items():
        n = p.get("n") or 0
        if not n:
            continue
        setup = (p["tcp_ms"] + p["tls_ms"]) / n
        wait = p["wait_ms"] / n
        network = min(rtt, wait)
        out[name] = {"setup": setup, "network": network, "server": max(0.0, wait - network),
                     "download": p["dl_ms"] / n, "total": p["total_ms"] / n, "n": n, "new_conns": p["conn_n"],
                     "tcp": p["tcp_ms"] / max(1, p["conn_n"]), "tls": p["tls_ms"] / max(1, p["conn_n"])}
    return out


def _window(values, w=10):
    """Sum over the last w seconds (inclusive)."""
    out, acc = [], 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= w:
            acc -= values[i - w]
        out.append(acc)
    return out


def is_5xx(code):
    return code.isdigit() and code.startswith("5")


def code_label(code):
    if code == "no reply":
        return "No reply / timeout"
    try:
        from http import HTTPStatus
        return f"{code} {HTTPStatus(int(code)).phrase}"
    except (ValueError, KeyError):
        return code


def tl_pct(tl, codes, w=10):
    """Rolling share (%) of requests that ended with one of these codes."""
    n = _window(tl["n"], w)
    hits = _window([sum(tl["err"][c][i] for c in codes if c in tl["err"]) for i in range(len(tl["n"]))], w)
    return [100.0 * h / t if t else 0.0 for h, t in zip(hits, n)]


def tl_avg(tl, name, w=10):
    cnt, ms = tl["ep"][name]
    c, m = _window(cnt, w), _window(ms, w)
    return [mm / cc if cc else None for cc, mm in zip(c, m)]


def chart_avg_over_time(tl, slo, bp_ts=None, top=4):
    x = [(s - tl["t0"]) / 60 for s in tl["ts"]]
    overall = {k: (sum(tl["ep"][k][1]) / max(1, sum(tl["ep"][k][0]))) for k in tl["ep"]}
    ranked = sorted(overall, key=lambda k: -overall[k])
    colours = [C_P95, C_P99, C_P50, "#8c6bc2"]
    fig, ax = plt.subplots(figsize=(9, 3.6))
    for name, col in zip(ranked[:top], colours):
        ax.plot(x, tl_avg(tl, name), color=col, lw=1.5, label=name.split(" ", 1)[-1])
    rest = ranked[top:]
    if rest:
        series = [tl_avg(tl, k) for k in rest]
        mean = []
        for i in range(len(x)):
            vals = [s[i] for s in series if s[i] is not None]
            mean.append(sum(vals) / len(vals) if vals else None)
        ax.plot(x, mean, color=C_USERS, lw=1.3, label=f"others ({len(rest)}, mean)")
    ax.axhline(slo, color=C_BAD, lw=1, ls="--", label=f"OBL benchmark {slo:,.0f} ms")
    if bp_ts:
        bx = (bp_ts - tl["t0"]) / 60
        ax.axvline(bx, color=C_BAD, lw=1, ls=":")
        ax.annotate("degradation point", (bx, 0), xycoords=("data", "axes fraction"), xytext=(4, 4),
                    textcoords="offset points", color=C_BAD, fontsize=9)
    ax.set_ylabel("Average response (ms)")
    ax.set_xlabel("Minutes into test")
    ax.set_ylim(bottom=0)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, fontsize=8.5)
    return to_svg(fig)


def chart_5xx(tl, slo):
    x = [(s - tl["t0"]) / 60 for s in tl["ts"]]
    pct = tl_pct(tl, [c for c in tl["err"] if is_5xx(c)])
    fig, ax = plt.subplots(figsize=(9, 2.6))
    ax.plot(x, pct, color=C_BAD, lw=1.8, label="5xx responses (% of requests)")
    ax.axhline(slo, color=C_BAD, lw=1, ls="--", label=f"OBL benchmark {slo:g}%")
    ax.set_ylim(0, max([slo * 2] + [p * 1.15 for p in pct]))
    ax.set_ylabel("% of requests")
    ax.set_xlabel("Minutes into test")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2)
    return to_svg(fig)


def chart_other_errors(tl):
    others = [c for c in tl["err"] if not is_5xx(c)]
    if not others:
        return None
    order = sorted(others, key=lambda c: -sum(tl["err"][c]))
    palette = [C_P50, C_P95, "#e07ab0", "#8fd18a", C_USERS, "#8c6bc2", C_P99, "#c9a227"]
    x = [(s - tl["t0"]) / 60 for s in tl["ts"]]
    fig, ax = plt.subplots(figsize=(9, 3.2))
    for c, col in zip(order, palette * 3):
        ax.plot(x, tl_pct(tl, [c]), color=col, lw=1.5, label=code_label(c))
    ax.set_ylabel("% of requests")
    ax.set_xlabel("Minutes into test")
    ax.set_ylim(bottom=0)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, fontsize=8.5)
    return to_svg(fig)


def chart_time_split(split, slo):
    names = sorted(split, key=lambda k: split[k]["total"])
    parts = [("setup", "Connection setup (TCP + TLS)", C_USERS), ("network", "Network round trip", C_P50),
             ("server", "Server processing (estimated)", C_P95), ("download", "Download", C_P99)]
    fig, ax = plt.subplots(figsize=(9, 0.42 * len(names) + 1.3))
    left = [0.0] * len(names)
    labels = [k.split(" ", 1)[-1] if " " in k else k for k in names]
    for key, lab, col in parts:
        vals = [split[k][key] for k in names]
        ax.barh(labels, vals, left=left, color=col, height=0.6, label=lab)
        left = [a + b for a, b in zip(left, vals)]
    for y, k in enumerate(names):
        ax.annotate(f"{split[k]['total']:,.0f} ms", (left[y], y), xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=8.5)
    ax.axvline(slo, color=C_BAD, lw=1, ls="--")
    ax.set_xlim(0, max(left + [slo]) * 1.15)
    ax.set_xlabel("Average per request (ms)")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, fontsize=8.5)
    return to_svg(fig)


def chart_network(tl):
    x = [(s - tl["t0"]) / 60 for s in tl["ts"]]
    wn, wm = _window(tl["wait_n"]), _window(tl["wait_ms"])
    wait = [m / n if n else None for n, m in zip(wn, wm)]
    fig, ax = plt.subplots(figsize=(9, 3.0))
    ax.plot(x, wait, color=C_P95, lw=1.6, label="Waiting for the first byte (all endpoints, 10 s average)")
    rx = [(s - tl["t0"]) / 60 for s, _ in tl["rtt"]]
    ax.plot(rx, [v for _, v in tl["rtt"]], color=C_P50, lw=1.4, marker="o", ms=2.5,
            label="Network round trip (TCP connect)")
    ax.set_ylabel("ms")
    ax.set_xlabel("Minutes into test")
    ax.set_ylim(bottom=0)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, fontsize=8.5)
    return to_svg(fig)


def degradation_from_timeline(tl, hist, args):
    """First moment an endpoint's 10 s average or the 5xx share stayed over the benchmark for --sustain seconds."""
    start = args.warmup
    series = [tl_avg(tl, k) for k in tl["ep"]]
    p5 = tl_pct(tl, [c for c in tl["err"] if is_5xx(c)])
    run = 0
    for i in range(len(tl["ts"])):
        bad = (any(s[i] is not None and s[i] > args.avg_ms for s in series) or p5[i] > args.max_5xx_pct)
        run = run + 1 if (bad and i >= start) else 0
        if run >= args.sustain:
            ts = tl["ts"][i - args.sustain + 1]
            users = next((h["users"] for h in hist if h["ts"] >= ts), hist[-1]["users"] if hist else 0)
            return {"ts": ts, "users": users}
    return None


def label(e):
    """Endpoint label; avoids 'GET GET /x' when the request name already has the method."""
    name, method = e.get("name") or e.get("Name", ""), e.get("method") or e.get("Method", "")
    return name if name.upper().startswith(method.upper() + " ") else f"{method} {name}".strip()


def fmt_ms_fine(v):
    """Like fmt_ms, but keeps one decimal under 10 ms (network round trips can be well under 1 ms)."""
    if v is None:
        return "–"
    return f"{v:.1f} ms" if v < 10 else fmt_ms(v)


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


def sustained_throughput(hist):
    """Average requests per second once all users are running (the ramp-up doesn't count)."""
    if not hist:
        return None, 0
    peak = max(h["users"] for h in hist)
    first = next(i for i, h in enumerate(hist) if h["users"] == peak)
    window = [h["rps"] for h in hist[first + 3:] if h["users"] == peak] or [h["rps"] for h in hist[first:]]
    return (sum(window) / len(window) if window else None), peak


def throughput_target(run_ctx):
    peak = (run_ctx or {}).get("peak_rps")
    if not peak:
        return None
    factor = (run_ctx or {}).get("stress_factor") or 2.0
    return {"peak": peak, "factor": factor, "value": round(peak * factor, 2)}


def obl_assessment(endpoints, total, hist, failures, args, run_ctx=None):
    kinds = classify_failures(failures)
    api = [e for e in endpoints if is_api(e) and e["count"]]
    for e in endpoints:
        k = kinds.get((e["method"], e["name"]), {"5xx": 0, "no_reply": 0, "4xx": 0})
        e["n5xx"], e["no_reply"], e["n4xx"] = k["5xx"], k["no_reply"], k["4xx"]
        e["pct5xx"] = 100.0 * e["n5xx"] / e["count"] if e["count"] else 0.0
        e["area"] = area_of(e) if is_api(e) else ("Token" if e["method"] == "AUTH" else "Settings")
        e["ok"] = (not is_api(e)) or ((e["avg"] or 0) <= args.avg_ms and e["pct5xx"] <= args.max_5xx_pct)
    n5xx = sum(e["n5xx"] for e in api)
    no_reply = sum(e["no_reply"] for e in api)
    requests_ = sum(e["count"] for e in api) or 1
    pct5xx = 100.0 * n5xx / requests_
    slow = sorted([e for e in api if (e["avg"] or 0) > args.avg_ms], key=lambda e: -(e["avg"] or 0))
    worst = max(api, key=lambda e: e["avg"] or 0, default=None)
    sustained, peak_users = sustained_throughput(hist)
    target = throughput_target(run_ctx)
    if target and sustained is not None:
        t_ok = sustained >= target["value"] * 0.98      # within 2%: smoothing of the per-second measurement
        t_check = {"name": "Throughput and users", "ok": t_ok,
                   "target": f"≥ {target['factor']:g} × peak-hour requests per second of the existing digital channel "
                             f"({target['peak']:g} req/s × {target['factor']:g} = {target['value']:g} req/s)",
                   "measured": f"{sustained:,.1f} req/s sustained with {peak_users:,.0f} concurrent users"}
    else:
        t_check = {"name": "Throughput and users", "ok": None, "na": "Not assessed",
                   "target": "≥ 2 × peak-hour requests per second of the existing digital channel",
                   "measured": (f"{sustained:,.1f} req/s sustained with {peak_users:,.0f} concurrent users; "
                                if sustained is not None else "") +
                               "no target: peak-hour usage isn’t set in the test profile"}
    checks = [
        t_check,
        {"name": "Performance", "target": f"average ≤ {args.avg_ms:,.0f} ms per endpoint response", "ok": not slow,
         "measured": (f"slowest endpoint averages {fmt_ms(worst['avg'])} ({label(worst)})" if worst else "no API calls")
         + (f"; {len(slow)} endpoint(s) over" if slow else "")},
        {"name": "Error rate", "target": f"≤ {args.max_5xx_pct:g}% 5xx responses", "ok": pct5xx <= args.max_5xx_pct,
         "measured": f"{pct5xx:.2f}% ({n5xx:,} of {requests_:,} requests)"
         + (f"; {no_reply:,} got no reply" if no_reply else "")},
        {"name": "Availability", "target": f"{args.availability_pct:g}% uptime per quarter", "ok": None,
         "na": "Not in scope",
         "measured": "Not in the scope of a stress test; reported in a dedicated availability report over at least "
                     "three months"},
    ]
    findings = []
    if t_check["ok"] is False:
        findings.append(f"Throughput: {sustained:,.1f} req/s sustained against a target of {target['value']:g} req/s "
                        f"({target['factor']:g} × peak {target['peak']:g} req/s). Add users or raise the requests per "
                        "second per user, or check whether the financial institution limited the rate.")
    for e in slow:
        findings.append(f"Performance: {label(e)} averaged {fmt_ms(e['avg'])} against {args.avg_ms:,.0f} ms "
                        f"(p95 {fmt_ms(e['p95'])}, {e['count']:,} requests).")
    if not checks[2]["ok"]:
        for e in sorted([e for e in api if e["n5xx"]], key=lambda e: -e["n5xx"])[:5]:
            findings.append(f"Error rate: {label(e)} returned {e['n5xx']:,} 5xx response{'s' if e['n5xx'] != 1 else ''} "
                            f"({e['pct5xx']:.2f}%).")
    return {"checks": checks, "findings": findings, "passed": all(c["ok"] for c in checks if c["ok"] is not None),
            "pct5xx": pct5xx, "worst": worst, "sustained": sustained, "peak_users": peak_users, "target": target,
            "requests": requests_, "n4xx": sum(e["n4xx"] for e in api), "no_reply": no_reply}


def verdict_text(assess, total, hist, bp, args):
    peak_users = assess.get("peak_users") or max((h["users"] for h in hist), default=0)
    c = {x["name"]: x for x in assess["checks"]}
    if assess["passed"]:
        parts = []
        if c["Throughput and users"]["ok"]:
            parts.append(f"throughput ({assess['sustained']:,.1f} req/s sustained against a "
                         f"{assess['target']['value']:g} req/s target)")
        parts.append(f"performance (every endpoint averages within {args.avg_ms:,.0f} ms)")
        parts.append(f"error rate ({assess['pct5xx']:.2f}% 5xx)")
        listed = ", ".join(parts[:-1]) + " and " + parts[-1]
        return ("Passed", f"Meets the benchmarks for {listed}, with up to {peak_users:.0f} concurrent users.")
    missed = [x["name"].lower() for x in assess["checks"] if x["ok"] is False]
    listed = missed[0] if len(missed) == 1 else ", ".join(missed[:-1]) + " and " + missed[-1]
    return ("Failed", "Misses the benchmark for " + listed +
            f", with up to {peak_users:.0f} concurrent users. See What we found.")


def errors_by_type(tl, failures, hist, api_requests):
    """One row per error type: counts towards the benchmark, count, share, where and when, example message."""
    rows = []
    t0 = hist[0]["ts"] if hist else None

    def when(first, last):
        if t0 is None or first is None:
            return ""
        users = next((h["users"] for h in hist if h["ts"] >= first), None)
        f, l = (first - t0) / 60, (last - t0) / 60
        span = f"at {f:.1f} min" if abs(l - f) < 0.1 else f"{f:.1f}–{l:.1f} min"
        return span + (f" (from ≈ {users:.0f} users)" if users else "")
    if tl and tl.get("errors"):
        for code, e in tl["errors"].items():
            eps = sorted(e["endpoints"].items(), key=lambda kv: -kv[1])
            where = ", ".join(k.split(" ", 1)[-1] for k, _ in eps[:3]) + (f" +{len(eps) - 3} more" if len(eps) > 3 else "")
            rows.append({"code": code, "count": e["count"], "where": where, "when": when(e["first"], e["last"]),
                         "example": e.get("example", "")})
    else:   # older runs: from the failures table, without timings
        groups = {}
        for f in failures:
            m = STATUS.search(f.get("Error") or "")
            code = m.group(1) if m else ("no reply" if NO_REPLY.search(f.get("Error") or "") else "other")
            g = groups.setdefault(code, {"code": code, "count": 0, "eps": {}, "example": ""})
            n = int(num(f.get("Occurrences")) or 0)
            g["count"] += n
            g["eps"][f.get("Name", "")] = g["eps"].get(f.get("Name", ""), 0) + n
            g["example"] = g["example"] or re.sub(r"^\w+\((['\"])(.*)\1\)$", r"\2", f.get("Error") or "")[:240]
        for g in groups.values():
            eps = sorted(g["eps"].items(), key=lambda kv: -kv[1])
            rows.append({"code": g["code"], "count": g["count"], "when": "",
                         "where": ", ".join(k.split(" ", 1)[-1] for k, _ in eps[:3]) + (f" +{len(eps) - 3} more" if len(eps) > 3 else ""),
                         "example": g["example"]})
    if not any(is_5xx(r["code"]) for r in rows):
        rows.append({"code": "5xx", "count": 0, "where": "–", "when": "", "example": "–"})
    for r in rows:
        r["counts"] = is_5xx(r["code"]) or r["code"] == "5xx"
        r["share"] = 100.0 * r["count"] / api_requests if api_requests else 0.0
        r["label"] = "5xx" if r["code"] == "5xx" else code_label(r["code"])
    rows.sort(key=lambda r: (not r["counts"], -r["count"]))
    return rows


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
.callout{border-left:4px solid var(--steel);background:color-mix(in srgb,var(--steel) 8%%,transparent);padding:10px 14px;border-radius:6px;margin:0 0 20px;font-size:.95rem}
td.l,th.l{text-align:left!important;white-space:normal!important}
td.err{white-space:normal!important;min-width:220px;max-width:340px;color:var(--muted);font-size:.85rem;font-family:ui-monospace,Consolas,monospace}
.na{color:var(--muted);font-weight:600}
main h3{font-size:1rem;margin:22px 0 4px}
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


def _result_cell(c):
    if c["ok"] is None:
        return f"<td class=\"na\">{html.escape(c.get('na', 'Not in scope'))}</td>"
    return f"<td class=\"{'ok' if c['ok'] else 'bad'}\">{'Met' if c['ok'] else 'Not met'}</td>"


BENCH_NOTE = ("Throughput is set from the existing digital channel: the requests per second it handles in its peak "
              "hour, multiplied by 2 to add the stress element. Performance and error rate are the recommended "
              f"benchmarks from the {OBL_REF}; only 5xx responses count as errors, as 4xx are attributable to the "
              "TPP. Availability is not in the scope of a stress test.")


def build_html(ctx):
    e = html.escape
    a = ctx["args"]
    split = ctx.get("split") or {}
    run_id = str((ctx.get("run_ctx") or {}).get("run_id", ""))
    charts = ctx["charts"]
    tgt = ctx.get("target")
    rows = "".join(
        f"<tr><td class=\"l\">{e(label(x))}</td><td>{x['count']:,}</td><td>{fmt_ms(x['avg'])}</td>"
        f"<td>{fmt_ms(split[x['name']]['server']) if x['name'] in split else '–'}</td>"
        f"<td>{fmt_ms(x['p95'])}</td><td>{x.get('n5xx', 0):,}</td>"
        f"<td>{x.get('n4xx', 0):,}" + (f" ({100.0 * x.get('n4xx', 0) / x['count']:.1f}%)" if x.get('n4xx') and x['count'] else "") + "</td>"
        f"<td class=\"{'ok' if x['ok'] else 'bad'}\">{('Pass' if x['ok'] else 'Fail') if is_api(x) else '–'}</td></tr>"
        for x in ctx["endpoints"])
    bench = "".join(f"<tr><td>{e(c['name'])}</td><td>{e(c['target'])}</td><td>{e(c['measured'])}</td>{_result_cell(c)}</tr>"
                    for c in ctx["checks"])
    et = "".join(
        f"<tr><td class=\"l\"><b>{e(r['label'])}</b></td><td class=\"l\">{'Yes' if r['counts'] else 'No'}"
        f"{' · availability out of scope' if r['code'] == 'no reply' else ''}</td>"
        f"<td>{r['count']:,}</td><td>{r['share']:.2f}%</td>"
        f"<td class=\"l\">{e(r['where'] or '–')}{('; ' + e(r['when'])) if r['when'] else ''}</td>"
        f"<td class=\"l err\">{e(r['example'] or '–')}</td></tr>" for r in ctx["error_types"])
    facts = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in ctx["facts"])
    obs = "".join(f"<li>{e(n)}</li>" for n in ctx["notes"])
    status = "pass" if ctx["verdict"][0] == "Passed" else "fail"

    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(ctx['title'])}</title><style>{CSS % CSS_VARS}</style></head><body>{BRAND_BAR}<main>
<div class="titlebar"><div><h1>{e(ctx['title'])}</h1><p class="sub">{e(ctx['subtitle'])}</p></div>
<button class="pdf" type="button" id="pdfBtn" data-run="{e(run_id)}">Download PDF</button></div>
{details_html(ctx)}
<section class="verdict {status}"><strong>{ctx['verdict'][0]}</strong><p>{e(ctx['verdict'][1])}</p></section>
<dl class="facts">{facts}</dl>"""]
    if ctx.get("callout"):
        parts.append(f"<div class=\"callout\">{ctx['callout']}</div>")
    parts.append(f"<h2>Benchmarks</h2><p class=\"note\">{e(BENCH_NOTE)}</p>"
                 "<div class=\"scroll\"><table class=\"bench\"><thead><tr><th>Requirement</th><th>Benchmark</th>"
                 f"<th>This run</th><th>Result</th></tr></thead><tbody>{bench}</tbody></table></div>")
    if obs and ctx["verdict"][0] != "Passed":
        parts.append(f"<h2>What we found</h2><ul class=\"obs\">{obs}</ul>")
    if charts.get("throughput"):
        tnote = ("Requests per second and concurrent users over the run." +
                 (f" The target line is the peak-hour usage of the existing digital channel ({tgt['peak']:g} req/s) "
                  f"multiplied by {tgt['factor']:g}, the stress element. Sustained throughput is measured once all "
                  "users are running." if tgt else " No throughput target: set the peak-hour usage in the test profile."))
        parts.append(f"<h2>1. Throughput and users</h2><p class=\"note\">{e(tnote)}</p>{charts['throughput']}")
    bp = ctx.get("bp")
    perf_note = (f"The OBL benchmark is an average of {a.avg_ms:,.0f} ms per endpoint response. " +
                 (f"Degradation point at about {bp['users']:,.0f} concurrent users: from there, an endpoint’s average "
                  f"or the 5xx share stayed over the benchmark for {a.sustain} seconds or more." if bp else
                  f"No degradation point: no endpoint’s average stayed above {a.avg_ms:,.0f} ms for {a.sustain} "
                  "seconds or more."))
    parts.append(f"<h2>2. Performance</h2><p class=\"note\">{e(perf_note)}</p>")
    if charts.get("avg_time"):
        parts.append("<h3>Average response time per endpoint over time</h3><p class=\"note\">10-second rolling "
                     "average. The slowest four endpoints are shown separately; the rest as their mean.</p>"
                     + charts["avg_time"])
    parts.append("<h3>Average response time per endpoint</h3><p class=\"note\">Over the whole run, slowest first, "
                 f"against the benchmark.</p>{charts['endpoints']}")
    if charts.get("split"):
        parts.append("<h3>Where the time goes</h3><p class=\"note\">Average per request, per endpoint. "
                     "<b>Connection setup</b> is paid only when a new connection is opened (TCP and TLS handshake), "
                     "so it is spread over all requests. <b>Network round trip</b> is the measured time to reach the "
                     "resource server and back. <b>Server processing</b> is the rest of the wait for the first byte: "
                     "an estimate of the financial institution’s own time. <b>Download</b> is receiving the response "
                     "body, which grows with response size.</p>" + charts["split"])
    if charts.get("network"):
        parts.append("<h3>Network latency over time</h3><p class=\"note\">The network round trip is measured every "
                     "5 seconds by opening a TCP connection to the resource server. If both lines rise together, the "
                     "network slowed down; if only the wait for the first byte rises, the server did. A TCP connect "
                     "also includes the server accepting the connection, so it can rise briefly when many connections "
                     "open at once, for example at the start of a run.</p>" + charts["network"])
    if charts.get("latency"):
        parts.append("<h3>Response time percentiles</h3><p class=\"note\">For engineers: p50, p95 and p99 across all "
                     "endpoints. Informational only; Locust rounds these (10 ms steps between 100 ms and 1 s).</p>"
                     + charts["latency"])
    parts.append("<h2>3. Error rate</h2><p class=\"note\">Only 5xx responses count towards the OBL benchmark. Other "
                 "errors are shown separately so their cause is visible.</p>")
    if charts.get("e5xx"):
        parts.append(f"<h3>5xx errors</h3>{charts['e5xx']}")
        parts.append("<h3>Other errors</h3><p class=\"note\">Every error that isn’t a 5xx, by type: 4xx responses, "
                     "which OBL attributes to the TPP, and requests that got no reply. Shown so their cause is visible; "
                     "they don’t count towards the error rate benchmark.</p>"
                     + (charts.get("eother") or "<p class=\"note\">No other errors in this run.</p>"))
    elif charts.get("errors"):
        parts.append("<h3>All errors</h3><p class=\"note\">This run was recorded before errors were split by type, so "
                     "5xx and other errors are shown together here; the error rate benchmark above counts 5xx only.</p>"
                     + charts["errors"])
    parts.append("<h3>Errors by type</h3><p class=\"note\">Every error the financial institution returned, grouped by "
                 "type, with where and when it happened and an example message.</p>"
                 "<div class=\"scroll\"><table><thead><tr><th class=\"l\">Type</th><th class=\"l\">Counts towards "
                 "benchmark</th><th>Requests</th><th>Share</th><th class=\"l\">Where / when</th><th class=\"l\">"
                 f"Example message</th></tr></thead><tbody>{et}</tbody></table></div>")
    parts.append(f"<h2>4. Endpoints summary</h2><p class=\"note\">Sorted by average response time. An endpoint passes "
                 f"when its average is ≤ {a.avg_ms:,.0f} ms and 5xx ≤ {a.max_5xx_pct:g}%. Token and settings lines "
                 "are shown for information only.</p><div class=\"scroll\"><table><thead><tr><th class=\"l\">"
                 "Endpoint</th><th>Requests</th><th>Average</th><th>Server (est.)</th><th>p95</th><th>5xx</th><th>4xx</th>"
                 "<th>Status</th>"
                 f"</tr></thead><tbody>{rows}</tbody></table></div>")
    parts.append(f"<footer>Generated {e(fmt_time(datetime.now(timezone.utc).timestamp()))} from "
                 f"{e(os.path.basename(ctx['prefix']))}_*.csv</footer></main>{PDF_JS}</body></html>")
    return "".join(parts)


def build_md(ctx):
    a = ctx["args"]
    lines = [f"# {ctx['title']}", "", f"**{ctx['verdict'][0]}.** {ctx['verdict'][1]}", ""]
    lines += [f"- {k}: {v}" for k, v in ctx["facts"]] + [""]
    if ctx.get("callout_text"):
        lines += [f"> {ctx['callout_text']}", ""]
    lines += ["## Benchmarks", "", BENCH_NOTE, "", "| Requirement | Benchmark | This run | Result |", "|---|---|---|---|"]
    lines += [f"| {c['name']} | {c['target']} | {c['measured']} | "
              f"{'➖ ' + c.get('na', 'Not in scope') if c['ok'] is None else '✅ Met' if c['ok'] else '❌ Not met'} |"
              for c in ctx["checks"]]
    lines += [""]
    if ctx["notes"] and ctx["verdict"][0] != "Passed":
        lines += ["## What we found", ""] + [f"- {n}" for n in ctx["notes"]] + [""]
    split = ctx.get("split") or {}
    if split:
        lines += ["## Where the time goes (average per request)", "",
                  "| Endpoint | Connection setup | Network | Server (est.) | Download | Total | New connections |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        lines += [f"| {k} | {fmt_ms_fine(v['setup'])} | {fmt_ms_fine(v['network'])} | {fmt_ms_fine(v['server'])} | "
                  f"{fmt_ms_fine(v['download'])} | {fmt_ms_fine(v['total'])} | {v['new_conns']:,} |"
                  for k, v in sorted(split.items(), key=lambda kv: -kv[1]["total"])]
        lines += [""]
    lines += ["## Errors by type", "", "| Type | Counts towards benchmark | Requests | Share | Where / when |",
              "|---|---|---:|---:|---|"]
    lines += [f"| {r['label']} | {'Yes' if r['counts'] else 'No'} | {r['count']:,} | {r['share']:.2f}% | "
              f"{r['where'] or '–'}{('; ' + r['when']) if r['when'] else ''} |" for r in ctx["error_types"]]
    lines += ["", f"## Endpoints summary (average ≤ {a.avg_ms:,.0f} ms, 5xx ≤ {a.max_5xx_pct:g}%)", "",
              "| Endpoint | Requests | Average | p95 | 5xx | 4xx | Status |",
              "|---|---:|---:|---:|---:|---:|---|"]
    lines += [f"| {label(x)} | {x['count']:,} | {fmt_ms(x['avg'])} | {fmt_ms(x['p95'])} | {x.get('n5xx', 0):,} | "
              f"{x.get('n4xx', 0):,} | {('✅' if x['ok'] else '❌') if is_api(x) else '–'} |" for x in ctx["endpoints"]]
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
    tl = load_timeline(args.prefix)
    assess = obl_assessment(endpoints, total, hist, failures, args, run_ctx)
    endpoints.sort(key=lambda e: (not is_api(e), -(e["avg"] or 0)))
    failures.sort(key=lambda f: num(f.get("Occurrences")) or 0, reverse=True)
    # degradation point: same rules as the verdict (endpoint average over the benchmark, or 5xx over 0.5%)
    if tl and hist:
        bp = degradation_from_timeline(tl, hist, args)
    else:
        bp, _ = find_breaking_point(hist, args.avg_ms, args.max_5xx_pct, args.sustain, args.warmup, key="p50")
    target = assess["target"]

    facts = []
    if hist:
        facts += [("Started", fmt_time(hist[0]["ts"])),
                  ("Duration", fmt_duration(hist[-1]["ts"] - hist[0]["ts"])),
                  ("Peak users", f"{assess['peak_users']:,.0f}")]
        if assess["sustained"] is not None:
            facts.append(("Sustained throughput", f"{assess['sustained']:,.1f} req/s"))
    if target:
        facts.append(("Throughput target", f"{target['value']:g} req/s"))
    split = time_split(tl)
    rtt = median_rtt(tl)
    facts += [("Total requests", f"{total['count']:,}"),
              ("Average response", fmt_ms(total["avg"])),
              ] + ([("Network round trip (median)", fmt_ms_fine(rtt))] if rtt is not None else []) + [
              ("5xx error rate", f"{assess['pct5xx']:.2f}%")]
    if bp:
        facts.append(("Degradation point", f"{bp['users']:,.0f} users"))
    if exceptions:
        facts.append(("Script exceptions", f"{sum(int(num(x.get('Count')) or 0) for x in exceptions):,}"))

    error_types = errors_by_type(tl, failures, hist, assess["requests"])
    other = [r for r in error_types if not r["counts"] and r["count"]]
    other_share = sum(r["share"] for r in other)
    callout = callout_text = ""
    if other_share >= 1.0:
        top = max(other, key=lambda r: r["count"])
        callout_text = (f"Not counted towards the verdict: {other_share:.1f}% of requests got an error other than 5xx, "
                        f"mostly {top['label']}" + (f", {top['when']}" if top["when"] else "") + ". See 3. Error rate.")
        callout = (f"<b>Not counted towards the verdict:</b> {other_share:.1f}% of requests got an error other than 5xx, "
                   f"mostly <b>{html.escape(top['label'])}</b>" + (f", {html.escape(top['when'])}" if top["when"] else "")
                   + ". See <i>3. Error rate</i>.")

    charts = {"endpoints": chart_endpoints([e for e in endpoints if is_api(e)], args.avg_ms)}
    if len(hist) > 1:
        charts.update(throughput=chart_throughput(hist, bp, target), latency=chart_latency(hist, bp))
        if tl:
            if split:
                charts["split"] = chart_time_split(split, args.avg_ms)
            if tl["rtt"]:
                charts["network"] = chart_network(tl)
            charts.update(avg_time=chart_avg_over_time(tl, args.avg_ms, bp["ts"] if bp else None),
                          e5xx=chart_5xx(tl, args.max_5xx_pct), eother=chart_other_errors(tl))
        else:
            charts.update(errors=chart_errors(hist, bp, args.max_5xx_pct))
    else:
        print("Note: no usable _stats_history.csv, so time-series charts are skipped.")

    ctx = {
        "args": args, "prefix": args.prefix, "title": args.title,
        "subtitle": (" · ".join(x for x in (run_ctx.get("tenant"), run_ctx.get("profile")) if x) if run_ctx else
                     " – ".join(x for x in (args.env, args.target) if x) or "Locust run"),
        "run_ctx": run_ctx, "target": target, "bp": bp,
        "verdict": verdict_text(assess, total, hist, bp, args), "checks": assess["checks"],
        "facts": facts, "notes": assess["findings"] + ([f"Performance started to degrade at about {bp['users']:,.0f} concurrent users."] if bp else []),
        "endpoints": endpoints, "failures": failures, "charts": charts, "error_types": error_types,
        "callout": callout, "callout_text": callout_text, "split": split,
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
