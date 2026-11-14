"""Routes for the Tenants page, served by Locust's own web server."""
import json
import os

from flask import Response, jsonify, request

import ob_core as core

HERE = os.path.dirname(os.path.abspath(__file__))


def register(app, environment):
    def page(name):
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            return Response(fh.read(), mimetype="text/html")

    def ok(**kw):
        return jsonify({"ok": True, **kw})

    def fail(msg, status=400):
        return jsonify({"ok": False, "error": str(msg)}), status

    def tenant_payload(tid):
        t = core.get_tenant(tid)
        return {"id": tid, "values": t, "files": core.file_status(t),
                "issues": core.validate_tenant({**t, "_id": tid})}

    @app.route("/ob-tenants")
    def ob_tenants_page():
        return page("ob_tenants.html")

    @app.route("/ob-settings")  # old address
    def ob_settings_redirect():
        return Response("", status=302, headers={"Location": "/ob-tenants"})

    @app.route("/ob-callback")
    def ob_callback_page():
        return page("ob_callback.html")

    @app.route("/ob-tenants/api/meta")
    def ob_meta():
        web_host = getattr(environment.parsed_options, "web_host", "") if environment.parsed_options else ""
        return ok(schema=core.SCHEMA, steps=core.STEPS, endpoints=core.ENDPOINTS, areas=core.AREAS,
                  open_to_network=web_host in ("", "*", "0.0.0.0", "::"),
                  callback=request.host_url.rstrip("/") + "/ob-callback")

    @app.route("/ob-tenants/api/tenants", methods=["GET", "POST"])
    def ob_tenant_list():
        if request.method == "POST":  # create
            body = request.get_json(force=True) or {}
            tid, _ = core.save_tenant(None, {**body.get("values", {}),
                                             **({"discovery": body["discovery"]} if "discovery" in body else {})})
            return ok(**tenant_payload(tid))
        return ok(tenants=core.list_tenants())

    @app.route("/ob-tenants/api/tenants/<tid>", methods=["GET", "PUT", "DELETE"])
    def ob_tenant(tid):
        try:
            if request.method == "DELETE":
                core.delete_tenant(tid)
                return ok(tenants=core.list_tenants())
            if request.method == "PUT":
                body = request.get_json(force=True) or {}
                core.save_tenant(tid, {**body.get("values", {}),
                                       **({"complete": body["complete"]} if "complete" in body else {}),
                                       **({"discovery": body["discovery"]} if "discovery" in body else {})})
            return ok(**tenant_payload(tid))
        except KeyError as e:
            return fail(e, 404)

    @app.route("/ob-tenants/api/discover", methods=["POST"])
    def ob_discover():
        body = request.get_json(force=True) or {}
        try:
            found, fill = core.fetch_discovery(body.get("url", ""), body.get("ca_check", "Yes"), body.get("ca_file", ""))
        except ValueError as e:
            return fail(e)
        return ok(found=found, fill=fill)

    @app.route("/ob-tenants/api/tenants/<tid>/upload/<key>", methods=["POST"])
    def ob_upload(tid, key):
        f = request.files.get("file")
        if not f:
            return fail("No file received")
        try:
            info = core.save_upload(tid, key, f.read())
        except ValueError as e:
            return fail(e)
        return ok(info=info, **tenant_payload(tid))

    @app.route("/ob-tenants/api/tenants/<tid>/test", methods=["POST"])
    def ob_test(tid):
        return ok(results=[{"ok": a, "message": b} for a, b in core.test_connection(tid)])

    @app.route("/ob-tenants/api/tenants/<tid>/export")
    def ob_export(tid):
        secrets_ = request.args.get("secrets") == "1"
        name = core.slug(core.get_tenant(tid).get("name")) + ("-with-secrets" if secrets_ else "") + ".yaml"
        return Response(core.export_tenant(tid, secrets_), mimetype="application/x-yaml",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.route("/ob-tenants/api/import", methods=["POST"])
    def ob_import():
        try:
            tid, notes = core.import_tenant((request.get_json(force=True) or {}).get("text", ""))
        except Exception as e:  # noqa: BLE001
            return fail(f"Could not read that config: {e}")
        return ok(notes=notes, **tenant_payload(tid))

    # ------------------------------------------------- authorisation callback ----
    @app.route("/ob-tenants/api/complete", methods=["POST"])
    def ob_complete():
        body = request.get_json(force=True) or {}
        try:
            result = core.complete_authorisation(body.get("url") or body.get("code", ""), body.get("state"))
        except Exception as e:  # noqa: BLE001
            return fail(e)
        return ok(result=result, tokens=core.token_overview(result["profile"]))

    @app.route("/ob-profiles/api/complete", methods=["POST"])
    def ob_profile_complete():
        return ob_complete()

    @app.route("/ob-settings/api/complete", methods=["POST"])  # older callback pages
    def ob_complete_old():
        return ob_complete()

    # ----------------------------------------------------------- test profiles ----
    def profile_payload(pid):
        p = core.get_profile(pid)
        try:
            issues = core.validate_profile(core.get_run(pid))
        except KeyError as e:
            issues = [str(e).strip("'\"")]
        tenant_ids = {x["id"] for x in core.list_tenants()}
        t = core.get_tenant(p["tenant"]) if p.get("tenant") in tenant_ids else None
        return {"id": pid, "values": p, "tokens": core.token_overview(pid), "issues": issues,
                "tenant": ({"id": p["tenant"], "name": t.get("name"), "rs_host": t.get("rs_host"), "version": t.get("version"),
                            "redirect_uri": t.get("redirect_uri", ""),
                            "areas": [a for a in core.AREAS if t.get("test_" + a.lower()) == "Yes"],
                            "supported": {a: [k for k in (t.get(a.lower() + "_endpoints") or "").split(",") if k]
                                          for a in core.AREAS}} if t else None)}

    @app.route("/ob-profiles")
    def ob_profiles_page():
        return page("ob_profiles.html")

    @app.route("/ob-profiles/api/meta")
    def ob_profiles_meta():
        return ok(schema=core.PROFILE_SCHEMA, steps=core.PROFILE_STEPS, endpoints=core.ENDPOINTS, areas=core.AREAS,
                  tenants=core.list_tenants(), callback=request.host_url.rstrip("/") + "/ob-callback",
                  templates=core.standard_templates(), payment_types=core.PAYMENT_TYPES)

    def _profile_body():
        body = request.get_json(force=True) or {}
        return {**body.get("values", {}), **({"weights": body["weights"]} if "weights" in body else {})}

    @app.route("/ob-profiles/api/profiles", methods=["GET", "POST"])
    def ob_profile_list():
        if request.method == "POST":
            pid, _ = core.save_profile(None, _profile_body())
            return ok(**profile_payload(pid))
        return ok(profiles=core.list_profiles())

    @app.route("/ob-profiles/api/profiles/<pid>", methods=["GET", "PUT", "DELETE"])
    def ob_profile(pid):
        try:
            if request.method == "DELETE":
                core.delete_profile(pid)
                return ok(profiles=core.list_profiles())
            if request.method == "PUT":
                core.save_profile(pid, _profile_body())
            return ok(**profile_payload(pid))
        except KeyError as e:
            return fail(e, 404)

    @app.route("/ob-profiles/api/profiles/<pid>/consents", methods=["POST"])
    def ob_profile_consents(pid):
        body = request.get_json(force=True) or {}
        try:
            created = core.create_consents(pid, body.get("area", "AIS"), body.get("count", 1), body.get("ptype"),
                                           body.get("role") or "payment")
        except Exception as e:  # noqa: BLE001
            return fail(e)
        return ok(created=created, tokens=core.token_overview(pid))

    @app.route("/ob-profiles/api/profiles/<pid>/tokens", methods=["GET", "POST"])
    def ob_profile_tokens(pid):
        if request.method == "POST":
            body = request.get_json(force=True) or {}
            if body.get("role"):
                core.set_consent_role(pid, body.get("consent_id"), body["role"])
            else:
                core.remove_tokens(pid, body.get("area"), body.get("consent_id"))
        return ok(tokens=core.token_overview(pid))

    @app.route("/ob-profiles/api/profiles/<pid>/export")
    def ob_profile_export(pid):
        secrets_ = request.args.get("secrets") == "1"
        name = core.slug(core.get_profile(pid).get("name")) + ("-with-secrets" if secrets_ else "") + ".yaml"
        return Response(core.export_profile(pid, secrets_), mimetype="application/x-yaml",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.route("/ob-profiles/api/import", methods=["POST"])
    def ob_profile_import():
        try:
            pid, notes = core.import_profile((request.get_json(force=True) or {}).get("text", ""))
        except Exception as e:  # noqa: BLE001
            return fail(f"Could not read that profile: {e}")
        return ok(notes=notes, **profile_payload(pid))


    # ------------------------------------------------------------- run history ----
    import importlib.util
    import io
    import subprocess
    import sys
    import zipfile
    from flask import send_file

    exemption_config = os.getenv("OB_EXEMPTION_CONFIG", os.path.join(os.getcwd(), "exemption_config.yaml"))
    has_pdf = importlib.util.find_spec("playwright") is not None

    @app.route("/ob-profiles/api/profiles/<pid>/runs")
    def ob_profile_runs(pid):
        return ok(runs=core.list_runs(pid), has_pdf=has_pdf)

    @app.route("/ob-runs/<run_id>/<kind>")
    def ob_run_file(run_id, kind):
        try:
            d = core.run_dir(run_id)
        except KeyError:
            return Response("Unknown run", status=404, mimetype="text/plain")
        prefix = os.path.join(d, "run")
        info = {}
        try:
            info = json.load(open(os.path.join(d, "run.json")))
        except Exception:  # noqa: BLE001
            pass
        label = " – ".join(x for x in (info.get("tenant_name"), info.get("profile_name")) if x)
        if kind == "csv":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(os.listdir(d)):
                    if f.endswith(".csv"):
                        z.write(os.path.join(d, f), f)
            buf.seek(0)
            return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"run-{run_id}-data.zip")
        if kind in ("report", "summary"):
            out = f"{prefix}_report.html" if kind == "report" else f"{prefix}_summary.md"
            if not os.path.exists(out):
                subprocess.run([sys.executable, os.path.join(HERE, "report.py"), prefix, "--env", label or run_id],
                               cwd=HERE, capture_output=True, text=True)
        elif kind in ("exemption", "exemption-pdf"):
            if not os.path.exists(exemption_config):
                return Response(f"Exemption config not found: {exemption_config}", mimetype="text/plain")
            out = f"{prefix}_exemption_report." + ("pdf" if kind == "exemption-pdf" else "html")
            if not os.path.exists(out):
                cmd = [sys.executable, os.path.join(HERE, "exemption_report.py"), prefix, "--config", exemption_config]
                if kind == "exemption-pdf":
                    cmd.append("--pdf")
                subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
        else:
            return Response("Unknown report", status=404, mimetype="text/plain")
        if not os.path.exists(out):
            return Response("The report could not be created for this run.", mimetype="text/plain")
        return send_file(out, as_attachment=kind in ("summary", "exemption-pdf"),
                         download_name=f"run-{run_id}-{os.path.basename(out)[4:]}")

    @app.route("/ob-runs/<run_id>/report-pdf", methods=["GET", "HEAD"])
    def ob_run_pdf(run_id):
        if not has_pdf:
            return Response("PDF rendering needs Playwright: pip install playwright && playwright install chromium",
                            status=501, mimetype="text/plain")
        try:
            d = core.run_dir(run_id)
        except KeyError:
            return Response("Unknown run", status=404, mimetype="text/plain")
        if request.method == "HEAD":
            return Response("", status=200)
        prefix = os.path.join(d, "run")
        html_path, pdf_path = f"{prefix}_report.html", f"{prefix}_report.pdf"
        live = not os.path.exists(os.path.join(d, "run.json"))   # still running: rebuild from the latest numbers
        if live or not os.path.exists(html_path):
            subprocess.run([sys.executable, os.path.join(HERE, "report.py"), prefix], cwd=HERE, capture_output=True, text=True)
        if live or not os.path.exists(pdf_path) or os.path.getmtime(pdf_path) < os.path.getmtime(html_path):
            script = ("import sys\nfrom playwright.sync_api import sync_playwright\n"
                      "with sync_playwright() as p:\n"
                      "    b = p.chromium.launch(); pg = b.new_page(); pg.goto('file://' + sys.argv[1])\n"
                      "    pg.emulate_media(media='print')\n"
                      "    pg.pdf(path=sys.argv[2], format='A4', print_background=True,\n"
                      "           margin={'top': '14mm', 'bottom': '14mm', 'left': '12mm', 'right': '12mm'})\n"
                      "    b.close()\n")
            proc = subprocess.run([sys.executable, "-c", script, html_path, pdf_path], capture_output=True, text=True,
                                  timeout=120)
            if proc.returncode != 0 or not os.path.exists(pdf_path):
                return Response("The PDF could not be created: " + (proc.stderr or "")[-300:], status=500,
                                mimetype="text/plain")
        return send_file(pdf_path, mimetype="application/pdf", as_attachment=True,
                         download_name=f"open-banking-api-stress-test-{run_id}.pdf")

    @app.route("/ob-runs/<run_id>", methods=["DELETE"])
    def ob_run_delete(run_id):
        try:
            core.delete_run(run_id)
        except KeyError as e:
            return fail(e, 404)
        return ok()
