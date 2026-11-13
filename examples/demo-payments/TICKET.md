# PAY-123: Idempotent transfer creation (made-up demo ticket)

`POST /v1/transfers` must be idempotent per `Idempotency-Key` header.

## Acceptance criteria

1. A new key with a valid body creates the transfer: `201` and the transfer JSON.
2. Reusing a key returns `409 Conflict` and does **not** create a second transfer.
3. A request without `Idempotency-Key` is rejected with `400`.
4. `amount` must be a positive integer (cents); otherwise `422`.
5. `GET /v1/transfers/{id}` keeps working as before.
