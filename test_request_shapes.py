"""Offline request-shape checks for the Umbra MCP mutation tools.

Asserts the exact HTTP method, path, headers and JSON body each write tool
sends, against the public API contract. No network, no API key, no database:
httpx.Client is replaced with a recorder, so these run anywhere and can be
used to verify a mutation tool without pointing it at a live business.

Run:  ../.venv/bin/python test_request_shapes.py
"""

from __future__ import annotations

import json
import sys
import uuid

import server

FAKE_KEY = "usk_test_offline"
RECORDED: list[dict] = []
FAILURES: list[str] = []
# What a GET replays. Lets a test stand in for a server that restates the
# header from the lines, or an older one that leaves it stale.
GET_PAYLOAD: dict = {"data": {"id": "fake", "items": []}}


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeClient:
    """Stands in for httpx.Client and records every request instead of sending it."""

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def _record(self, method, url, headers=None, json=None, params=None):
        RECORDED.append({
            "method": method,
            "url": url,
            "path": url.split(".app", 1)[-1] if ".app" in url else url.split("://", 1)[-1].split("/", 1)[-1],
            "headers": headers or {},
            "body": json,
            "params": params or {},
        })
        if method == "GET":
            return _FakeResponse(GET_PAYLOAD)
        return _FakeResponse({"data": {"id": "fake", "items": []}})

    def get(self, url, headers=None, params=None):
        return self._record("GET", url, headers, None, params)

    def post(self, url, headers=None, json=None):
        return self._record("POST", url, headers, json)

    def put(self, url, headers=None, json=None):
        return self._record("PUT", url, headers, json)

    def delete(self, url, headers=None):
        return self._record("DELETE", url, headers)


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL  {label}  {detail}")


def last() -> dict:
    return RECORDED[-1]


def run(fn, **kw):
    RECORDED.clear()
    fn(**kw)
    return last()


