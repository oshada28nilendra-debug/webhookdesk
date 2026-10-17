# WebhookDesk

A dependency-free Python 3.12+ command-line webhook workbench for developers debugging local integrations. Capture a request, inspect its headers and exact body, then deliberately replay it to a named development receiver. SQLite retains events and individual retry outcomes across restarts.

## Quick start

No pip install, model download, paid service, or infrastructure is required.

```sh
python -m unittest discover -s tests -v
python demo.py
```

The demo uses **synthetic order data**, real HTTP over loopback, temporary SQLite databases, and two replays of the same event. It asserts that the receiver records only one unique receipt. Temporary demo data is removed on exit.

For interactive use, generate a token and use the same value in both terminals:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
export WEBHOOKDESK_TOKEN='paste-generated-token-here'
python webhookdesk.py --db capture.sqlite3 serve
```

In a second terminal, set the same `WEBHOOKDESK_TOKEN`, then:

```sh
python webhookdesk.py --db receiver.sqlite3 receiver
```

In a third terminal, set that token and send the sample:

```sh
curl --fail http://127.0.0.1:8765/capture \
  -H "Authorization: Bearer $WEBHOOKDESK_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @samples/order.json
python webhookdesk.py --db capture.sqlite3 list
python webhookdesk.py --db capture.sqlite3 show EVENT_ID
python webhookdesk.py --db capture.sqlite3 replay EVENT_ID --target demo --retries 2
python webhookdesk.py --db capture.sqlite3 show EVENT_ID > evidence.json
```

Replace `EVENT_ID` with the returned UUID. Repeat the replay: the sample receiver reports `duplicate: true`. Stop servers with Ctrl-C. PowerShell users can set `$env:WEBHOOKDESK_TOKEN` instead of `export`.

`targets.json` maps names to exact allowed endpoints. The included `demo` is `http://127.0.0.1:8766/receive`. Only literal IPv4 loopback, explicit ports, and HTTP paths are accepted; external hosts, DNS names, credentials, query strings and fragments are rejected. Use `--targets FILE` to select a configuration. Replay accepts a target **name**, never a request-provided URL. Redirects are recorded, never followed. The file is a trusted operator configuration, not an upload API.

## Behavior and architecture

- `POST /capture` requires a bearer token, one Content-Length, and at most 1 MiB. Both servers bind exclusively to `127.0.0.1`; browser Origin requests are rejected. There is no public tunnel or deployment configuration.
- SQLite stores UUID, capture time, repeated headers and raw body bytes. Common credential headers are redacted. `show` returns a readable UTF-8 preview, lossless base64 body and attempt history; `list` returns IDs/times/sizes. Inspection is local CLI access, not a web dashboard.
- Replay sends exact body bytes, `application/octet-stream`, a fresh configured bearer token, and a stable event UUID as `Idempotency-Key`. Original request headers are **not forwarded**, so captured authorization, cookies, Host and signatures cannot leak into replays. Adapting signed provider events is outside this MVP.
- Each HTTP outcome or transport error is committed to an attempts table. Default is one attempt; `--retries 0..2` permits at most three. Only transport errors, 429 and 5xx are retried, with 0.1/0.2-second waits and 3-second connection timeout. Response previews are capped at 4096 bytes. Inspect status values rather than treating CLI completion as delivery success.
- The sample receiver uses a unique key and SHA-256 body digest in a transaction. Same key/body returns duplicate; same key/different body returns 409. The simulated business effect is the receipt row itself, so duplicate prevention survives process restarts. Real receivers must atomically couple idempotency with their own business transaction.

## Evidence

Measured locally on October 17, 2026: **10 tests passed** in 4.360 seconds. `python demo.py` captured one event and produced two HTTP 200 replays, flags `[false, true]`, two persisted attempts and **one** unique receipt in 0.0317 seconds (excludes server shutdown). This is a functional smoke measurement on one synthetic payload, not a throughput benchmark or evidence of production reliability.

Tests exercise binary persistence, redaction, auth/origin/route failures, oversize/malformed framing, target rejection, missing IDs, retry bounds, real local 503→200 retries, redirect/client-error stopping, mocked connection failures, idempotency conflict, and restart persistence. CI runs the same suite and demo under Python 3.12.

## Trade-offs and limitations

This is a single-user trusted-local development tool. Python's standard HTTP server is not a hardened production ingress. Do not expose it via port forwarding or a reverse proxy. Captured bodies, custom headers, and response previews may contain secrets; databases are unencrypted and access relies on local filesystem permissions. Redaction is deliberately limited, not a guarantee of secret removal. Use synthetic data and remove your local DB when finished.

Storage has no retention quota or pagination; long-lived use can exhaust disk. No TLS, provider signature verification, dashboard, async job queue, distributed locking, multi-tenant roles or background replay. A crash after delivery but before attempt logging can leave uncertain delivery evidence; stable idempotency keys make deliberate retry safer only when the receiver honors them. No claim of exactly-once network delivery. The demo receiver validates no business schema and has no external side effects. HTTP framing rejects chunked bodies; timestamps are local wall-clock capture times. No browser UI exists to validate.

## Interview explanation

“I separated capture from replay so delivery is deliberate and inspectable. SQLite preserves raw evidence and each attempt. Replay uses a named loopback allowlist and constructs new headers rather than forwarding credentials. The receiver demonstrates the difference between at-least-once delivery attempts and a transactional idempotent effect. I tested retries, redirect refusal and persistence, and documented the crash window where delivery may have happened before its attempt was recorded.”
