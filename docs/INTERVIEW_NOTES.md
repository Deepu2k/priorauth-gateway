# Interview notes

Short answers to the five questions in the project plan, each pointing at the code that backs it.
Read the code behind each one until you can explain it without these notes.

## 1. Why a queue instead of calling the health plan from the API?

- **The caller should not wait on the slowest system.** Health plans can take seconds or be down for
  minutes. With a queue the API answers `202 Accepted` in milliseconds, and the client polls
  `GET /requests/{id}`. (`api/routes.py: submit`)
- **Failures become retries, not errors.** If the health plan is down, the request waits in the retry
  queue and goes out again instead of failing back to the clinician. (`queue.py`, `worker/main.py`)
- **Load levelling.** A burst of submissions piles up in the queue, and the workers work through it at
  the rate the health plan can take (`prefetch_count=1` per worker; add workers to go faster).
- **Independent scaling and deploys.** The API and worker are separate processes. You can restart the
  worker without dropping submissions.

The cost: it is eventually consistent (the client polls), and you have to handle duplicates and
the window between saving the row and publishing the message (see 2 and the README's outbox note).

## 2. What happens when the health plan is down?

Walk through `worker/processor.py` and `worker/main.py`:

1. The worker marks the request `sent` and calls the health plan with a timeout
   (`PAYER_TIMEOUT_SECONDS`, default 2s).
2. `clients/payer.py` sorts the failure: timeouts, connection errors, 5xx and 429 are
   `RetryableError`; other 4xx are `PermanentError`.
3. Retryable and attempts left: history gets `retry_scheduled`, and the worker publishes the message
   to the retry queue (`priorauth.requests.retry.<delay>ms`) with `x-attempt` + 1, then acks the original.
4. The retry queue has no consumer. Its `x-message-ttl` (`RETRY_DELAY_MS`) expires the message, and
   its dead-letter settings route it back to the work queue. That is the delay, with no sleeping in
   the worker.
5. On the third failure (`MAX_ATTEMPTS`) the request becomes `failed` (`retries_exhausted`) and the
   message goes to `priorauth.requests.dlq` with the last error in `x-last-error`.
6. Permanent errors skip the retries and go to the DLQ on the first attempt.
7. Unparseable messages are rejected, and the work queue's own dead-letter setting sends them to the
   DLQ too.

The test interviewers ask about: `tests/integration/test_flow.py::test_health_plan_timing_out_three_times_ends_in_dlq`
(real RabbitMQ) and `tests/unit/test_processor.py::test_three_timeouts_end_in_dead_letter_queue`.

Details worth mentioning:
- **Ack after publish.** The worker acks only after the broker confirms the follow-up message
  (publisher confirms), so a crash causes a duplicate, never a lost request.
- **Duplicates are harmless.** The worker locks the row (`SELECT ... FOR UPDATE`) and skips requests
  already in a final state (`test_duplicate_message_does_not_reprocess`).
- **No transaction is held open during network calls.** Claim, call, record: three short steps.

## 3. What is FHIR and why use it?

FHIR (Fast Healthcare Interoperability Resources, from HL7) is the standard for exchanging health
data as JSON or XML "resources" over REST: `Patient`, `Coverage`, `ServiceRequest`, `Claim`, and so
on. Each has a defined structure and coded fields (CPT for procedures, ICD-10 for diagnoses), so two
systems that have never met can still read each other's data. US rules (the 21st Century Cures Act,
and CMS's prior authorization rule, CMS-0057-F) push payers and providers to FHIR APIs, and the
Da Vinci "Prior Authorization Support" guide defines how prior auth works over FHIR.

In this project (`fhir.py`): requests carry FHIR-shaped `Patient`, `Coverage` and `ServiceRequest`;
they travel to the health plan as a FHIR `Bundle`; validation errors come back as a FHIR
`OperationOutcome`; and a `patientReference` like `Patient/123` is resolved against a real FHIR
server (hapi.fhir.org) by `clients/fhir_server.py`. It is FHIR-*shaped*, not a full FHIR
implementation: no profiles, no terminology validation, and not the Da Vinci PAS `Claim` bundle.

## 4. How is a request authenticated, and why check a role before each action?

- **People** log in at `POST /auth/token` and get a signed JWT (HS256) with `sub`, `roles`, `iss`,
  `aud` and `exp`. `security.py: decode_token` checks the signature with a pinned algorithm (so an
  `alg: none` token fails), the issuer, the audience and expiry.
- **Partner systems** send `X-API-Key`. Only SHA-256 hashes of keys are configured, and comparison is
  constant-time.
- **Roles** (`submitter`, `viewer`, `admin`) are checked by a FastAPI dependency,
  `require_role(...)`, that runs before the endpoint body. So an unauthorized caller gets a 403 before
  any work happens: no database write, no message, and not even a validation error that would
  describe the schema (`test_role_check_runs_before_validation`).
- **Least privilege and ownership.** A submitter sees only its own requests and gets a 404, not a 403,
  for anyone else's, so request ids cannot be probed.
- Secrets come from environment variables (`.env` locally, Secrets Manager in the Terraform); none
  are in the repo.
- In production the token endpoint would be replaced by an OIDC provider (Keycloak, Okta, Cognito),
  with RS256 and the provider's public keys. Only `decode_token` changes.

## 5. How would you debug a request stuck in `sent`?

See `docs/RUNBOOK.md`, "A request is stuck in `sent`". In short:

1. `GET /requests/{id}`: `attempts` and `lastError`. If `lastError` is set, it is waiting for a retry.
2. `GET /requests/{id}/history`: the exact sequence of attempts and errors, with timestamps.
3. `grep` the request id in the worker logs: every line has `request_id`, `attempt` and `trace_id`.
4. The dashboard: retry queue depth, health plan outcomes (timeouts vs unreachable vs 5xx), latency.
5. Jaeger: the trace for that request shows API → worker → health plan spans and where time went.
6. If the worker died mid-call, the unacked message is redelivered on reconnect, so the request
   moves on once a worker is running.
