#!/usr/bin/env python3
"""Made-up 'payments' service for demoing the manual-qa skill. Backed by Postgres.

Started by docker-compose.yml (needs psycopg). Endpoints:
  GET  /health
  POST /v1/transfers            body {"from","to","amount"}, header Idempotency-Key
  GET  /v1/transfers/{id}
"""

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.rows import dict_row

DSN = os.environ["DATABASE_DSN"]
SCHEMA = """
create table if not exists transfers (
    id              text primary key,
    from_account    text not null,
    to_account      text not null,
    amount          bigint not null,
    idempotency_key text not null,
    state           text not null default 'PENDING',
    created_at      timestamptz not null default now()
)
"""


def db():
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path.startswith("/v1/transfers/"):
            with db() as conn:
                row = conn.execute(
                    "select id, from_account, to_account, amount, state from transfers where id = %s",
                    (self.path.rsplit("/", 1)[1],),
                ).fetchone()
            return self._send(200, row) if row else self._send(404, {"error": "not found"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/transfers":
            return self._send(404, {"error": "not found"})
        key = self.headers.get("Idempotency-Key")
        if not key:
            return self._send(400, {"error": "Idempotency-Key header required"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            amount = int(body["amount"])  # BUG (planted): no check that amount > 0
            src, dst = str(body["from"]), str(body["to"])
        except (ValueError, KeyError, TypeError):
            return self._send(422, {"error": "invalid body"})
        with db() as conn:
            if conn.execute("select 1 from transfers where idempotency_key = %s", (key,)).fetchone():
                return self._send(409, {"error": "duplicate idempotency key"})
            row = conn.execute(
                "insert into transfers (id, from_account, to_account, amount, idempotency_key) "
                "values (%s, %s, %s, %s, %s) returning id, from_account, to_account, amount, state",
                ("tr_" + uuid.uuid4().hex[:8], src, dst, amount, key),
            ).fetchone()
        self._send(201, row)


if __name__ == "__main__":
    with db() as conn:
        conn.execute(SCHEMA)
    print("demo payments service on :8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
