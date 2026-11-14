# Ozone API – Open Banking Stress Test

Stress testing for UK Open Banking APIs (v3.1 and v4.0), built on [Locust](https://locust.io).
Describe each financial institution once, define reusable test profiles, generate tokens through
the real consent flow, run the load, and get a report judged against the Open Banking Limited
(OBL) performance benchmarks, all from the browser.

- **Tenant profiles:** each financial institution in scope, set up from its OpenID discovery document
- **Test profiles:** load, the APIs to call and their weights, tokens and test data, per test scenario
- **Consent flow built in:** create consents, approve them in the browser, tokens are captured and renewed automatically
- **AIS, PIS and CoF:** account information, domestic payments and confirmation of funds
- **Reports:** Passed/Failed against the OBL benchmarks, charts, per-endpoint results, PDF download
- **Run history:** every run is kept under its test profile, with summary and downloads
- **Runs anywhere:** Python on your machine, or a Docker image for a laptop or a server (login + HTTPS)

---

## Contents

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Tenant profiles](#tenant-profiles)
- [Test profiles](#test-profiles)
- [Running a test](#running-a-test)
- [Reports and the Passed/Failed verdict](#reports-and-the-passedfailed-verdict)
- [Deployment](#deployment)
- [Configuration reference](#configuration-reference)
- [Project structure](#project-structure)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Third-party software](#third-party-software)

---

## Quick start

### With Docker (recommended)

```bash
docker compose up -d --build
```

Open **http://localhost:8089**. By default it is reachable from your own machine only.

### With Python

Requires Python 3.10 or newer.

```bash
./setup.sh                          # creates .venv and installs everything
source .venv/bin/activate
locust -f locustfile.py
```

Open **http://localhost:8089**. For one-click PDF reports also run
`pip install playwright && playwright install --with-deps chromium`.

### First steps

1. **Tenant profiles → New tenant profile:** enter the financial institution's discovery URL and follow the steps.
2. **Test profiles → New test profile:** choose the tenant profile, the APIs, the load and how tokens are supplied.
3. **Locust's start page:** choose the test profile and press **Start**.
4. **Download Data** or **Test profiles → Runs:** open the report.

---

## How it works

```
Tenant profile  ──►  Test profile  ──►  Run  ──►  Report
(the financial        (what to test,       (Locust)    (OBL benchmarks,
 institution)          how hard, tokens)                 charts, PDF)
```

| | Holds | Changes |
|---|---|---|
| **Tenant profile** | URLs, client registration, certificates, supported APIs and standard version | Rarely: set up once per financial institution |
| **Test profile** | Load, APIs to call and their weights, tokens, test data | Often: one per test scenario |
| **Run** | Results, summary and reports of one test | Every time you press Start |

---

## Tenant profiles

Open **Tenant profiles** in the header, or `/ob-tenants`. A tenant profile describes a financial
institution in scope of the stress testing. **New tenant profile** walks through:

| Step | What you set |
|---|---|
| **1. Tenant** | Name and OpenID discovery URL. *Fetch discovery document* fills in everything the financial institution publishes; if its server uses self-signed certificates, you're offered *Accept self-signed certificates and retry* |
| **2. URLs** | Resource server (suggested), issuer, authorisation and token endpoints, client ID, client authentication, redirect URL, response type |
| **3. Certificates** | TLS certificate verification (and CA file), transport certificate and key (mTLS), signing key and key ID, signature issuer. Checked on upload: format, expiry, key matches certificate |
| **4. Supported APIs** | AIS, PIS and/or CoF, and the UK Open Banking version (3.1 or 4.0) |
| **5. AIS / PIS / CoF APIs** | The endpoints this financial institution supports |
| **6. Review and save** | Summary, anything still missing, *Test connection* |

**TLS certificate verification:** *Verify against trusted CAs* (default), *Accept self-signed
certificates* (for servers whose certificates can't be traced to a publicly trusted root CA) or
*Verify with a custom CA file*.

**Client authentication** at the token endpoint:

| Method | Needs |
|---|---|
| `tls_client_auth` | Client ID + transport certificate |
| `client_secret_basic` | Client ID + client secret, sent as `Authorization: Basic base64(client_id:client_secret)` |
| `client_secret_post` | Client ID + client secret in the request body |

**Redirect URL:** defaults to this app's own address (`…/ob-callback`), so consent approvals are
captured automatically. It must be registered for your client at the financial institution.

Tenant profiles can be exported and imported as YAML, optionally including tokens and certificates.

---

## Test profiles

Open **Test profiles** in the header, or `/ob-profiles`. A test profile runs against one tenant
profile and holds everything that changes from test to test.

| Step | What you set |
|---|---|
| **1. Profile** | Name and tenant profile |
| **2. Scope** | The areas to test (only those the tenant profile supports) and, for each, the APIs to call with their weights |
| **3. Load** | Users, ramp up, requests per second per user (the total is shown live), run time (e.g. 10m, 1h30m; empty = until Stop), payment submissions (at the start or spread), peak-hour requests per second of the existing digital channel and the stress factor (the throughput target is shown live), mix of users between areas |
| **4. AIS / PIS / CoF tokens** | How tokens are supplied, plus the test data |
| **5. Review and save** | Summary and anything still missing |

### Supplying tokens

- **Paste tokens:** use tokens you already have; consent steps are skipped.
- **Consent flow:** the test data (permissions, payment details) appears first, because consents
  are built from it. Then:
  1. **Create** one or more consents.
  2. Click **Authorise ↗**, log in as the test user and approve.
  3. The financial institution redirects back to this app and the consent turns **Ready** by itself.
     If the redirect doesn't arrive, use **Didn't come back?** and paste the redirect URL or
     just the authorisation code.

Tokens are renewed with the refresh token when they expire, also during a run. Each authorised
payment consent is used for exactly one payment.

### Payments (PIS)

All UK Open Banking payment types except VRP: domestic, domestic scheduled, domestic standing
orders, international, international scheduled, international standing orders and file payments.

- **Submitting a payment happens exactly once per authorised consent** (that's how payment consents
  work), so it has no weight. *Load → Payment submissions* chooses when:
  - **At the start, as fast as possible** (default, for stress tests): all payments are submitted
    first, then the full load goes to the other endpoints.
  - **Spread over the run time** (for long runs): evenly over the run (over the first minute if no
    run time is set).

  The start page summary shows what will happen, e.g. *3 submissions at the start*. Funds checks only
  work before a consent's payment is submitted, so with *At the start* they stop almost immediately.
- **Everything else is repeatable and shares the remaining requests per second by weight:** creating
  consents, reading consents and payment files, funds checks (until the consent's payment is
  submitted), reading payments and standing orders, payment details and report files.
- Calls that can't run yet (e.g. reading payments before the first submission) give their share to
  the others; if nothing can run, the test records a notice explaining why instead of going quiet.
- **Consents:** in *PIS tokens*, choose the payment type next to *Create*. Pasted consents use lines of
  `ConsentId,AccessToken,type` (type optional, domestic by default).
- **Request templates:** each payment type has an editable JSON template for its consent request,
  pre-filled for UK OB v3.1 or v4.0, with placeholders such as `{{Amount}}`, `{{CreditorId}}`,
  `{{ExecutionDateTime}}`, `{{Frequency}}` and `{{IntlCurrency}}`. Edit one only if your financial
  institution needs something different (e.g. its supported standing order frequency codes).
- **File payments:** Open Banking JSON (`UK.OBIE.PaymentInitiation.3.1`) or ISO 20022 pain.001
  (`UK.OBIE.pain.001.001.08`); the file is generated and uploaded when the consent is created, with
  its hash, number of transactions and control sum calculated automatically.

### Run history

Each profile card shows its latest run. **Runs** lists every run of the profile, newest first, with
a summary (duration, users, requests, requests per second, errors, p95, Passed/Failed) and downloads
for the **Report**, **Summary** and **CSV** data.

---

## Running a test

On Locust's start page, choose the **Test profile** and press **Start**. Everything else comes from the
profile: users, ramp up, requests per second per user, run time, throughput target and host are shown
as a summary, with a link to change them in the test profile. The test stops by itself after the
profile's run time (or runs until **Stop** if the run time is empty). **Edit** during a run can still
change the users and the requests per second; they apply within seconds.

**Without the web UI:**

```bash
locust -f locustfile.py --headless -u 20 -r 2 -t 10m --ob-profile "Peak AIS"
# with Docker:
docker compose run --rm ob-stress-test --headless -u 20 -r 2 -t 10m --ob-profile "Peak AIS"
```

Users, ramp up and run time come from the profile; `-u`, `-r` and `-t` on the command line override them, and `--ob-user-tps` overrides the requests per second per user.

**Choosing the numbers:** a single user waits for each response before sending the next request, so
it can manage at most about `1000 / average response time (ms)` requests per second. For more load,
add users rather than raising the per-user rate.

---

## Reports and the Passed/Failed verdict

Every run is saved to `results/<date_time>/`. Reports are available from **Download Data** (current
run) and **Test profiles → Runs** (every run). **Download PDF** at the top of the report downloads
an A4 PDF when Playwright is installed (always in the Docker image); otherwise it opens the
browser's print dialog, where *Save as PDF* gives the same result.

The report contains:

- The tenant profile, test profile, standard, areas tested and every URL from the discovery document
- The verdict and key figures, plus a note when many requests got errors other than 5xx (e.g. 429 rate limiting)
- **Benchmarks:** throughput and users, performance, error rate, availability
- *What we found*: only when the run fails, naming what failed and where
- **1. Throughput and users:** requests per second and users, with the throughput target line
- **2. Performance:** average response time per endpoint over time, average per endpoint, where the time goes (connection setup, network, server, download), network latency over time, percentiles (for engineers)
- **3. Error rate:** 5xx errors against the benchmark, all other errors by type, and the errors-by-type table
- **4. Endpoints summary:** requests, average, p95, 5xx and 4xx per endpoint

### How response times are measured

Each response time runs from sending the request until the last byte of the response arrives, as seen
from the machine running the test. The report splits it per request:

| Part | Measured how |
|---|---|
| **Connection setup** | DNS, TCP and TLS (mTLS) handshake, timed whenever a new connection is opened; reused connections skip it |
| **Network round trip** | A TCP connect to the resource server every 5 seconds during the run |
| **Server processing (estimated)** | Time until the first byte of the response, minus the network round trip |
| **Download** | From the first byte until the last byte of the response body |

The financial institution's exact internal time can only come from its side (some APIs return a
`Server-Timing` header; each request also carries an `x-fapi-interaction-id` it can look up).

### Open Banking benchmarks

From the OBL Operational Guidelines v4.0.1, *Availability and performance*:

| Requirement | Benchmark | In the stress test report |
|---|---|---|
| **Throughput and users** | ≥ peak-hour requests per second of the existing digital channel × stress factor (2) | Sustained requests per second once all users are running; *Not assessed* if no peak-hour figure is set |
| **Performance** | Average response ≤ 750 ms per endpoint (AIS: per page of up to 100 records) | Every API endpoint's average must meet it |
| **Error rate** | ≤ 0.5% 5xx responses | 5xx only; 4xx are attributable to the TPP |
| **Availability** | 99.5% uptime per quarter | **Not in scope.** Availability is measured over a longer period and belongs in a dedicated availability report covering at least three months |

A run **passes** when throughput (if a target is set), performance and error rate are all met. The throughput target is the busiest hour of the existing digital channel (PSU interface) multiplied by the stress factor, 2 by default, to add the stress element; within 2% counts as met. Token renewals and settings lines
are shown in the tables but don't count towards the verdict. Percentiles (median, p95, p99) are
shown for information; Locust approximates them by rounding to 10 ms between 100 ms and 1 s, and to
100 ms above 1 s.

**Report from the command line:**

```bash
python report.py results/2026-11-13_101500/run
python report.py results/2026-11-13_101500/run --avg-ms 750 --max-5xx-pct 0.5
```

---

## Deployment

### Local Docker

```bash
docker compose up -d --build          # start (data is kept in the ob-data volume)
docker compose logs -f ob-stress-test # follow the log
docker compose down                   # stop; data is kept
```

### Server: login and HTTPS

1. Put the server's TLS certificate and key in `tls/server.pem` and `tls/server.key`.
2. Create `.env` (see `.env.example`):
   ```
   OB_BIND=0.0.0.0
   OB_WEB_USER=admin
   OB_WEB_PASSWORD=<a strong password>
   ```
3. Start:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.server.yml up -d --build
   ```
4. Open `https://<server>:8089`. For automatic consent capture, set the tenant profile's redirect
   URL to `https://<server>:8089/ob-callback` and register it with the financial institution.

### From Docker Hub with GitHub Actions

`.github/workflows/docker-publish.yml` builds the image for x86 and ARM and pushes it to Docker Hub:

| Event | Image tags |
|---|---|
| Push to `main` | `:main` |
| Tag `v1.2.3` | `:1.2.3`, `:1.2`, `:latest`, then deploys to the server if switched on |

**Repository settings** (*Settings → Secrets and variables → Actions*):

| Name | Type | Value |
|---|---|---|
| `DOCKERHUB_REPO` | variable | e.g. `yourname/ob-stress-test` |
| `DOCKERHUB_USERNAME` | secret | Docker Hub user |
| `DOCKERHUB_TOKEN` | secret | Docker Hub access token (read & write) |
| `DEPLOY_ENABLED` | variable | `true` to deploy version tags |
| `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PATH` | secrets | Server address, SSH user and key, and the folder with `docker-compose.deploy.yml` |

**On the server**, a folder with `docker-compose.deploy.yml`, `.env` (`OB_IMAGE`, `OB_WEB_USER`,
`OB_WEB_PASSWORD`) and `tls/` is enough; no source code is needed.

**Release:** `git tag v1.0.0 && git push --tags`. Add required reviewers to the `production`
environment to approve each deployment.

### Moving an image without a registry

```bash
docker save ob-stress-test | gzip > ob-stress-test.tar.gz
docker load < ob-stress-test.tar.gz      # on the server
```

Apple Silicon builds ARM images; for an x86 server use
`docker buildx build --platform linux/amd64 -t ob-stress-test --load .`

### Moving existing data into Docker

```bash
docker compose cp ob_tenants.json ob-stress-test:/data/
docker compose cp ob_profiles.json ob-stress-test:/data/
docker compose cp ob_tokens.json ob-stress-test:/data/
docker compose cp certs ob-stress-test:/data/
docker compose restart
```

Certificate paths are resolved inside the data folder automatically.

---

## Configuration reference

### Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `OB_WEB_USER`, `OB_WEB_PASSWORD` | app | When both are set, every page asks for this login (`/ob-health` stays open) |
| `OB_BIND` | compose | Address the port binds to: `127.0.0.1` (default, this machine only) or `0.0.0.0` |
| `OB_IMAGE` | `docker-compose.deploy.yml` | Image to run, e.g. `yourname/ob-stress-test:1.2.3` |
| `OB_HEALTH_SCHEME` | health check | `https` when Locust serves HTTPS (set by the server compose files) |
| `OB_PROFILE` | headless runs | Test profile to run, same as `--ob-profile` |
| `OB_USER_TPS` | headless runs | Requests per second per user, same as `--ob-user-tps` |

### Command-line options

| Option | |
|---|---|
| `--ob-profile NAME` | Test profile to run (name or id); default: the first ready profile |
| `--ob-user-tps N` | Requests per second per user; overrides the profile |
| `--tls-cert`, `--tls-key` | Serve the web UI over HTTPS (Locust options) |

### Data files

All in the working folder (`/data` in Docker), private and ignored by git:

| File / folder | Contents |
|---|---|
| `ob_tenants.json` | Tenant profiles |
| `ob_profiles.json` | Test profiles |
| `ob_tokens.json` | Tokens and pending consents, per test profile |
| `certs/<tenant>/` | Uploaded certificates and keys |
| `results/<date_time>/` | One folder per run: CSVs, `context.json`, `run.json`, reports |

---

## Project structure

| File | Purpose |
|---|---|
| `locustfile.py` | The load test: users, tasks, token renewal, run recording, report links |
| `ob_core.py` | Tenant profiles, test profiles, certificates, discovery, consent flow, token store, run history |
| `ob_web.py` | Web routes for the Tenant profiles and Test profiles pages, consent callback and downloads |
| `ob_brand.py` | Branding and the style layer over Locust's pages, plus the start page fields |
| `ob_tenants.html`, `ob_profiles.html`, `ob_callback.html` | The pages |
| `report.py` | The stress test report (HTML and Markdown) |
| `brand/` | `brand.json` (name, colours, font) and the logos |
| `Dockerfile`, `docker-compose*.yml`, `healthcheck.py` | Docker image and deployment |
| `.github/workflows/docker-publish.yml` | Build, publish to Docker Hub and deploy |
| `exemption_report.py`, `exemption_config.yaml` | Optional PSD2 fallback exemption report (command line only) |
| `examples/`, `example-report.html` | Sample run data and a sample report |

### Branding

`brand/brand.json` sets the name, colours and font used on every page and in the reports.
`brand/logo.svg` is used on light backgrounds, `brand/logo_on_dark.svg` on the navy headers.

---

## Security

- **The web UI holds tokens and keys.** Keep it on `127.0.0.1` locally; on a server use the login
  (`OB_WEB_USER` / `OB_WEB_PASSWORD`) and HTTPS.
- **Data stays out of git and out of the image.** `.gitignore` and `.dockerignore` exclude tokens,
  profiles, certificates, keys and results.
- **Exports with secrets** (*Include tokens and certificate/key files*) are complete copies: treat
  them like passwords.
- **Accept self-signed certificates** only for environments you control.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| *The server's certificate couldn't be verified* | Self-signed certificates: set *TLS certificate verification* to *Accept self-signed certificates*, or upload the CA file |
| `401: token expired or invalid` | Paste fresh tokens, or authorise a new consent; consent-flow tokens renew automatically while their refresh token is valid |
| `invalid_client` at the token endpoint | Wrong client ID, secret or authentication method (Tenant profile → URLs), or the transport certificate isn't the registered one |
| A consent stays *Waiting for approval* | The redirect URL isn't this app's `/ob-callback` or isn't registered: use *Didn't come back?* and paste the redirect URL or code |
| A test profile shows *Not ready to run* | Hover over it, or open *Review and save*: it lists exactly what is missing |
| Download PDF opens the print dialog | Playwright isn't installed: choose *Save as PDF*, or install Playwright (it's included in the Docker image) |
| Requests per second stay below users × rate | Responses are too slow for the per-user rate: add users, or the financial institution is slowing down under load |

---

## Third-party software

Load generation uses [Locust](https://locust.io), MIT licence, copyright (c) 2009–2026 Carl
Byström, Jonatan Heyman and Lars Holmberg. Its licence text ships with the installed package; keep
it with any copy of this tool you distribute.
