# Umbra ERP MCP Server

MCP server for managing ERP data (customers, invoices, products, quotes, payments, employees, leave requests, webhooks) via Umbra ERP public API.

## Configuration

API key resolved in order:
1. `UMBRA_API_KEY` environment variable
2. `~/.claude/scripts/.env` file (UMBRA_API_KEY=...)

**Production URL:** `https://umbra-erp-api-europenorth1-wufqavak5a-lz.a.run.app` (override with `UMBRA_API_URL` env var)
**Staging URL:** `https://staging-umbra-erp-api-europenorth1-wufqavak5a-lz.a.run.app`

## Tools (67 total)

| Resource | Tools |
|----------|-------|
| Customers | `list_customers`, `get_customer`, `create_customer`, `update_customer`, `delete_customer`, `customer_statement` |
| Invoices | `list_invoices`, `get_invoice`, `create_invoice`, `update_invoice`, `delete_invoice`, `get_invoice_pay_link` |
| Products | `list_products`, `get_product`, `create_product`, `update_product`, `delete_product` |
| Quotes | `list_quotes`, `get_quote`, `create_quote`, `update_quote`, `delete_quote`, `convert_quote_to_invoice` |
| Payments | `list_payments`, `get_payment`, `create_payment` |
| Receipts | `list_receipts`, `get_receipt` |
| Recurring invoices | `list_recurring_invoices`, `create_recurring_invoice`, `update_recurring_invoice` |
| Recurring quotes | `list_recurring_quotes`, `create_recurring_quote`, `update_recurring_quote`, `set_recurring_quote_status`, `recurring_quote_history` |
| Reports | `aged_receivables` |
| Bills (AP) | `list_bills`, `get_bill`, `create_bill`, `record_bill_payment` |
| Journal (read-only) | `list_journal_entries`, `get_journal_entry` |
| CRM | `list_contacts`, `get_contact`, `create_contact`, `list_leads`, `create_lead`, `update_lead`, `convert_lead_to_customer`, `list_activities`, `create_activity` |
| Employees | `list_employees`, `get_employee`, `create_employee`, `update_employee`, `delete_employee` |
| Leave | `list_leave_requests`, `create_leave_request`, `update_leave_request`, `delete_leave_request` |
| Webhooks | `list_webhooks`, `create_webhook`, `delete_webhook`, `test_webhook` |
| Status | `list_workspaces`, `check_status` |

## Rules for this repo

- **Dollars everywhere.** Every tool argument and every non-`Cents` response field is dollars,
  including `record_bill_payment.amount`. Never convert to cents. Fields ending in `Cents` are the
  only integer-cents values; prefer them for arithmetic.
- **No journal write tool.** Journal entries are posted from real events by design.
- **Every mutation sends an `Idempotency-Key`.** Tools auto-generate one and accept
  `idempotency_key` so a deliberate retry replays instead of duplicating.
- **A 5xx from a create/convert does not mean nothing happened.** Those tools return
  `writeMayHaveLanded: true` with the key they used; verify before retrying.
- **Permissions.** `bills`, `journal`, `reports`, `employees` and `payroll` only became grantable on
  2026-08-29. Keys minted earlier 403 on those endpoints and must be re-minted.

## Tests

```bash
../.venv/bin/python test_request_shapes.py   # offline; asserts every write tool's request shape
../.venv/bin/python -c "import server"       # import smoke test
```

## Auth

**API key format:** `usk_live_*` (secret key with full access including sensitive employee data)
**Header:** `X-Api-Key`

## Running

```bash
python3 server.py
```
