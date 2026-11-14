"""Brand (name, colours, font, logo) for every page and report. Edit brand/brand.json."""
import base64
import json
import mimetypes
import os
import re

BRAND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brand")
DEFAULTS = {"name": "Ozone API", "product": "Open Banking Stress Test", "navy": "#0B1340",
            "navy_soft": "#1B2766", "accent": "#35BFD4", "accent_ink": "#0B1340", "accent_text": "#167A8A",
            "accent_on_dark": "#35BFD4", "orange": "#FDAA35", "coral": "#FD6435", "logo_on_dark": "logo_on_dark.svg",
            "ok": "#1A7F52", "bad": "#C62828",
            "font": '"Inter", "Segoe UI", system-ui, -apple-system, sans-serif', "logo": "logo.svg",
            "show_name_with_logo": True}


def load():
    b = dict(DEFAULTS)
    path = os.path.join(BRAND_DIR, "brand.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            b.update({k: v for k, v in json.load(fh).items() if not k.startswith("_") and v not in (None, "")})
    return b


def logo_path(b=None, on_dark=False):
    b = b or load()
    names = ([b.get("logo_on_dark")] if on_dark else []) + [b.get("logo"), "logo.svg", "logo.png"]
    for name in names:
        p = os.path.join(BRAND_DIR, name or "")
        if name and os.path.isfile(p):
            return p
    return None


def logo_data_uri(b=None, on_dark=False):
    p = logo_path(b, on_dark)
    if not p:
        return None
    mime = mimetypes.guess_type(p)[0] or "image/png"
    with open(p, "rb") as fh:
        return f"data:{mime};base64,{base64.b64encode(fh.read()).decode()}"


def theme_css(b=None):
    """CSS custom properties shared by the settings and callback pages."""
    b = b or load()
    return f""":root{{--navy:{b['navy']};--navy-soft:{b['navy_soft']};--accent:{b['accent']};--accent-ink:{b['accent_ink']};
--accent-text:{b['accent_text']};--orange:{b['orange']};--coral:{b['coral']};
--ok:{b['ok']};--bad:{b['bad']};--font:{b['font']};
--bg:#f4f6fa;--card:#fff;--ink:#0f1b2d;--muted:#5a6678;--line:#dfe4ec;--field:#fff;--chip:#eaeef5;
--warn-bg:#fff4e0;--warn:#8a5300;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root{{--accent:{b['accent_on_dark']};--accent-ink:{b['navy']};--accent-text:{b['accent_on_dark']};
--bg:#0a1324;--card:#111d33;--ink:#e7ecf4;--muted:#9aa8bd;--line:#22324f;--field:#0c172b;--chip:#1b2a45;
--ok:#5fd09a;--bad:#ff8a80;--warn-bg:#2e2410;--warn:#f2c069;color-scheme:dark}}}}
"""


def locust_css(b=None):
    """Style layer over Locust's own web UI: header, primary buttons, focus colours and logo."""
    b = b or load()
    has_logo = bool(logo_path(b))
    logo = ("content:none;" if has_logo else
            f"content:'{b['name']}';font:700 18px/30px {b['font']};color:#fff;letter-spacing:.2px;")
    name_after = ""
    return f"""
header.MuiAppBar-root{{background:{b['navy']}!important;background-image:none!important;
box-shadow:inset 0 -3px 0 {b['accent']}!important}}
header.MuiAppBar-root a.MuiLink-root:first-of-type > svg{{display:none!important}}
header.MuiAppBar-root a.MuiLink-root:first-of-type{{display:inline-flex;align-items:center;gap:10px}}
.ob-logo{{height:30px;width:auto;display:block}}
.ob-name{{font:700 17px/28px {b['font']};color:#fff;letter-spacing:.2px;white-space:nowrap}}
header.MuiAppBar-root a.MuiLink-root:first-of-type::before{{display:inline-block;{logo}}}
{name_after}
.MuiButton-containedPrimary,.MuiButton-contained.MuiButton-colorPrimary{{background-color:{b['accent']}!important;
color:{b['accent_ink']}!important;box-shadow:none!important}}
.MuiButton-containedPrimary:hover,.MuiButton-contained.MuiButton-colorPrimary:hover{{filter:brightness(.92)}}
.MuiButton-outlinedPrimary,.MuiButton-textPrimary{{color:{b['accent_text']}!important;border-color:{b['accent']}!important}}
.Mui-focused .MuiOutlinedInput-notchedOutline{{border-color:{b['accent']}!important}}
.MuiInputLabel-root.Mui-focused,.MuiFormLabel-root.Mui-focused{{color:{b['accent_text']}!important}}
.MuiTab-root.Mui-selected{{color:{b['accent_text']}!important}} .MuiTabs-indicator{{background-color:{b['accent']}!important}}
.MuiCheckbox-root.Mui-checked,.MuiRadio-root.Mui-checked,.MuiSwitch-switchBase.Mui-checked{{color:{b['accent']}!important}}
body,.MuiTypography-root,.MuiButton-root,.MuiInputBase-root,.MuiTableCell-root{{font-family:{b['font']}!important}}
.MuiButton-root{{text-transform:none!important;font-weight:600!important;letter-spacing:.1px!important;border-radius:8px!important;
transition:filter .15s,background-color .15s,transform .05s!important}}
.MuiButton-root:active{{transform:translateY(1px)}}
.MuiButton-root.Mui-focusVisible{{outline:3px solid {b['accent']}55!important;outline-offset:2px}}
.MuiButton-root{{box-sizing:border-box;height:36px!important;min-height:36px!important;padding-top:0!important;padding-bottom:0!important;
font-size:13.5px!important;line-height:1!important}}
form .MuiButton-contained.MuiButton-colorPrimary,form button[type=submit]{{box-shadow:0 1px 2px rgba(11,30,63,.15),0 4px 14px {b['accent']}40!important}}
.MuiDialogActions-root,.MuiCardActions-root{{gap:8px}} .MuiDialogActions-root>:not(:first-of-type){{margin-left:0!important}}
.MuiOutlinedInput-root{{border-radius:8px!important}}
.MuiAccordion-root{{border-radius:10px!important;overflow:hidden;box-shadow:0 0 0 1px rgba(15,27,45,.08)!important}}
.MuiAccordion-root:before{{display:none}}
.MuiTab-root{{text-transform:none!important;font-weight:600!important;font-size:14px!important}}
.ob-hint{{margin:4px 14px 0;font:12px/1.4 {b['font']};color:#5a6678}}
.ob-hint.warn{{color:{b['bad']}}}
select.ob-select{{appearance:auto;background:transparent;border:0;outline:0;width:100%;cursor:pointer;font:inherit}}
#ob-credit{{position:fixed;left:16px;bottom:10px;font:12px {b['font']};color:#8a94a6;text-decoration:none;z-index:1}}
#ob-credit:hover{{color:{b['accent_text']}}}
main .MuiButton-outlined{{color:{b['accent_text']}!important;border-color:{b['accent']}!important}}
main a.MuiLink-root,.MuiTabPanel-root a,.MuiContainer-root a.MuiLink-root{{color:{b['accent_text']}!important}}
[data-mui-color-scheme=dark] main a.MuiLink-root,.MuiCssBaseline-root:is([data-theme=dark]) main a.MuiLink-root{{color:{b['accent_on_dark']}!important}}
a[href*="aka.ms/loadtesting"],a[href*="azure.microsoft"]{{display:none!important}}
/* one shared header button design: outlined; Stop is the only solid one */
header.MuiAppBar-root .MuiButton-root,#ob-settings-link,#ob-profiles-link{{box-sizing:border-box;height:36px!important;min-height:36px!important;margin:0!important;
min-width:0!important;padding:0 14px!important;border-radius:8px!important;font:600 13.5px/1 {b['font']}!important;
letter-spacing:.1px!important;text-transform:none!important;color:#fff!important;background:transparent!important;
border:1px solid rgba(255,255,255,.38)!important;box-shadow:none!important;display:inline-flex!important;align-items:center;
text-decoration:none;white-space:nowrap}}
header.MuiAppBar-root .MuiButton-root:hover,#ob-settings-link:hover,#ob-profiles-link:hover{{background:rgba(255,255,255,.1)!important;border-color:#fff!important}}
header.MuiAppBar-root .MuiButton-root.MuiButton-colorError{{background:{b['bad']}!important;border-color:{b['bad']}!important}}
header.MuiAppBar-root .MuiButton-root.MuiButton-colorError:hover{{filter:brightness(1.08)}}
header.MuiAppBar-root .MuiBox-root:has(> .MuiBox-root > .MuiButton-root):not(:has(.MuiTypography-root)){{gap:8px!important}}
#ob-nav{{display:flex;gap:8px;align-items:center;flex:none;margin:0 auto 0 4px}}
#ob-summary{{border:1px solid rgba(128,128,128,.35);border-radius:8px;padding:12px 16px;margin:4px 0 8px}}
#ob-summary dl{{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;margin:0 0 10px}}
#ob-summary dt{{opacity:.7}} #ob-summary dd{{margin:0;font-weight:600;overflow-wrap:anywhere}}
#ob-summary a{{color:{b.get('link', b['accent'])};font-weight:600;text-decoration:none}} #ob-summary a:hover{{text-decoration:underline}}
#ob-settings-link,#ob-profiles-link{{margin:0!important}}
header.MuiAppBar-root .MuiBox-root:has(> .MuiButton-root){{gap:8px!important}}
header.MuiAppBar-root .MuiToolbar-root>.MuiBox-root{{column-gap:20px!important}}
header.MuiAppBar-root .MuiToolbar-root>.MuiBox-root>.MuiBox-root:first-child{{column-gap:10px!important}}
header.MuiAppBar-root .MuiToolbar-root{{align-items:center!important}}
header.MuiAppBar-root .MuiToolbar-root>*,header.MuiAppBar-root .MuiToolbar-root .MuiBox-root{{align-self:center;align-items:center}}
#ob-settings-link,#ob-profiles-link{{flex:none!important;width:122px;justify-content:center;padding:0 10px!important}}
header.MuiAppBar-root .MuiBox-root .MuiButton-root{{min-width:72px!important;justify-content:center;padding:0 12px!important}}
header.MuiAppBar-root .MuiBox-root:has(> .MuiBox-root > .MuiButton-root){{margin-top:0!important;margin-bottom:0!important;padding-top:0!important;padding-bottom:0!important}}
"""

LOCUST_JS = """
(function () {
  var B = window.OB_BRAND || {};
  var tenants = null, chosen = null, tps = null, tpsTouched = false;   // "tenants" now holds test profiles
  fetch('/ob-profiles/api/profiles').then(function (r) { return r.json(); })
    .then(function (j) { tenants = j.profiles || []; enhance(); }).catch(function () { tenants = []; });

  function brandHeader() {
    var bar = document.querySelector('header.MuiAppBar-root a.MuiLink-root');
    if (!bar || document.getElementById('ob-settings-link')) return;
    if (B.logo) {
      bar.querySelectorAll('svg,img').forEach(function (n) { n.style.display = 'none'; });
      var img = document.createElement('img');
      img.className = 'ob-logo'; img.src = '/ob-brand/logo?on=dark'; img.alt = B.nameWithLogo ? '' : B.name;
      bar.appendChild(img);
      if (B.nameWithLogo) { var t = document.createElement('span'); t.className = 'ob-name'; t.textContent = B.name; bar.appendChild(t); }
    }
    var nav = document.createElement('div');   // own row: exactly 8 px between the two buttons
    nav.id = 'ob-nav';
    var a = document.createElement('a');
    a.id = 'ob-settings-link'; a.href = '/ob-tenants'; a.textContent = 'Tenant profiles';
    a.title = 'Financial institutions in scope: URLs, certificates and supported APIs';
    var pl = document.createElement('a');
    pl.id = 'ob-profiles-link'; pl.href = '/ob-profiles'; pl.textContent = 'Test profiles';
    pl.title = 'Load, APIs to call, tokens and test data';
    nav.appendChild(a); nav.appendChild(pl);
    bar.insertAdjacentElement('afterend', nav);
    if (!document.getElementById('ob-credit')) {
      var c = document.createElement('a');
      c.id = 'ob-credit'; c.href = 'https://locust.io'; c.target = '_blank'; c.rel = 'noopener';
      c.textContent = 'Load generation powered by Locust (MIT licence)';
      document.body.appendChild(c);
    }
  }

  function setReact(input, value) {
    var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(input, value); input.dispatchEvent(new Event('input', {bubbles: true}));
  }
  function cloneBox(ref, id, label) {
    var box = ref.closest('.MuiFormControl-root').cloneNode(true);
    box.id = id + '-box'; box.removeAttribute('title');
    var lab = box.querySelector('label'); if (lab) { lab.textContent = label; lab.setAttribute('for', id); }
    var leg = box.querySelector('legend span'); if (leg) leg.textContent = label;
    box.querySelectorAll('.MuiFormHelperText-root').forEach(function (n) { n.remove(); });
    return box;
  }
  function hint(box, text, warn) {
    var h = box.querySelector('.ob-hint');
    if (!h) { h = document.createElement('p'); h.className = 'ob-hint'; box.appendChild(h); }
    h.textContent = text; h.classList.toggle('warn', !!warn);
  }
  function totalText(users) {
    var r = parseFloat(tps), u = parseFloat(users);
    if (!(r > 0)) return 'Users pause 1–3 seconds between requests';
    if (!(u > 0)) return 'Per user';
    var total = Math.round(r * u * 10) / 10;
    return u + (u === 1 ? ' user' : ' users') + ' × ' + r + ' ≈ ' + total + ' requests per second in total';
  }
  function humanTime(v) {
    v = (v || '').trim().toLowerCase();
    if (!v) return 'until you press Stop';
    if (/^\\d+$/.test(v)) return v + ' s';
    return v.replace(/(\\d+)h/, '$1 h ').replace(/(\\d+)m/, '$1 min ').replace(/(\\d+)s/, '$1 s').trim();
  }
  function runSeconds(v) {
    v = (v || '').trim().toLowerCase();
    if (!v) return 0;
    if (/^\\d+$/.test(v)) return parseInt(v);
    var s = 0, m, re = /(\\d+)([hms])/g;
    while ((m = re.exec(v))) s += parseInt(m[1]) * {h: 3600, m: 60, s: 1}[m[2]];
    return s;
  }
  function humanSecs(s) {
    s = Math.round(s);
    if (s < 60) return s + ' s';
    var mm = Math.floor(s / 60), ss = s % 60;
    return mm + ' min' + (ss ? ' ' + ss + ' s' : '');
  }
  function summary(form, after, t) {
    var s = form.querySelector('#ob-summary');
    if (!s) { s = document.createElement('div'); s.id = 'ob-summary'; after.after(s); }
    var u = parseFloat(t.users) || 0, r = parseFloat(t.user_tps) || 0, peak = parseFloat(t.peak_rps) || 0,
        factor = parseFloat(t.stress_factor) || 2;
    var rows = [['Users', u + (u === 1 ? ' user' : ' users') + ', ramp up ' + t.spawn_rate + ' per second'],
                ['Requests per second', r > 0 ? r + ' per user, ≈ ' + Math.round(u * r * 10) / 10 + ' in total' : 'users pause 1–3 s between requests'],
                ['Run time', humanTime(t.run_time)]];
    if (peak > 0) rows.push(['Throughput target', Math.round(peak * factor * 10) / 10 + ' req/s (peak ' + peak + ' × ' + factor + ')']);
    var n = parseInt(t.pis_submits) || 0;
    if (n > 0) {
      if ((t.pis_submit_mode || '').indexOf('Spread') === 0) {
        var secs = runSeconds(t.run_time), every = (secs ? secs * 0.9 : 60) / n;
        rows.push(['Payments', n + ' submission' + (n === 1 ? '' : 's') + ', spread: first after ~' + humanSecs(every / 2) +
                   (n > 1 ? ', then every ' + humanSecs(every) : '')]);
      } else {
        rows.push(['Payments', n + ' submission' + (n === 1 ? '' : 's') + ' at the start, as fast as the load allows; then the full load on the other endpoints']);
      }
    }
    rows.push(['Host', t.rs_host || '–']);
    s.innerHTML = '';
    var dl = document.createElement('dl');
    rows.forEach(function (row) {
      var dt = document.createElement('dt'); dt.textContent = row[0];
      var dd = document.createElement('dd'); dd.textContent = row[1];
      dl.appendChild(dt); dl.appendChild(dd);
    });
    var a = document.createElement('a'); a.href = '/ob-profiles#' + t.id; a.textContent = 'Change these in the test profile →';
    s.appendChild(dl); s.appendChild(a);
  }
  function tenantById(id) { return (tenants || []).filter(function (t) { return t.id === id; })[0]; }

  function enhanceForm(form) {
    var inputs = form.querySelectorAll('input');
    if (inputs.length < 2) return;
    var users = inputs[0], ramp = inputs[1];
    var host = form.querySelector('input[name="host"]');
    var isStart = !!host;
    // Requests per second per user, right under "Ramp up"
    if (!form.querySelector('#ob-tps')) {
      var box = cloneBox(ramp, 'ob-tps', 'Requests per second per user');
      var inp = box.querySelector('input');
      inp.id = 'ob-tps'; inp.name = 'ob_user_tps'; inp.type = 'number'; inp.step = 'any'; inp.min = '0';
      inp.removeAttribute('required'); inp.value = tps == null ? '' : tps;
      inp.addEventListener('input', function () { tps = inp.value; tpsTouched = true; hint(box, totalText(users.value)); });
      ramp.closest('.MuiFormControl-root').after(box);
      users.addEventListener('input', function () { hint(box, totalText(users.value)); });
      hint(box, totalText(users.value));
    }
    // Tenant dropdown at the top of the start form
    if (isStart && tenants !== null && !form.querySelector('#ob-tenant')) {
      var tbox = cloneBox(users, 'ob-tenant', 'Test profile');
      var old = tbox.querySelector('input');
      var sel = document.createElement('select');
      sel.id = 'ob-tenant'; sel.name = 'ob_profile'; sel.className = old.className + ' ob-select';
      if (!tenants.length) {
        var o = document.createElement('option'); o.value = ''; o.textContent = 'No test profiles yet'; sel.appendChild(o);
      }
      tenants.forEach(function (t) {
        var o = document.createElement('option'); o.value = t.id;
        o.textContent = t.tenant_name + ' · ' + t.name + (t.ready ? '' : ' (not ready to run)'); sel.appendChild(o);
      });
      old.replaceWith(sel);
      users.closest('.MuiFormControl-root').before(tbox);
      var apply = function () {
        var t = tenantById(sel.value); chosen = sel.value;
        if (!t) { hint(tbox, 'Create a test profile first (Test profiles).', true); return; }
        setReact(users, t.users); setReact(ramp, t.spawn_rate);
        tps = t.user_tps; var f = form.querySelector('#ob-tps'); if (f) f.value = tps;
        if (host && t.rs_host) setReact(host, t.rs_host);
        // Start only: everything comes from the test profile, so show it instead of editable fields
        [users, ramp, f, host].forEach(function (i) { if (i) i.closest('.MuiFormControl-root').style.display = 'none'; });
        form.querySelectorAll('.MuiAccordion-root').forEach(function (a) { a.style.display = 'none'; });
        summary(form, tbox, t);
        var probs = t.issues || []; hint(tbox, !probs.length ? (t.areas.join(' + ') + ' · UK OB v' + t.version) : ('Not ready to run: ' + probs.join(' ')), probs.length > 0);
      };
      sel.value = chosen || (tenants.filter(function (t) { return t.ready; })[0] || tenants[0] || {}).id || '';
      sel.addEventListener('change', function () { tpsTouched = false; apply(); });
      apply();
    }
  }

  function enhance() {
    brandHeader();
    document.querySelectorAll('form').forEach(enhanceForm);
  }
  new MutationObserver(enhance).observe(document.documentElement, {childList: true, subtree: true});
  enhance();
})();
"""


def register(app):
    from flask import Response, request, send_file

    @app.route("/ob-brand/theme.css")
    def ob_brand_theme():
        return Response(theme_css(), mimetype="text/css")

    @app.route("/ob-brand/locust.css")
    def ob_brand_locust():
        return Response(locust_css(), mimetype="text/css")

    @app.route("/ob-brand/logo")
    def ob_brand_logo():
        p = logo_path(on_dark=request.args.get("on") == "dark")
        return send_file(p) if p else Response("", status=404)

    @app.route("/ob-brand/info")
    def ob_brand_info():
        b = load()
        return {"name": b["name"], "product": b["product"], "logo": bool(logo_path(b)),
                "name_with_logo": bool(b.get("show_name_with_logo", True))}

    @app.after_request
    def ob_brand_inject(resp):
        """Add the brand style layer to Locust's own pages."""
        if (resp.mimetype == "text/html" and not request.path.startswith("/ob-")
                and resp.status_code == 200 and not resp.direct_passthrough):
            html = resp.get_data(as_text=True)
            if "</head>" in html and "/ob-brand/locust.css" not in html:
                b = load()
                html = re.sub(r"<title>.*?</title>", f"<title>{b['name']} – {b['product']}</title>", html, count=1)
                html = html.replace("</head>", '<link rel="stylesheet" href="/ob-brand/locust.css"></head>', 1)
                info = json.dumps({"name": b["name"], "logo": bool(logo_path(b)),
                                   "nameWithLogo": bool(b.get("show_name_with_logo", True))})
                resp.set_data(html.replace("</body>", f"<script>window.OB_BRAND={info};{LOCUST_JS}</script></body>", 1))
        return resp