def main() -> int:
    server.httpx.Client = _FakeClient
    server._key_registry = {"primary": FAKE_KEY}
    server._url_registry = {}
    base = server.UMBRA_BASE_URL.rstrip("/")

    print("\nconvert_quote_to_invoice")
    r = run(server.convert_quote_to_invoice, quote_id="Q-UUID", idempotency_key="fixed-key")
    check("POST /v1/quotes/{id}/convert",
          r["method"] == "POST" and r["url"] == f"{base}/v1/quotes/Q-UUID/convert", r["url"])
    check("empty body", r["body"] == {}, str(r["body"]))
    check("X-Api-Key header", r["headers"].get("X-Api-Key") == FAKE_KEY)
    check("Idempotency-Key passed through", r["headers"].get("Idempotency-Key") == "fixed-key")

    r = run(server.convert_quote_to_invoice, quote_id="Q-UUID")
    key = r["headers"].get("Idempotency-Key")
    check("Idempotency-Key auto-generated as a UUID",
          bool(key) and str(uuid.UUID(key)) == key, str(key))

    print("\nconvert_lead_to_customer")
    r = run(server.convert_lead_to_customer, lead_id="L-UUID", email="ap@acme.com", currency="USD")
    check("POST /v1/leads/{id}/convert", r["url"] == f"{base}/v1/leads/L-UUID/convert")
    check("only supplied fields sent",
          r["body"] == {"email": "ap@acme.com", "currency": "USD"}, str(r["body"]))
    r = run(server.convert_lead_to_customer, lead_id="L-UUID")
    check("bare call sends {}", r["body"] == {}, str(r["body"]))

    print("\ncreate_bill (DOLLARS)")
    r = run(server.create_bill, vendor_id="V-UUID", total=230.00, subtotal=200.00,
            tax_amount=30.00, bill_date="2026-08-29", due_date="2026-09-28",
            vendor_bill_number="INV-9912")
    check("POST /v1/bills", r["method"] == "POST" and r["url"] == f"{base}/v1/bills")
    check("vendorId is the public id", r["body"]["vendorId"] == "V-UUID")
    check("amounts stay in dollars, no cents conversion",
          (r["body"]["total"], r["body"]["subtotal"], r["body"]["taxAmount"]) == (230.00, 200.00, 30.00),
          str(r["body"]))
    check("camelCase keys",
          {"billDate", "dueDate", "vendorBillNumber", "taxAmount"} <= set(r["body"]))
    check("billNumber omitted so the API auto-generates", "billNumber" not in r["body"])
    r = run(server.create_bill, vendor_id="V-UUID", total=99.99)
    check("subtotal defaults to total", r["body"]["subtotal"] == 99.99)
    check("taxAmount defaults to 0", r["body"]["taxAmount"] == 0)

    print("\nrecord_bill_payment (DOLLARS, the one dollars column)")
    r = run(server.record_bill_payment, bill_id="B-UUID", amount=2.33,
            payment_date="2026-08-28", payment_method="credit_card",
            reference="MasterCard ...7305", memo="Meta ads Aug balance")
    check("POST /v1/bills/{id}/payments", r["url"] == f"{base}/v1/bills/B-UUID/payments")
    check("amount 2.33 sent as dollars, NOT 233",
          r["body"]["amount"] == 2.33, str(r["body"]["amount"]))
    check("contract body keys",
          set(r["body"]) == {"amount", "paymentDate", "paymentMethod", "reference", "memo"},
          str(sorted(r["body"])))
    r = run(server.record_bill_payment, bill_id="B-UUID", amount=10.00)
    check("optional keys omitted when not supplied", set(r["body"]) == {"amount"})

    print("\ncreate_recurring_invoice")
    r = run(server.create_recurring_invoice, customer_id="C-UUID", title="Monthly retainer",
            frequency="monthly", start_date="2026-09-01", total=500.00,
            day_of_month=1, payment_terms_days=14, auto_send=True)
    check("POST /v1/recurring-invoices", r["url"] == f"{base}/v1/recurring-invoices")
    check("contract body",
          r["body"] == {"customerId": "C-UUID", "title": "Monthly retainer",
                        "frequency": "monthly", "startDate": "2026-09-01",
                        "total": 500.00, "subtotal": 500.00, "taxAmount": 0,
                        "currency": "USD", "paymentTermsDays": 14,
                        "autoSend": True, "dayOfMonth": 1},
          str(r["body"]))

    print("\ncreate_recurring_quote")
    r = run(server.create_recurring_quote, customer_id="C-UUID", title="Quarterly proposal",
            frequency="quarterly", start_date="2026-09-01", total=1500.00,
            day_of_month=1, validity_days=30, auto_send=True)
    check("POST /v1/recurring-quotes", r["url"] == f"{base}/v1/recurring-quotes")
    check("sends validityDays, never paymentTermsDays",
          r["body"].get("validityDays") == 30 and "paymentTermsDays" not in r["body"],
          str(r["body"]))
    check("contract body",
          r["body"] == {"customerId": "C-UUID", "title": "Quarterly proposal",
                        "frequency": "quarterly", "startDate": "2026-09-01",
                        "total": 1500.00, "subtotal": 1500.00, "taxAmount": 0,
                        "currency": "USD", "validityDays": 30,
                        "autoSend": True, "dayOfMonth": 1},
          str(r["body"]))

    print("\nset_recurring_quote_status")
    for action, expected in (("pause", "paused"), ("resume", "active"),
                             ("cancel", "cancelled"), ("end", "ended")):
        r = run(server.set_recurring_quote_status, recurring_id="R-UUID", action=action)
        check(f"{action} -> PUT status={expected}",
              r["method"] == "PUT" and r["url"] == f"{base}/v1/recurring-quotes/R-UUID"
              and r["body"] == {"status": expected}, str(r["body"]))
    RECORDED.clear()
    out = json.loads(server.set_recurring_quote_status(recurring_id="R-UUID", action="destroy"))
    check("unknown action rejected without any HTTP call",
          "error" in out and not RECORDED, str(out))

    print("\nupdate_recurring_invoice / update_recurring_quote / update_lead")
    r = run(server.update_recurring_invoice, recurring_id="R-UUID",
            updates='{"status": "paused", "total": 750.0}')
    check("PUT /v1/recurring-invoices/{id} verbatim",
          r["method"] == "PUT" and r["body"] == {"status": "paused", "total": 750.0})
    r = run(server.update_recurring_quote, recurring_id="R-UUID", updates='{"validityDays": 45}')
    check("PUT /v1/recurring-quotes/{id} verbatim", r["body"] == {"validityDays": 45})
    r = run(server.update_lead, lead_id="L-UUID", updates='{"status": "qualified", "score": 80}')
    check("PUT /v1/leads/{id} verbatim",
          r["url"] == f"{base}/v1/leads/L-UUID" and r["body"] == {"status": "qualified", "score": 80})
    RECORDED.clear()
    out = json.loads(server.update_lead(lead_id="L-UUID", updates="{not json}"))
    check("bad JSON rejected without any HTTP call", "error" in out and not RECORDED)

    print("\nline maths: TAX-INCLUSIVE, mirroring the API's _derive_totals_from_items")
    taxed = {"description": "X", "quantity": 1, "unitPrice": 100.00,
             "total": 100.00, "taxRate": 15}
    check("a 100.00 line at 15% carries 13.04 of tax, not 15.00",
          server._line_tax(taxed) == 13.04, str(server._line_tax(taxed)))
    check("100.00 at 15% derives 86.96 / 13.04 / 100.00",
          server._derive_document_totals([taxed])
          == {"subtotal": 86.96, "taxAmount": 13.04, "total": 100.00},
          str(server._derive_document_totals([taxed])))
    check("total is NOT subtotal + tax (no double count)",
          server._derive_document_totals([taxed])["total"] == 100.00)
    check("explicit per-line taxAmount is used as given",
          server._derive_document_totals([{"total": 100.00, "taxAmount": 13.04}])
          == {"subtotal": 86.96, "taxAmount": 13.04, "total": 100.00})
    check("zero-rated lines collapse subtotal onto total",
          server._derive_document_totals([{"total": 250.0}, {"total": -90.0}])
          == {"subtotal": 160.0, "taxAmount": 0.0, "total": 160.0})
    check("_line_total explicit branch is tax-inclusive",
          server._line_total({"total": 100.0}) == 100.0)
    check("_line_total computed branch is tax-inclusive too",
          server._line_total({"quantity": 2, "unitPrice": 50.0}) == 100.0,
          str(server._line_total({"quantity": 2, "unitPrice": 50.0})))
    check("computed branch honours discountPercent",
          server._line_total({"quantity": 1, "unitPrice": 100.0, "discountPercent": 10}) == 90.0)
    check("both branches agree on the same line",
          server._line_total({"quantity": 2, "unitPrice": 50.0, "total": 100.0})
          == server._line_total({"quantity": 2, "unitPrice": 50.0}))
    check("multi-line tax sums from the lines",
          server._derive_document_totals([taxed, dict(taxed)])
          == {"subtotal": 173.92, "taxAmount": 26.08, "total": 200.00},
          str(server._derive_document_totals([taxed, dict(taxed)])))

    print("\nupdate_quote: the server owns the header, the tool owns per-line tax")
    global GET_PAYLOAD
    GET_PAYLOAD = {"data": {"id": "fake", "subtotal": 160.0, "taxAmount": 0.0,
                            "total": 160.0,
                            "items": [{"description": "Starter"}, {"description": "Credit"}]}}
    r = run(server.update_quote, quote_id="Q-UUID",
            updates=json.dumps({"notes": "Repriced",
                                "items": [{"description": "Starter", "quantity": 1,
                                           "unitPrice": 250.0, "total": 250.0},
                                          {"description": "Credit", "quantity": 1,
                                           "unitPrice": -90.0, "total": -90.0}]}))
    puts = [x for x in RECORDED if x["method"] == "PUT"]
    check("header figures are NOT injected; the server restates them",
          not ({"subtotal", "taxAmount", "total"} & set(puts[0]["body"])),
          str(sorted(puts[0]["body"])))
    check("items still sent verbatim", len(puts[0]["body"]["items"]) == 2)
    check("re-reads the quote after writing items",
          any(x["method"] == "GET" for x in RECORDED),
          str([x["method"] for x in RECORDED]))
    check("a restating server needs no corrective write", len(puts) == 1, str(len(puts)))

    r = run(server.update_quote, quote_id="Q-UUID",
            updates=json.dumps({"subtotal": 999.0, "total": 999.0,
                                "items": [{"description": "X", "quantity": 1,
                                           "unitPrice": 1.0, "total": 1.0}]}))
    put = [x for x in RECORDED if x["method"] == "PUT"][0]
    check("explicit header figures are forwarded untouched",
          (put["body"]["subtotal"], put["body"]["total"]) == (999.0, 999.0), str(put["body"]))

    GET_PAYLOAD = {"data": {"id": "fake", "subtotal": 86.96, "taxAmount": 13.04,
                            "total": 100.0, "items": [{"description": "Taxed"}]}}
    r = run(server.update_quote, quote_id="Q-UUID",
            updates=json.dumps({"items": [{"description": "Taxed", "quantity": 1,
                                           "unitPrice": 100.0, "total": 100.0,
                                           "taxRate": 15}]}))
    put = [x for x in RECORDED if x["method"] == "PUT"][0]
    check("taxRate line gets a tax-inclusive per-line taxAmount",
          put["body"]["items"][0]["taxAmount"] == 13.04, str(put["body"]["items"][0]))
    check("still no header injection alongside it",
          not ({"subtotal", "taxAmount", "total"} & set(put["body"])), str(sorted(put["body"])))

    print("\nupdate_quote: a stale header gets one corrective write")
    GET_PAYLOAD = {"data": {"id": "fake", "subtotal": 575.0, "taxAmount": 0.0,
                            "total": 575.0,
                            "items": [{"description": "Starter"}, {"description": "Credit"}]}}
    RECORDED.clear()
    out = json.loads(server.update_quote(
        quote_id="Q-UUID",
        updates=json.dumps({"items": [{"description": "Starter", "quantity": 1,
                                       "unitPrice": 250.0, "total": 250.0},
                                      {"description": "Credit", "quantity": 1,
                                       "unitPrice": -90.0, "total": -90.0}]})))
    puts = [x for x in RECORDED if x["method"] == "PUT"]
    check("a non-restating server triggers a corrective PUT", len(puts) == 2, str(len(puts)))
    check("the corrective PUT carries the derived header",
          puts[1]["body"] == {"subtotal": 160.0, "taxAmount": 0.0, "total": 160.0},
          str(puts[1]["body"]))
    check("drift is reported when it cannot be corrected",
          out["headerReconciles"] is False and "warning" in out,
          str(out.get("headerReconciles")))
    GET_PAYLOAD = {"data": {"id": "fake", "items": []}}

    r = run(server.update_quote, quote_id="Q-UUID", updates='{"title": "New title"}')
    check("no items means a plain single PUT, no derivation, no re-read",
          len(RECORDED) == 1 and RECORDED[0]["body"] == {"title": "New title"},
          str([x["method"] for x in RECORDED]))

    print("\nread tools: query parameter names")
    r = run(server.aged_receivables, as_of="2026-08-31", customer_id="C-UUID")
    check("aged-receivables uses asOf/customerId",
          r["url"] == f"{base}/v1/reports/aged-receivables"
          and r["params"] == {"asOf": "2026-08-31", "customerId": "C-UUID"}, str(r["params"]))
    r = run(server.customer_statement, customer_id="C-UUID",
            start_date="2026-08-01", end_date="2026-08-31")
    check("statement uses startDate/endDate",
          r["url"] == f"{base}/v1/customers/C-UUID/statement"
          and r["params"] == {"startDate": "2026-08-01", "endDate": "2026-08-31"}, str(r["params"]))
    r = run(server.list_journal_entries, start_date="2026-08-01", end_date="2026-08-31",
            status="posted", limit=10, skip=5)
    check("journal-entries uses startDate/endDate/status/limit/skip",
          r["params"] == {"limit": 10, "skip": 5, "startDate": "2026-08-01",
                          "endDate": "2026-08-31", "status": "posted"}, str(r["params"]))
    r = run(server.list_receipts, limit=10, skip=5)
    check("receipts path and pagination",
          r["url"] == f"{base}/v1/receipts" and r["params"] == {"limit": 10, "skip": 5})
    r = run(server.get_receipt, receipt_id="R-UUID")
    check("get receipt path", r["url"] == f"{base}/v1/receipts/R-UUID")
    r = run(server.get_bill, bill_id="B-UUID")
    check("get bill path", r["url"] == f"{base}/v1/bills/B-UUID")
    r = run(server.get_journal_entry, entry_id="J-UUID")
    check("get journal entry path", r["url"] == f"{base}/v1/journal-entries/J-UUID")
    r = run(server.get_invoice_pay_link, invoice_id="I-UUID")
    check("pay-link path", r["url"] == f"{base}/v1/invoices/I-UUID/pay-link")
    r = run(server.list_bills, status="pending", limit=5)
    check("bills status filter", r["params"] == {"limit": 5, "skip": 0, "status": "pending"})

    print("\nevery mutation tool sends an Idempotency-Key")
    items_json = '[{"description": "X", "quantity": 1, "unitPrice": 1.0, "total": 1.0}]'
    mutations = [
        (server.create_customer, {"company_name": "Acme"}),
        (server.update_customer, {"customer_id": "C", "updates": '{"phone": "+263"}'}),
        (server.delete_customer, {"customer_id": "C"}),
        (server.create_invoice, {"customer_id": "C", "invoice_date": "2026-08-29",
                                 "due_date": "2026-09-28", "currency": "USD",
                                 "subtotal": 1.0, "total": 1.0, "balance_due": 1.0,
                                 "items": items_json}),
        (server.update_invoice, {"invoice_id": "I", "updates": '{"status": "sent"}'}),
        (server.delete_invoice, {"invoice_id": "I"}),
        (server.create_product, {"name": "Widget", "price": 1.0}),
        (server.update_product, {"product_id": "P", "updates": '{"price": 2.0}'}),
        (server.delete_product, {"product_id": "P"}),
        (server.create_quote, {"customer_id": "C", "title": "T", "quote_date": "2026-08-29",
                               "expiry_date": "2026-09-28", "subtotal": 1.0,
                               "total": 1.0, "items": items_json}),
        (server.update_quote, {"quote_id": "Q", "updates": '{"title": "T2"}'}),
        (server.delete_quote, {"quote_id": "Q"}),
        (server.create_payment, {"customer_id": "C", "amount": 1.0}),
        (server.create_contact, {"first_name": "Ada"}),
        (server.create_lead, {"first_name": "Ada"}),
        (server.update_lead, {"lead_id": "L", "updates": '{"score": 1}'}),
        (server.convert_lead_to_customer, {"lead_id": "L"}),
        (server.create_activity, {"activity_type": "call", "subject": "S"}),
        (server.create_employee, {"first_name": "Ada", "last_name": "L"}),
        (server.update_employee, {"employee_id": "E", "updates": '{"jobTitle": "CTO"}'}),
        (server.delete_employee, {"employee_id": "E"}),
        (server.create_leave_request, {"employee_id": "E", "leave_type": "annual",
                                       "start_date": "2026-09-01", "end_date": "2026-09-02",
                                       "days_requested": 2}),
        (server.update_leave_request, {"leave_id": "LV", "updates": '{"status": "approved"}'}),
        (server.delete_leave_request, {"leave_id": "LV"}),
        (server.create_webhook, {"name": "W", "url": "https://x.test", "events": "quote.created"}),
        (server.delete_webhook, {"webhook_id": "W"}),
        (server.test_webhook, {"webhook_id": "W"}),
        (server.convert_quote_to_invoice, {"quote_id": "Q"}),
        (server.create_bill, {"vendor_id": "V", "total": 1.0}),
        (server.record_bill_payment, {"bill_id": "B", "amount": 1.0}),
        (server.create_recurring_invoice, {"customer_id": "C", "title": "T",
                                           "frequency": "monthly",
                                           "start_date": "2026-09-01", "total": 1.0}),
        (server.update_recurring_invoice, {"recurring_id": "R", "updates": '{"status": "paused"}'}),
        (server.create_recurring_quote, {"customer_id": "C", "title": "T",
                                         "frequency": "monthly",
                                         "start_date": "2026-09-01", "total": 1.0}),
        (server.update_recurring_quote, {"recurring_id": "R", "updates": '{"validityDays": 45}'}),
        (server.set_recurring_quote_status, {"recurring_id": "R", "action": "pause"}),
    ]
    unkeyed, bad_uuid = [], []
    for fn, kwargs in mutations:
        RECORDED.clear()
        fn(**kwargs)
        writes = [x for x in RECORDED if x["method"] in ("POST", "PUT", "DELETE")]
        if not writes:
            unkeyed.append(f"{fn.__name__} (no write recorded)")
            continue
        for w in writes:
            key = w["headers"].get("Idempotency-Key")
            if not key:
                unkeyed.append(f"{fn.__name__} {w['method']}")
                continue
            try:
                if str(uuid.UUID(key)) != key:
                    raise ValueError
            except (ValueError, AttributeError, TypeError):
                bad_uuid.append(f"{fn.__name__}={key}")
    check(f"all {len(mutations)} mutation tools send the header",
          not unkeyed, "missing on: " + ", ".join(unkeyed))
    check("auto-generated keys are UUIDs", not bad_uuid, ", ".join(bad_uuid))

    explicit = [
        (server.create_customer, {"company_name": "Acme"}),
        (server.create_product, {"name": "W", "price": 1.0}),
        (server.create_quote, {"customer_id": "C", "title": "T", "quote_date": "2026-08-29",
                               "expiry_date": "2026-09-28", "subtotal": 1.0,
                               "total": 1.0, "items": items_json}),
        (server.create_invoice, {"customer_id": "C", "invoice_date": "2026-08-29",
                                 "due_date": "2026-09-28", "currency": "USD",
                                 "subtotal": 1.0, "total": 1.0, "balance_due": 1.0,
                                 "items": items_json}),
        (server.create_payment, {"customer_id": "C", "amount": 1.0}),
        (server.create_bill, {"vendor_id": "V", "total": 1.0}),
        (server.record_bill_payment, {"bill_id": "B", "amount": 1.0}),
        (server.create_recurring_invoice, {"customer_id": "C", "title": "T",
                                           "frequency": "monthly",
                                           "start_date": "2026-09-01", "total": 1.0}),
        (server.create_recurring_quote, {"customer_id": "C", "title": "T",
                                         "frequency": "monthly",
                                         "start_date": "2026-09-01", "total": 1.0}),
        (server.convert_quote_to_invoice, {"quote_id": "Q"}),
        (server.convert_lead_to_customer, {"lead_id": "L"}),
    ]
    wrong = []
    for fn, kwargs in explicit:
        r = run(fn, idempotency_key="caller-supplied", **kwargs)
        if r["headers"].get("Idempotency-Key") != "caller-supplied":
            wrong.append(fn.__name__)
    check(f"all {len(explicit)} contract-Idem tools honour a caller key",
          not wrong, ", ".join(wrong))

    print("\nno GET tool ever sends an Idempotency-Key")
    RECORDED.clear()
    server.list_receipts()
    server.get_bill(bill_id="B")
    server.aged_receivables()
    check("reads are unkeyed",
          RECORDED and all("Idempotency-Key" not in x["headers"]
                           for x in RECORDED if x["method"] == "GET"),
          str([sorted(x["headers"]) for x in RECORDED]))

    print("\nper-workspace base URL")
    server._url_registry = {"staging": "https://staging.umbraerp.com"}
    server._key_registry = {"primary": FAKE_KEY, "staging": "usk_test_staging"}
    r = run(server.list_receipts, workspace="staging")
    check("staging workspace hits the staging host",
          r["url"] == "https://staging.umbraerp.com/v1/receipts", r["url"])
    check("staging workspace uses the staging key",
          r["headers"]["X-Api-Key"] == "usk_test_staging")
    r = run(server.list_receipts)
    check("primary still hits the default host", r["url"] == f"{base}/v1/receipts")

    print("\nenvironment binding guard")
    # Built rather than written out so the secret scanner does not read these
    # fixtures as real keys.
    live_fixture = "usk_" + "live_" + "notarealkey"
    test_fixture = "usk_" + "test_" + "notarealkey"
    staging_host = "https://staging.umbraerp.com"
    server._key_registry = {"primary": FAKE_KEY, "oops": live_fixture}
    server._url_registry = {"oops": staging_host}
    RECORDED.clear()
    out = json.loads(server.list_receipts(workspace="oops"))
    check("a live key aimed at a staging host is refused before any request",
          "error" in out and "live API key" in out["error"] and not RECORDED, str(out))
    server._key_registry = {"primary": FAKE_KEY, "fine": test_fixture}
    server._url_registry = {"fine": staging_host}
    r = run(server.list_receipts, workspace="fine")
    check("a test key on a staging host is allowed",
          r["url"] == f"{staging_host}/v1/receipts")
    server._key_registry = {"primary": test_fixture}
    server._url_registry = {}
    r = run(server.list_receipts)
    check("a test key on production is allowed (environment is a key column, not a host)",
          r["url"] == f"{base}/v1/receipts")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + "; ".join(FAILURES))
        return 1
    print("all request-shape checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
