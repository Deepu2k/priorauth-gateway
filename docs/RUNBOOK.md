# Runbook

How to tell what is wrong with PriorAuth Gateway using only the logs, the dashboard and the API.

Where to look (with `docker compose up`):

| What                        | Where                                                         |
| --------------------------- | ------------------------------------------------------------- |
| One request's full story    | `GET /requests/{id}/history`                                   |
| Logs for one request        | `docker compose logs api worker \| grep <request id>`          |
| Dashboard                   | Grafana → PriorAuth Gateway (http://localhost:3000)            |
| One request's trace         | Jaeger (http://localhost:16686), search by `trace_id` from a log line |
| Queues                      | RabbitMQ UI (http://localhost:15672) → Queues                  |
| Dead letters                | `GET /admin/dlq` (admin token)                                 |

Every log line is JSON with `service`, `request_id`, `attempt` and `trace_id`, so one `grep` on a
request id gives its whole path through the API and the worker.

---

## Drill: the health plan goes down

This is the failure I caused on purpose, and how I found it.

### What I broke

```bash
docker compose stop mock-payer
```

Then I submitted the sample request (`scripts/sample_request.json`).

### Symptom

The request never reached a decision. `GET /requests/{id}` a second after submitting:

```json
{ "status": "sent", "attempts": 2, "lastError": "health plan unreachable (ConnectError)" }
```

and a few seconds later:

```json
{ "status": "failed", "attempts": 3, "lastError": "health plan unreachable (ConnectError)" }
```

### Finding the cause from the dashboard

- **Health plan calls by outcome**: every call is `unreachable`; no `approved` or `denied`.
- **Retries scheduled and dead-lettered**: retries rise, then `dead-lettered: retries_exhausted`.
- **Dead letter queue** stat turns red (above 0).
- **Requests in and out**: `received` keeps going, decisions drop to zero. So the API is fine and the
  problem is downstream of the worker.

The same numbers from the worker's metrics endpoint (`curl localhost:9100/metrics`) after one request:

```
priorauth_payer_attempts_total{outcome="unreachable"} 3.0
priorauth_retries_scheduled_total 2.0
priorauth_dead_lettered_total{reason="retries_exhausted"} 1.0
priorauth_requests_failed_total{reason="retries_exhausted"} 1.0
```

### Confirming it in the logs

`docker compose logs worker | grep <request id>` (fields trimmed):

```
02:25:14.589 info    message_received          attempt=1
02:25:14.635 warning attempt_failed_will_retry attempt=1 error="health plan unreachable (ConnectError)"
02:25:14.640 info    message_routed            attempt=1 to=retry
02:25:15.642 info    message_received          attempt=2
02:25:15.656 warning attempt_failed_will_retry attempt=2 error="health plan unreachable (ConnectError)"
02:25:15.661 info    message_routed            attempt=2 to=retry
02:25:16.663 info    message_received          attempt=3
02:25:16.676 error   request_failed            attempt=3 reason=retries_exhausted
02:25:16.684 info    message_routed            attempt=3 to=dead
```

(That run used `RETRY_DELAY_MS=1000`; the default is 5 seconds between attempts.)

One full line, to show the shape:

```json
{"service": "priorauth-worker", "error": "health plan unreachable (ConnectError)", "max_attempts": 3,
 "event": "attempt_failed_will_retry", "attempt": 1, "request_id": "5b46461c-e7f0-4b1d-83a3-ae4b77d69db5",
 "level": "warning", "timestamp": "2026-11-08T02:25:14.635003Z",
 "trace_id": "135904419c6ec30e32c31ec0c79ac5f6", "span_id": "3a9a6f4d95747f33"}
```

`ConnectError` (not `ReadTimeout`, not an HTTP 5xx) means nothing is listening at
`PAYER_BASE_URL`: the health plan is down, not slow. `docker compose ps` confirmed `mock-payer` was
stopped.

### Fix and recovery

```bash
docker compose start mock-payer
```

New requests are approved again straight away. Requests that already failed stay `failed`: that
state is final, and the worker skips messages for finished requests, so moving them back from the
DLQ does nothing. The partner resubmits them (a replay endpoint is on the "next" list in the README).

---

## Other symptoms

### A request is stuck in `received`

The API saved it but no worker has taken it.

1. **Queue depth** panel: is `priorauth.requests` growing? Then the worker is down or stuck.
   `docker compose ps worker`, `docker compose logs worker --tail 50`.
2. Look for `broker_unavailable_reconnecting` in the worker log: the broker is down.
3. If the queue is empty and the request is still `received`, the message was lost between commit
   and publish. The API marks this case `failed` with `enqueue_failed` and returns 503, so this
   should not happen; if it does, it is a bug (see "transactional outbox" in the README).

### A request is stuck in `sent`

The worker picked it up and has not recorded an outcome.

1. `GET /requests/{id}`: `attempts` and `lastError` say which attempt it is on and why the last failed.
   If `lastError` is set, it is waiting in the retry queue: check the **Waiting for retry** stat.
   It will move within `RETRY_DELAY_MS`.
2. If `lastError` is empty, the worker is inside a call. Health plan calls time out after
   `PAYER_TIMEOUT_SECONDS` and FHIR lookups after `FHIR_TIMEOUT_SECONDS` (3 tries), so this lasts
   seconds, not minutes.
3. If it stays `sent` longer than that, the worker probably died mid-call. The message was not
   acked, so RabbitMQ redelivers it when the worker reconnects, and the attempt runs again. Check
   `docker compose ps worker` and restart it.
4. In Jaeger, open the request's trace (`trace_id` from any of its log lines): the last span shows
   where the time went.

### Requests fail with `permanent_error`

The health plan or the FHIR server said no in a way retrying will not fix (HTTP 4xx, unknown
Patient). `lastError` has the message. These skip the retries and go straight to the DLQ.

### 503 from `POST /requests`

The API could not reach RabbitMQ. The request was saved as `failed` (`enqueue_failed`); resubmit
once the broker is back. Check `docker compose ps rabbitmq`.

### Lots of 401s

Tokens last `JWT_TTL_MINUTES` (30). Clients must log in again when they see `token expired`.
