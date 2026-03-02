# Umbra ERP MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that enables AI agents to manage ERP data through the Umbra ERP public API. Supports customers, invoices, products, quotes, payments, employees, leave requests, and webhooks.

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

## Available Tools (31)

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

### System
| Tool | Description |
|------|-------------|
| `check_status` | Verify API connectivity and auth |

## Authentication

All requests use the `X-Api-Key` header. API keys come in two types:

| Type | Prefix | Access |
|------|--------|--------|
| **Secret** | `usk_live_*` / `usk_test_*` | Full access including salary, bank details, national ID |
| **Publishable** | `uk_live_*` / `uk_test_*` | Standard access, sensitive employee fields excluded |

Keys are scoped by **permissions** (customers, invoices, products, quotes, payments, employees, webhooks). Your key only accesses the resources it has permission for.

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
| `UMBRA_API_KEY` | — | Your Umbra ERP API key (required) |
| `UMBRA_API_URL` | `https://umbra-erp-api-europenorth1-ynddvnxogq-lz.a.run.app` | API base URL |

## API Documentation

- **Interactive docs:** `{API_URL}/api/docs` (Swagger UI)
- **ReDoc:** `{API_URL}/api/redoc`

## License

MIT
