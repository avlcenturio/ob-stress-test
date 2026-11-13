# Open Banking API Stress Test

**Passed.** Meets the Open Banking benchmarks for performance (every endpoint averages within 750 ms) and error rate (0.00% 5xx), with up to 100 concurrent users.

- Started: 19 May 2026, 14:54 UTC
- Duration: 5 min 12 s
- Peak users: 100
- Peak throughput: 64.9 req/s
- Total requests: 16,336
- Average throughput: 52.2 req/s
- Average response: 115 ms
- 5xx error rate: 0.00%

## Open Banking benchmarks (OBL Operational Guidelines v4.0.1, Availability and performance)

| Requirement | Benchmark | This run | Result |
|---|---|---|---|
| Availability | 99.5% uptime per quarter | Not in the scope of a stress test | ➖ Not in scope |
| Performance | average ≤ 750 ms per endpoint response | slowest endpoint averages 189 ms (GET /v1/accounts/[id]/transactions) | ✅ Met |
| Error rate | ≤ 0.5% 5xx responses | 0.00% (0 of 16,332 requests) | ✅ Met |

Availability is not in the scope of a stress test: it is measured over a longer period and should be reported in a dedicated availability report covering at least three months (OBL measures it per quarter).

## Endpoints (average ≤ 750 ms, 5xx ≤ 0.5%)

| Endpoint | Area | Requests | Average | p95 | 5xx | Status |
|---|---|---:|---:|---:|---:|---|
| GET /v1/accounts/[id]/transactions | AIS | 2,800 | 189 ms | 336 ms | 0 | ✅ |
| POST /v1/payments/sepa-credit-transfers | AIS | 1,400 | 165 ms | 294 ms | 0 | ✅ |
| POST /v1/consents | AIS | 933 | 130 ms | 231 ms | 0 | ✅ |
| POST /token (client_credentials) | AIS | 933 | 112 ms | 200 ms | 0 | ✅ |
| GET /v1/accounts | AIS | 2,800 | 112 ms | 200 ms | 0 | ✅ |
| GET /v1/accounts/[id]/balances | AIS | 2,800 | 106 ms | 189 ms | 0 | ✅ |
| DELETE /v1/consents/[id] | AIS | 933 | 83 ms | 147 ms | 0 | ✅ |
| GET /v1/payments/sepa-credit-transfers/[id]/status | AIS | 1,400 | 83 ms | 147 ms | 0 | ✅ |
| POST /v1/funds-confirmations | CoF | 1,400 | 77 ms | 136 ms | 0 | ✅ |
| GET /v1/consents/[id] | AIS | 933 | 71 ms | 126 ms | 0 | ✅ |
