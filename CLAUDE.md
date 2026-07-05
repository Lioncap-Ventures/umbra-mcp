# Umbra ERP MCP Server

MCP server for managing ERP data (customers, invoices, products, quotes, payments, employees, leave requests, webhooks) via Umbra ERP public API.

## Configuration

API key resolved in order:
1. `UMBRA_API_KEY` environment variable
2. `~/.claude/scripts/.env` file (UMBRA_API_KEY=...)

**Production URL:** `https://umbra-erp-api-europenorth1-wufqavak5a-lz.a.run.app` (override with `UMBRA_API_URL` env var)
**Staging URL:** `https://staging-umbra-erp-api-europenorth1-wufqavak5a-lz.a.run.app`

## Tools (31 total)

| Resource | Tools |
|----------|-------|
| Customers | `list_customers`, `get_customer`, `create_customer`, `update_customer`, `delete_customer` |
| Invoices | `list_invoices`, `get_invoice`, `create_invoice`, `update_invoice`, `delete_invoice` |
| Products | `list_products`, `get_product`, `create_product`, `update_product`, `delete_product` |
| Quotes | `list_quotes`, `get_quote`, `create_quote`, `update_quote`, `delete_quote` |
| Payments | `list_payments`, `get_payment`, `create_payment` |
| Employees | `list_employees`, `get_employee`, `create_employee`, `update_employee`, `delete_employee` |
| Leave | `list_leave_requests`, `create_leave_request`, `update_leave_request`, `delete_leave_request` |
| Webhooks | `list_webhooks`, `create_webhook`, `delete_webhook`, `test_webhook` |
| Status | `check_status` |

## Auth

**API key format:** `usk_live_*` (secret key with full access including sensitive employee data)
**Header:** `X-Api-Key`

## Running

```bash
python3 server.py
```
