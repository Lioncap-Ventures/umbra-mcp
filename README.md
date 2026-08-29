# Umbra ERP MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that enables AI agents to manage ERP data through the Umbra ERP public API. Supports customers, invoices, quotes, products, payments, receipts, recurring invoice and quote schedules, supplier bills and payments, journal entries, aged receivables, customer statements, employees, leave requests, webhooks, and CRM contacts, leads and activities.

## Quick Start

### 1. Get an API Key

Sign up at [umbraerp.com](https://umbraerp.com) and generate an API key from **Settings > API Keys**. Secret keys (`usk_*`) have access to sensitive employee data; publishable keys (`uk_*`) do not.

### 2. Install

```bash
# Clone the repo
git clone https://github.com/Lioncap-Ventures/umbra-mcp.git
cd umbra-mcp

# Install dependencies
pip install mcp httpx
```

### 3. Configure

Set your API key via environment variable or add it to a `.env` file:

```bash
# Option A: Environment variable
export UMBRA_API_KEY="usk_live_your_key_here"

# Option B: Add to ~/.claude/scripts/.env (for Claude Code)
echo 'UMBRA_API_KEY=usk_live_your_key_here' >> ~/.claude/scripts/.env
```

### 4. Register with Claude Code

Add to `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "umbra": {
      "command": "python3",
      "args": ["/path/to/umbra-mcp/server.py"]
    }
  }
}
```

Then restart Claude Code. The Umbra ERP tools will be available in all conversations.

### 5. Run Standalone

```bash
python3 server.py
```

## Money units

Every amount a tool takes or returns is in **dollars** (`150.00`), never cents. That includes
`record_bill_payment`, whose `amount` is dollars end to end. Do not convert.

The reports return `Cents` twins (`bucketsCents`, `totalCents`, `closingBalanceCents`) in integer
cents. Use those for comparison and summation and the dollar fields for display; float sums drift.

## Identifiers

Every id you pass to a tool is a **public UUID**. A few *nested response* fields are numeric row
ids instead and must never be fed back into a tool: `recurring*.customerId`, `bill.vendorId`,
`customer_statement.payments[].invoiceId`, `recurring_quote.lastGeneratedQuoteId`,
`journal.sourceId` and `journal.lines[].accountId`.

## Available Tools (67)

### Customers
| Tool | Description |
|------|-------------|
| `list_customers` | List customers with search, industry, and country filters |
| `get_customer` | Get a single customer by ID |
| `create_customer` | Create a new customer |
| `update_customer` | Update customer fields |
| `delete_customer` | Delete a customer |

### Invoices
| Tool | Description |
|------|-------------|
| `list_invoices` | List invoices with status and customer filters |
| `get_invoice` | Get invoice with line items |
| `create_invoice` | Create invoice with line items |
| `update_invoice` | Update invoice fields |
| `delete_invoice` | Delete an invoice |

### Products
| Tool | Description |
|------|-------------|
| `list_products` | List products with category and type filters |
| `get_product` | Get a single product |
| `create_product` | Create a new product |
| `update_product` | Update product fields |
| `delete_product` | Delete a product |

### Quotes
| Tool | Description |
|------|-------------|
| `list_quotes` | List quotes with status and customer filters |
| `get_quote` | Get quote with line items |
| `create_quote` | Create quote with line items |
| `update_quote` | Update quote fields |
| `delete_quote` | Delete a quote |

### Payments
| Tool | Description |
|------|-------------|
| `list_payments` | List payments with customer filter |
| `get_payment` | Get a single payment |
| `create_payment` | Record a new payment |

### Employees
| Tool | Description |
|------|-------------|
| `list_employees` | List employees with status, department, and search filters |
| `get_employee` | Get employee details (sensitive fields with secret key) |
| `create_employee` | Create a new employee |
| `update_employee` | Update employee fields |
| `delete_employee` | Soft-delete (terminate) an employee |

### Leave Requests
| Tool | Description |
|------|-------------|
| `list_leave_requests` | List leave requests for an employee |
| `create_leave_request` | Create a leave request (starts as pending) |
| `update_leave_request` | Approve, reject, or cancel a leave request |
| `delete_leave_request` | Delete a leave request |

### Webhooks
| Tool | Description |
|------|-------------|
| `list_webhooks` | List registered webhooks |
| `create_webhook` | Register a webhook for events |
| `delete_webhook` | Delete a webhook |
| `test_webhook` | Send a test event to a webhook |

### Receipts
| Tool | Description |
|------|-------------|
| `list_receipts` | List customer receipts (money in). Same rows as `list_payments` |
| `get_receipt` | Get a single receipt by public UUID |

### Pay links and quote conversion
| Tool | Description |
|------|-------------|
| `get_invoice_pay_link` | Get or mint the customer-facing card pay URL for an invoice |
| `convert_quote_to_invoice` | Convert a quote into a numbered invoice. Safe to retry |

### Recurring schedules
| Tool | Description |
|------|-------------|
| `list_recurring_invoices` | List recurring-invoice templates |
| `create_recurring_invoice` | Create a schedule that bills a customer automatically |
| `update_recurring_invoice` | Update a schedule (including pause/cancel via `status`) |
| `list_recurring_quotes` | List recurring-quote templates |
| `create_recurring_quote` | Create a schedule that emits quotes automatically |
| `update_recurring_quote` | Update a schedule, including `validityDays` |
| `set_recurring_quote_status` | Pause, resume, cancel or end a quote schedule |
| `recurring_quote_history` | Generation counters for one quote schedule |

### Reports
| Tool | Description |
|------|-------------|
| `aged_receivables` | Outstanding receivables bucketed by days PAST DUE |
| `customer_statement` | Invoices, receipts and closing balance for one customer |

### Bills (accounts payable)
| Tool | Description |
|------|-------------|
| `list_bills` | List supplier bills with a status filter |
| `get_bill` | Get a single bill by public UUID |
| `create_bill` | Record a supplier bill (a payable) |
| `record_bill_payment` | Pay a bill. Amount in DOLLARS. Overpayment is rejected, not clamped |

### Journal entries (read-only)
| Tool | Description |
|------|-------------|
| `list_journal_entries` | List ledger entries with date and status filters |
| `get_journal_entry` | Get one entry with its debit/credit lines |

There is deliberately no journal write tool. Entries are posted from real events; a hand-made entry
would be a figure in the ledger with no document behind it.

### CRM
| Tool | Description |
|------|-------------|
| `list_contacts`, `get_contact`, `create_contact` | CRM contacts |
| `list_leads`, `create_lead` | CRM leads |
| `update_lead` | Update lead fields (status is enum-validated) |
| `convert_lead_to_customer` | Convert a lead into a customer. Safe to retry |
| `list_activities`, `create_activity` | CRM activities |

### System
| Tool | Description |
|------|-------------|
| `list_workspaces` | List configured business workspaces (names only) |
| `check_status` | Verify API connectivity, auth and the resources reachable |

## Authentication

All requests use the `X-Api-Key` header. API keys come in two types:

| Type | Prefix | Access |
|------|--------|--------|
| **Secret** | `usk_live_*` / `usk_test_*` | Full access including salary, bank details, national ID |
| **Publishable** | `uk_live_*` / `uk_test_*` | Standard access, sensitive employee fields excluded |

Keys are scoped by **permissions**. Your key only reaches the resources it holds a scope for:

| Tools | Required permission |
|---|---|
| customers, `customer_statement` | `customers` |
| invoices, `get_invoice_pay_link`, recurring invoices | `invoices` |
| quotes, `convert_quote_to_invoice`, recurring quotes | `quotes` |
| products | `products` |
| payments, receipts | `payments` |
| leads, `update_lead`, `convert_lead_to_customer` | `leads` |
| contacts | `contacts` |
| activities | `activities` |
| employees, leave requests | `employees` |
| pay runs, payslips | `payroll` |
| bills, `record_bill_payment` | `bills` |
| journal entries | `journal` |
| `aged_receivables` | `reports` |
| webhooks | `webhooks` |

**`bills`, `journal`, `reports`, `employees` and `payroll` only became grantable on 2026-08-29.**
A key minted before that date cannot hold them and returns 403 on those endpoints. Re-mint the key
with the scopes you need.

## Rate Limits

- **100 requests/minute** per API key
- **1,000 requests/hour** per API key
- Rate limit headers (`X-RateLimit-Remaining-Minute`, `X-RateLimit-Remaining-Hour`) included in responses

## Webhook Events

Register webhooks to receive real-time notifications:

```
customer.created, customer.updated, customer.deleted
invoice.created, invoice.updated, invoice.deleted
product.created, product.updated, product.deleted
quote.created, quote.updated, quote.deleted
payment.created
employee.created, employee.updated, employee.deleted
leave.created, leave.updated, leave.deleted
```

Payloads are signed with HMAC-SHA256 via the `X-Webhook-Signature` header.

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `UMBRA_API_KEY` | none | Your Umbra ERP API key (required). Becomes the `primary` workspace |
| `UMBRA_API_KEY_<NAME>` | none | Extra business workspaces, e.g. `UMBRA_API_KEY_MWANA` |
| `UMBRA_API_URL` | `https://umbra-erp-api-europenorth1-wufqavak5a-lz.a.run.app` | Default API base URL |
| `UMBRA_API_URL_<NAME>` | `UMBRA_API_URL` | Base URL for one workspace. A key is bound to one business AND one environment, so a workspace holding a staging key needs its host set here or it will 401 against production |

Every tool takes an optional `workspace` argument (default `primary`).

### Workspace environment binding

Pairing `UMBRA_API_KEY_<NAME>` with `UMBRA_API_URL_<NAME>` is **convention, not something the code
can enforce in general**. A key is bound to one business and one environment, so a mismatched pair
fails safe: the request simply 401s rather than reaching the wrong business.

One direction is asserted, because it is unambiguous and the failure would be pointing real
credentials at a test system: a **live** key (`usk_live_*` / `uk_live_*`) sent to a host whose URL
contains `staging` is refused before the request leaves.

The reverse is deliberately **not** asserted. `environment` is a column on the key, not a property
of the host, and production currently serves active test-environment keys, so refusing a `usk_test_*`
key against a non-staging host would break a real configuration.

## Tests

```bash
../.venv/bin/python test_request_shapes.py
```

Offline request-shape checks: they replace `httpx.Client` with a recorder and assert the exact
method, path, headers and JSON body every write tool sends. No network, no API key, no database,
so a mutation tool can be verified without pointing it at a live business.

## API Documentation

- **Interactive docs:** `{API_URL}/api/docs` (Swagger UI)
- **ReDoc:** `{API_URL}/api/redoc`

## License

MIT
