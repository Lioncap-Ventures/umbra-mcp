"""Umbra ERP MCP Server — Enables Claude/Mufasa to manage ERP data via Umbra public API.

Production API: https://umbra-erp-api-europenorth1-wufqavak5a-lz.a.run.app
Staging API:    https://staging-umbra-erp-api-europenorth1-wufqavak5a-lz.a.run.app
Auth: X-Api-Key header

Multi-workspace: one MCP server can talk to several Umbra businesses. Each
workspace is a named API key. The plain UMBRA_API_KEY is the "primary"
workspace; extra businesses are added via UMBRA_API_KEY_<NAME> env vars
(e.g. UMBRA_API_KEY_MWANA, UMBRA_API_KEY_AVALON, UMBRA_API_KEY_LIONCAP).
Every tool takes an optional `workspace` argument (default "primary").
Keys are also read from ~/.claude/scripts/.env (same UMBRA_API_KEY* names).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("umbra-mcp")

# ============================================================================
# Configuration
# ============================================================================

UMBRA_BASE_URL = os.environ.get(
    "UMBRA_API_URL",
    "https://umbra-erp-api-europenorth1-wufqavak5a-lz.a.run.app",
)

_ENV_PREFIX = "UMBRA_API_KEY"
_URL_PREFIX = "UMBRA_API_URL_"
_PRIMARY = "primary"

# {workspace_name -> api_key}, built lazily at first use.
_key_registry: dict[str, str] | None = None
# {workspace_name -> base_url} for workspaces that live on a different host
# (e.g. a staging business). Built lazily alongside the key registry.
_url_registry: dict[str, str] | None = None


def _read_env_file(prefix: str) -> dict[str, str]:
    """Read env-var entries starting with `prefix` from ~/.claude/scripts/.env.

    Returns a dict keyed by the raw env-var name (e.g. UMBRA_API_KEY,
    UMBRA_API_KEY_MWANA). Missing file is not an error.
    """
    found: dict[str, str] = {}
    env_file = os.path.expanduser("~/.claude/scripts/.env")
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, val = line.split("=", 1)
                name, val = name.strip(), val.strip()
                if name.startswith(prefix) and name != "UMBRA_API_URL" and val:
                    found[name] = val
    except FileNotFoundError:
        pass
    return found


def _read_env_file_keys() -> dict[str, str]:
    """Read UMBRA_API_KEY* entries from ~/.claude/scripts/.env (Claude Code fallback)."""
    return _read_env_file(_ENV_PREFIX)


def _build_registry() -> dict[str, str]:
    """Build the {workspace -> key} registry from env vars and the .env fallback.

    - UMBRA_API_KEY                -> "primary"
    - UMBRA_API_KEY_<NAME>         -> "<name>" (lower-cased)
    Process env vars take precedence over ~/.claude/scripts/.env. Cached after
    first build.
    """
    global _key_registry
    if _key_registry is not None:
        return _key_registry

    # Lowest precedence: .env file. Highest precedence: real environment.
    raw: dict[str, str] = {}
    raw.update(_read_env_file_keys())
    for name, val in os.environ.items():
        if name.startswith(_ENV_PREFIX) and name != "UMBRA_API_URL" and val:
            raw[name] = val

    registry: dict[str, str] = {}
    for name, val in raw.items():
        if not val:
            continue
        if name == _ENV_PREFIX:  # exactly UMBRA_API_KEY
            registry[_PRIMARY] = val
        elif name.startswith(_ENV_PREFIX + "_"):  # UMBRA_API_KEY_<NAME>
            workspace = name[len(_ENV_PREFIX) + 1:].strip().lower()
            if workspace:
                registry[workspace] = val

    _key_registry = registry
    return registry


def _resolve_key(workspace: str = _PRIMARY) -> str:
    """Resolve a workspace name to its API key, or raise a clear error."""
    ws = (workspace or _PRIMARY).strip().lower()
    registry = _build_registry()
    if ws in registry:
        return registry[ws]
    if not registry:
        raise ValueError(
            "No Umbra API key found. Set UMBRA_API_KEY (or UMBRA_API_KEY_<NAME>) "
            "env var, or add it to ~/.claude/scripts/.env"
        )
    available = ", ".join(sorted(registry)) or "none"
    raise ValueError(
        f"No API key configured for workspace '{ws}'. Configured workspaces: "
        f"{available}. Set UMBRA_API_KEY_{ws.upper()} to add it."
    )


def _get_api_key(workspace: str = _PRIMARY) -> str:
    """Backwards-compatible alias for the primary (or a named) workspace key."""
    return _resolve_key(workspace)


def _build_url_registry() -> dict[str, str]:
    """Build {workspace -> base_url} from UMBRA_API_URL_<NAME> env vars.

    A key is bound to one business AND one environment, so a workspace holding
    a staging key must be sent to the staging host or it will 401 against
    production. UMBRA_API_URL stays the default for every workspace that has no
    override. Process env beats ~/.claude/scripts/.env. Cached after first build.
    """
    global _url_registry
    if _url_registry is not None:
        return _url_registry

    raw: dict[str, str] = {}
    raw.update(_read_env_file(_URL_PREFIX))
    for name, val in os.environ.items():
        if name.startswith(_URL_PREFIX) and val:
            raw[name] = val

    registry: dict[str, str] = {}
    for name, val in raw.items():
        workspace = name[len(_URL_PREFIX):].strip().lower()
        if workspace and val:
            registry[workspace] = val.rstrip("/")

    _url_registry = registry
    return registry


def _base_url(workspace: str = _PRIMARY) -> str:
    """Resolve the API base URL for a workspace (UMBRA_BASE_URL unless overridden)."""
    ws = (workspace or _PRIMARY).strip().lower()
    return _build_url_registry().get(ws, UMBRA_BASE_URL.rstrip("/"))


# ============================================================================
# HTTP helpers
# ============================================================================

def _headers(workspace: str = _PRIMARY, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"X-Api-Key": _resolve_key(workspace), "Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _get(path: str, params: dict | None = None, workspace: str = _PRIMARY) -> Any:
    url = f"{_base_url(workspace)}{path}"
    log.info("GET %s [ws=%s]", url, workspace)
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_headers(workspace), params=params or {})
        resp.raise_for_status()
        return resp.json()


def _post(path: str, data: dict, workspace: str = _PRIMARY,
          idempotency_key: str | None = None) -> Any:
    url = f"{_base_url(workspace)}{path}"
    log.info("POST %s [ws=%s]", url, workspace)
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, headers=_headers(workspace, idempotency_key), json=data)
        resp.raise_for_status()
        return resp.json()


def _put(path: str, data: dict, workspace: str = _PRIMARY) -> Any:
    url = f"{_base_url(workspace)}{path}"
    log.info("PUT %s [ws=%s]", url, workspace)
    with httpx.Client(timeout=30) as client:
        resp = client.put(url, headers=_headers(workspace), json=data)
        resp.raise_for_status()
        return resp.json()


def _delete(path: str, workspace: str = _PRIMARY) -> Any:
    url = f"{_base_url(workspace)}{path}"
    log.info("DELETE %s [ws=%s]", url, workspace)
    with httpx.Client(timeout=30) as client:
        resp = client.delete(url, headers=_headers(workspace))
        resp.raise_for_status()
        return resp.json()


def _idem(idempotency_key: str | None) -> str:
    """Return the caller's Idempotency-Key, or mint one.

    Every write goes out with a key. An agent retries on timeout without a
    human deciding to, and an unkeyed retry of "create bill" or "pay bill" is a
    duplicated supplier payment. Pass the SAME key to make a deliberate retry
    replay the original response instead of executing twice; replaying a key
    with a different body is rejected (422) rather than silently swallowed.
    """
    return (idempotency_key or "").strip() or str(uuid.uuid4())


def _ok(result: Any) -> str:
    return json.dumps(result, indent=2, default=str)


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        detail = e.response.text[:500]
        out: dict[str, Any] = {"error": f"HTTP {code}", "detail": detail}
        if code == 403 and "permission" in detail:
            out["hint"] = (
                "The API key is missing this scope. bills, journal, reports, employees "
                "and payroll only became grantable on 2026-08-29 — a key minted before "
                "then cannot hold them and must be re-minted with the scope."
            )
        elif code == 404:
            # The API cannot distinguish "no such row" from "another business's
            # row", and saying which would leak cross-tenant existence.
            out["hint"] = "Not found. Check the public id (UUID) and the workspace."
        return json.dumps(out)
    return json.dumps({"error": str(e)})


def _csv_list(value: str | None) -> list[str]:
    """Split a comma-separated string into a trimmed list (empty-safe)."""
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def _line_total(item: dict) -> float:
    """Net dollar total of one document line.

    Prefers the caller's explicit `total`; falls back to quantity x unitPrice
    less discountPercent so a caller who omits it still gets a coherent header.
    """
    if item.get("total") is not None:
        return float(item["total"])
    gross = float(item.get("quantity", 1) or 0) * float(item.get("unitPrice", 0) or 0)
    return gross * (1 - float(item.get("discountPercent", 0) or 0) / 100.0)


def _derive_document_totals(items: list[dict], payload: dict) -> dict[str, float]:
    """Derive {subtotal, taxAmount, total} in DOLLARS from a line-item array.

    Used only to backfill keys the caller did not send. The API patches the
    header figures and the lines independently: sending `items` alone replaces
    the lines but leaves `subtotal`/`total` at their old values, which is how a
    quote ends up with lines that add to $250 under a total that still reads
    $575. Deriving them keeps the document internally consistent.
    """
    subtotal = round(sum(_line_total(i) for i in items), 2)
    if payload.get("taxAmount") is not None:
        tax = float(payload["taxAmount"])
    else:
        tax = round(sum(_line_total(i) * float(i.get("taxRate", 0) or 0) / 100.0
                        for i in items), 2)
    discount = float(payload.get("discountAmount") or 0)
    return {
        "subtotal": subtotal,
        "taxAmount": tax,
        "total": round(subtotal - discount + tax, 2),
    }


# ============================================================================
# MCP Server
# ============================================================================

mcp = FastMCP(
    "Umbra ERP",
    instructions=(
        "Manage ERP + CRM data (customers, invoices, products, quotes, payments, employees, "
        "leave requests, webhooks, and CRM contacts, leads, activities) via the Umbra ERP "
        "public API. Used by Mufasa for business operations. Multi-workspace: every tool takes "
        "an optional `workspace` argument (default 'primary') to target a specific business; "
        "extra workspaces are configured via UMBRA_API_KEY_<NAME> env vars. Use list_workspaces "
        "or check_status to see which workspaces are configured."
    ),
)


# ── CUSTOMERS ────────────────────────────────────────────────────────────────

@mcp.tool()
def list_customers(
    limit: int = 50,
    skip: int = 0,
    search: str | None = None,
    industry: str | None = None,
    country: str | None = None,
    workspace: str = "primary",
) -> str:
    """List customers with optional filters.

    Args:
        limit: Max results (1-100, default 50)
        skip: Offset for pagination
        search: Search by company name, contact name, or email
        industry: Filter by industry
        country: Filter by country
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if search:
        params["search"] = search
    if industry:
        params["industry"] = industry
    if country:
        params["country"] = country
    try:
        return _ok(_get("/v1/customers", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_customer(customer_id: str, workspace: str = "primary") -> str:
    """Get a single customer by ID.

    Args:
        customer_id: The customer's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/customers/{customer_id}", workspace=workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_customer(
    company_name: str,
    contact_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    industry: str | None = None,
    country: str | None = None,
    currency: str = "USD",
    address: str | None = None,
    city: str | None = None,
    website: str | None = None,
    notes: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a new customer in the ERP.

    Args:
        company_name: Company/organization name (required)
        contact_name: Primary contact person's name
        email: Contact email
        phone: Contact phone (e.g., "+263771234567")
        industry: Industry/sector
        country: Country name
        currency: Currency code (default USD)
        address: Street address
        city: City
        website: Website URL
        notes: Additional notes
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {"companyName": company_name, "currency": currency}
    if contact_name:
        data["contactName"] = contact_name
    if email:
        data["email"] = email
    if phone:
        data["phone"] = phone
    if industry:
        data["industry"] = industry
    if country:
        data["country"] = country
    if address:
        data["address"] = address
    if city:
        data["city"] = city
    if website:
        data["website"] = website
    if notes:
        data["notes"] = notes
    try:
        return _ok(_post("/v1/customers", data, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def update_customer(customer_id: str, updates: str, workspace: str = "primary") -> str:
    """Update a customer. Pass a JSON string of fields to update.

    Args:
        customer_id: The customer's public UUID
        updates: JSON string with fields to update, e.g. '{"companyName": "New Name", "phone": "+263..."}'
        workspace: Target business workspace (default "primary")
    """
    try:
        data = json.loads(updates)
        return _ok(_put(f"/v1/customers/{customer_id}", data, workspace))
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in updates parameter"})
    except Exception as e:
        return _err(e)


@mcp.tool()
def delete_customer(customer_id: str, workspace: str = "primary") -> str:
    """Delete a customer.

    Args:
        customer_id: The customer's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_delete(f"/v1/customers/{customer_id}", workspace))
    except Exception as e:
        return _err(e)


# ── INVOICES ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_invoices(
    limit: int = 50,
    skip: int = 0,
    status: str | None = None,
    customer_id: str | None = None,
    workspace: str = "primary",
) -> str:
    """List invoices with optional filters.

    Args:
        limit: Max results (1-100)
        skip: Offset for pagination
        status: Filter by status (draft, sent, paid, overdue, cancelled)
        customer_id: Filter by customer UUID
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if status:
        params["status"] = status
    if customer_id:
        params["customer_id"] = customer_id
    try:
        return _ok(_get("/v1/invoices", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_invoice(invoice_id: str, workspace: str = "primary") -> str:
    """Get a single invoice with line items.

    Args:
        invoice_id: The invoice's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/invoices/{invoice_id}", workspace=workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_invoice(
    customer_id: str,
    invoice_date: str,
    due_date: str,
    currency: str,
    subtotal: float,
    total: float,
    balance_due: float,
    items: str,
    notes: str | None = None,
    tax_amount: float | None = None,
    discount_amount: float | None = None,
    workspace: str = "primary",
) -> str:
    """Create a new invoice.

    Args:
        customer_id: Customer UUID
        invoice_date: Date string (YYYY-MM-DD)
        due_date: Due date (YYYY-MM-DD)
        currency: Currency code (USD, ZAR, etc.)
        subtotal: Subtotal in dollars (e.g., 500.00)
        total: Total in dollars
        balance_due: Balance due in dollars
        items: JSON string array of line items, e.g. '[{"title":"Consulting","quantity":2,"unitPrice":250.00,"total":500.00}]'
        notes: Optional notes
        tax_amount: Tax amount in dollars
        discount_amount: Discount amount in dollars
        workspace: Target business workspace (default "primary")
    """
    try:
        line_items = json.loads(items)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in items parameter"})

    data: dict[str, Any] = {
        "customerId": customer_id,
        "invoiceDate": invoice_date,
        "dueDate": due_date,
        "currency": currency,
        "subtotal": subtotal,
        "total": total,
        "balanceDue": balance_due,
        "items": line_items,
    }
    if notes:
        data["notes"] = notes
    if tax_amount is not None:
        data["taxAmount"] = tax_amount
    if discount_amount is not None:
        data["discountAmount"] = discount_amount
    try:
        return _ok(_post("/v1/invoices", data, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def update_invoice(invoice_id: str, updates: str, workspace: str = "primary") -> str:
    """Update an invoice. Pass a JSON string of fields to update.

    Args:
        invoice_id: The invoice's public UUID
        updates: JSON string with fields to update, e.g. '{"status": "sent", "notes": "Updated"}'
        workspace: Target business workspace (default "primary")
    """
    try:
        data = json.loads(updates)
        return _ok(_put(f"/v1/invoices/{invoice_id}", data, workspace))
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in updates parameter"})
    except Exception as e:
        return _err(e)


@mcp.tool()
def delete_invoice(invoice_id: str, workspace: str = "primary") -> str:
    """Delete an invoice.

    Args:
        invoice_id: The invoice's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_delete(f"/v1/invoices/{invoice_id}", workspace))
    except Exception as e:
        return _err(e)


# ── PRODUCTS ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_products(
    limit: int = 50,
    skip: int = 0,
    category: str | None = None,
    product_type: str | None = None,
    workspace: str = "primary",
) -> str:
    """List products with optional filters.

    Args:
        limit: Max results (1-100)
        skip: Offset for pagination
        category: Filter by category
        product_type: Filter by type (physical, digital, service)
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if category:
        params["category"] = category
    if product_type:
        params["type"] = product_type
    try:
        return _ok(_get("/v1/products", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_product(product_id: str, workspace: str = "primary") -> str:
    """Get a single product.

    Args:
        product_id: The product's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/products/{product_id}", workspace=workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_product(
    name: str,
    price: float,
    currency: str = "USD",
    sku: str | None = None,
    cost_price: float | None = None,
    description: str | None = None,
    category: str | None = None,
    product_type: str = "physical",
    workspace: str = "primary",
) -> str:
    """Create a new product.

    Args:
        name: Product name (required)
        price: Selling price in dollars (e.g., 99.99)
        currency: Currency code (default USD)
        sku: Stock keeping unit code
        cost_price: Cost price in dollars
        description: Product description
        category: Product category
        product_type: Type: physical, digital, or service
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {"name": name, "price": price, "currency": currency, "type": product_type}
    if sku:
        data["sku"] = sku
    if cost_price is not None:
        data["costPrice"] = cost_price
    if description:
        data["description"] = description
    if category:
        data["category"] = category
    try:
        return _ok(_post("/v1/products", data, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def update_product(product_id: str, updates: str, workspace: str = "primary") -> str:
    """Update a product. Pass a JSON string of fields to update.

    Args:
        product_id: The product's public UUID
        updates: JSON string with fields to update
        workspace: Target business workspace (default "primary")
    """
    try:
        data = json.loads(updates)
        return _ok(_put(f"/v1/products/{product_id}", data, workspace))
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in updates parameter"})
    except Exception as e:
        return _err(e)


@mcp.tool()
def delete_product(product_id: str, workspace: str = "primary") -> str:
    """Delete a product.

    Args:
        product_id: The product's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_delete(f"/v1/products/{product_id}", workspace))
    except Exception as e:
        return _err(e)


# ── QUOTES ───────────────────────────────────────────────────────────────────

@mcp.tool()
def list_quotes(
    limit: int = 50,
    skip: int = 0,
    status: str | None = None,
    customer_id: str | None = None,
    workspace: str = "primary",
) -> str:
    """List quotes with optional filters.

    Args:
        limit: Max results (1-100)
        skip: Offset for pagination
        status: Filter by status (draft, sent, accepted, rejected, expired)
        customer_id: Filter by customer UUID
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if status:
        params["status"] = status
    if customer_id:
        params["customer_id"] = customer_id
    try:
        return _ok(_get("/v1/quotes", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_quote(quote_id: str, workspace: str = "primary") -> str:
    """Get a single quote with line items.

    Args:
        quote_id: The quote's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/quotes/{quote_id}", workspace=workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_quote(
    customer_id: str,
    title: str,
    quote_date: str,
    expiry_date: str,
    subtotal: float,
    total: float,
    items: str,
    notes: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a new quote.

    Args:
        customer_id: Customer UUID
        title: Quote title
        quote_date: Date string (YYYY-MM-DD)
        expiry_date: Expiry date (YYYY-MM-DD)
        subtotal: Subtotal in dollars
        total: Total in dollars
        items: JSON string array of line items, e.g. '[{"description":"Widget","quantity":5,"unitPrice":200.00,"total":1000.00}]'
            Each item takes: description, quantity, unitPrice, total, and
            optionally productId, discountPercent, taxRate, sortOrder.
            productId is a product's public UUID (from list_products/get_product),
            which the API resolves to the catalogue product; an unknown or
            omitted productId leaves the line as free text rather than failing.
            Prices are in dollars.
        notes: Optional notes
        workspace: Target business workspace (default "primary")
    """
    try:
        line_items = json.loads(items)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in items parameter"})

    data: dict[str, Any] = {
        "customerId": customer_id,
        "title": title,
        "quoteDate": quote_date,
        "expiryDate": expiry_date,
        "subtotal": subtotal,
        "total": total,
        "items": line_items,
    }
    if notes:
        data["notes"] = notes
    try:
        return _ok(_post("/v1/quotes", data, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def update_quote(quote_id: str, updates: str, workspace: str = "primary") -> str:
    """Update a quote. Pass a JSON string of fields to update.

    Scalar fields are safe partial updates: send only the keys you want
    changed (title, notes, status, reference, terms, footer, currency,
    customerId, quoteNumber, quoteDate, expiryDate, subtotal, discountAmount,
    discountPercent, taxAmount, total, customFields) and the rest are left
    alone.

    `items` is the exception: it is a FULL REPLACEMENT, not a merge. Sending
    it swaps the quote's line items for exactly the array given, so include
    every line you want to keep, not just the one you are changing. Omit the
    key to leave the existing lines untouched; send [] to clear them. Each
    item takes {productId?, description, quantity, unitPrice, total} and
    optionally discountPercent, taxRate, sortOrder: the same shape
    create_quote accepts, with productId as a product's public UUID and
    prices in dollars. A malformed items array rejects the whole update, so
    the scalar fields cannot land without the lines.

    Editing lines in place replaces the old delete-and-recreate workaround,
    which burned the quote number and its history.

    MONEY SAFETY. The API patches the header figures and the lines
    independently, so `items` on its own moves the lines and leaves `subtotal`
    / `total` at their old values — a quote whose lines add to $250 under a
    total that still reads $575. When you send `items` without `subtotal` /
    `total`, this tool derives them from the lines (sum of line totals, plus
    per-line `taxRate`, less any `discountAmount` you sent) so the document
    cannot contradict itself. Send them explicitly to override the derivation.

    Whenever `items` is sent the quote is re-read after the write and the tool
    returns the SERVER's post-write state under `quote`, with `itemsSent`,
    `itemsStored` and `itemsRoundTripped` so a silently dropped line array is
    visible instead of reading as a bare 200.

    Args:
        quote_id: The quote's public UUID
        updates: JSON string with fields to update
        workspace: Target business workspace (default "primary")
    """
    try:
        data = json.loads(updates)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in updates parameter"})

    items = data.get("items") if "items" in data else None
    if items is not None:
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            return json.dumps({"error": "'items' must be an array of objects"})
        derived = _derive_document_totals(items, data)
        for field in ("subtotal", "taxAmount", "total"):
            if field not in data:
                data[field] = derived[field]

    try:
        result = _put(f"/v1/quotes/{quote_id}", data, workspace)
        if items is None:
            return _ok(result)
        # Re-read: the write is not done until the server says the lines moved.
        fresh = _get(f"/v1/quotes/{quote_id}", workspace=workspace).get("data") or {}
        stored = fresh.get("items") or []
        out: dict[str, Any] = {
            "quote": fresh,
            "itemsSent": len(items),
            "itemsStored": len(stored),
            "itemsRoundTripped": len(stored) == len(items),
            "derivedTotals": {k: v for k, v in data.items()
                              if k in ("subtotal", "taxAmount", "total")},
        }
        if not out["itemsRoundTripped"]:
            out["warning"] = (
                f"Sent {len(items)} line(s) but the quote now stores {len(stored)}. "
                "The line items did NOT land — do not send this quote to a customer."
            )
        return _ok(out)
    except Exception as e:
        return _err(e)


@mcp.tool()
def delete_quote(quote_id: str, workspace: str = "primary") -> str:
    """Delete a quote.

    Args:
        quote_id: The quote's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_delete(f"/v1/quotes/{quote_id}", workspace))
    except Exception as e:
        return _err(e)


# ── PAYMENTS ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_payments(
    limit: int = 50,
    skip: int = 0,
    customer_id: str | None = None,
    workspace: str = "primary",
) -> str:
    """List payments with optional filters.

    Args:
        limit: Max results (1-100)
        skip: Offset for pagination
        customer_id: Filter by customer UUID
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if customer_id:
        params["customer_id"] = customer_id
    try:
        return _ok(_get("/v1/payments", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_payment(payment_id: str, workspace: str = "primary") -> str:
    """Get a single payment.

    Args:
        payment_id: The payment's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/payments/{payment_id}", workspace=workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_payment(
    customer_id: str,
    amount: float,
    currency: str = "USD",
    payment_method: str = "bank_transfer",
    payment_reference: str | None = None,
    invoice_id: str | None = None,
    notes: str | None = None,
    workspace: str = "primary",
) -> str:
    """Record a payment.

    Args:
        customer_id: Customer UUID
        amount: Payment amount in dollars (e.g., 250.00)
        currency: Currency code (default USD)
        payment_method: Method: bank_transfer, cash, card, mobile_money, other
        payment_reference: External reference number
        invoice_id: Link to invoice UUID (optional)
        notes: Additional notes
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {
        "customerId": customer_id,
        "amount": amount,
        "currency": currency,
        "paymentMethod": payment_method,
    }
    if payment_reference:
        data["paymentReference"] = payment_reference
    if invoice_id:
        data["invoiceId"] = invoice_id
    if notes:
        data["notes"] = notes
    try:
        return _ok(_post("/v1/payments", data, workspace))
    except Exception as e:
        return _err(e)


# ── CONTACTS (CRM) ───────────────────────────────────────────────────────────

@mcp.tool()
def list_contacts(
    limit: int = 50,
    skip: int = 0,
    search: str | None = None,
    workspace: str = "primary",
) -> str:
    """List CRM contacts with optional search.

    Args:
        limit: Max results (1-100, default 50)
        skip: Offset for pagination
        search: Search by name, email, phone, or company
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if search:
        params["search"] = search
    try:
        return _ok(_get("/v1/contacts", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_contact(contact_id: str, workspace: str = "primary") -> str:
    """Get a single CRM contact by ID.

    Args:
        contact_id: The contact's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/contacts/{contact_id}", workspace=workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_contact(
    first_name: str,
    last_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    job_title: str | None = None,
    contact_company: str | None = None,
    roles: str | None = None,
    tags: str | None = None,
    notes: str | None = None,
    custom_fields: str | None = None,
    address: str | None = None,
    city: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a new CRM contact.

    Args:
        first_name: Contact's first name (required)
        last_name: Contact's last name
        email: Email address
        phone: Phone number (e.g., "+263771234567")
        job_title: Job title / role at their company
        contact_company: Company/organization the contact belongs to
        roles: Comma-separated roles, e.g. "decision_maker,billing"
        tags: Comma-separated tags, e.g. "vip,newsletter"
        notes: Free-text notes
        custom_fields: JSON string object of custom fields, e.g. '{"linkedin":"..."}'
        address: Street address
        city: City
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {"firstName": first_name}
    if last_name:
        data["lastName"] = last_name
    if email:
        data["email"] = email
    if phone:
        data["phone"] = phone
    if job_title:
        data["jobTitle"] = job_title
    if contact_company:
        data["contactCompany"] = contact_company
    if roles:
        data["roles"] = _csv_list(roles)
    if tags:
        data["tags"] = _csv_list(tags)
    if notes:
        data["notes"] = notes
    if custom_fields:
        try:
            data["customFields"] = json.loads(custom_fields)
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON in custom_fields parameter"})
    if address:
        data["address"] = address
    if city:
        data["city"] = city
    try:
        return _ok(_post("/v1/contacts", data, workspace))
    except Exception as e:
        return _err(e)


# ── LEADS (CRM) ──────────────────────────────────────────────────────────────

@mcp.tool()
def list_leads(
    limit: int = 50,
    skip: int = 0,
    search: str | None = None,
    status: str | None = None,
    workspace: str = "primary",
) -> str:
    """List CRM leads with optional filters.

    Args:
        limit: Max results (1-100, default 50)
        skip: Offset for pagination
        search: Search by name, email, phone, or company
        status: Filter by pipeline status (e.g., new, contacted, qualified, won, lost)
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if search:
        params["search"] = search
    if status:
        params["status"] = status
    try:
        return _ok(_get("/v1/leads", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_lead(
    first_name: str,
    last_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    company: str | None = None,
    job_title: str | None = None,
    source: str | None = None,
    status: str | None = None,
    score: int | None = None,
    notes: str | None = None,
    tags: str | None = None,
    custom_fields: str | None = None,
    lead_temperature: str | None = None,
    next_follow_up_date: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a new CRM lead.

    Args:
        first_name: Lead's first name (required)
        last_name: Lead's last name
        email: Email address
        phone: Phone number (e.g., "+263771234567")
        company: Company/organization the lead belongs to
        job_title: Job title
        source: Lead source (e.g., website, referral, whatsapp, cold_call)
        status: Pipeline status (e.g., new, contacted, qualified, won, lost)
        score: Numeric lead score (higher = hotter)
        notes: Free-text notes
        tags: Comma-separated tags, e.g. "enterprise,inbound"
        custom_fields: JSON string object of custom fields, e.g. '{"budget":"5000"}'
        lead_temperature: Lead temperature (e.g., hot, warm, cold)
        next_follow_up_date: Next follow-up date (YYYY-MM-DD or ISO datetime)
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {"firstName": first_name}
    if last_name:
        data["lastName"] = last_name
    if email:
        data["email"] = email
    if phone:
        data["phone"] = phone
    if company:
        data["company"] = company
    if job_title:
        data["jobTitle"] = job_title
    if source:
        data["source"] = source
    if status:
        data["status"] = status
    if score is not None:
        data["score"] = score
    if notes:
        data["notes"] = notes
    if tags:
        data["tags"] = _csv_list(tags)
    if custom_fields:
        try:
            data["customFields"] = json.loads(custom_fields)
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON in custom_fields parameter"})
    if lead_temperature:
        data["leadTemperature"] = lead_temperature
    if next_follow_up_date:
        data["nextFollowUpDate"] = next_follow_up_date
    try:
        return _ok(_post("/v1/leads", data, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def update_lead(lead_id: str, updates: str, workspace: str = "primary") -> str:
    """Update a CRM lead. Pass a JSON string of the fields to change.

    Partial update: only the keys you send are touched. Requires the `leads`
    permission on the API key.

    Updatable keys: firstName, lastName, email, phone, company, jobTitle,
    source, status, score, notes, tags (array), customFields (object),
    leadTemperature, utmSource, utmMedium, utmCampaign, nextFollowUpDate
    (YYYY-MM-DD or ISO-8601). Any other key is ignored by the API rather than
    rejected, so check a field name here before relying on it.

    `status` is validated against the LeadStatus enum and a bad value returns
    400 listing the valid ones. Setting status to "converted" by hand does NOT
    create a customer — use convert_lead_to_customer for that, or the lead is
    marked converted with nothing on the other side.

    Args:
        lead_id: The lead's public UUID
        updates: JSON string with fields to update, e.g. '{"status": "qualified", "score": 80}'
        workspace: Target business workspace (default "primary")
    """
    try:
        data = json.loads(updates)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in updates parameter"})
    try:
        return _ok(_put(f"/v1/leads/{lead_id}", data, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def convert_lead_to_customer(
    lead_id: str,
    name: str | None = None,
    company_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    currency: str | None = None,
    idempotency_key: str | None = None,
    workspace: str = "primary",
) -> str:
    """Convert a CRM lead into a customer and mark the lead `converted`.

    SIDE EFFECTS: creates a customer record and writes `converted` back onto
    the lead. Requires the `leads` permission.

    Every argument except lead_id is optional and falls back to the lead's own
    value, so the usual call is just the lead id.

    Safe to retry. An already-converted lead returns its EXISTING customer with
    `alreadyConverted: true` and HTTP 200 rather than creating a second one;
    converting twice would split one relationship across two customer records
    and scatter that customer's invoices between them.

    Returns {"data": {id, name, email, phone, leadId}, "alreadyConverted": bool}
    where `id` is the customer's public UUID (feed that to get_customer /
    create_invoice) and `leadId` is the lead's public UUID.

    Args:
        lead_id: The lead's public UUID
        name: Override the customer's display name (default: lead's full name)
        company_name: Override company name (default: the lead's company)
        email: Override email (default: the lead's email)
        phone: Override phone (default: the lead's phone or mobile)
        currency: Currency code for the new customer (default: the business currency)
        idempotency_key: Reuse the same key to replay a timed-out call instead
            of re-running it. Auto-generated when omitted.
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {}
    if name:
        data["name"] = name
    if company_name:
        data["companyName"] = company_name
    if email:
        data["email"] = email
    if phone:
        data["phone"] = phone
    if currency:
        data["currency"] = currency
    try:
        return _ok(_post(f"/v1/leads/{lead_id}/convert", data, workspace,
                         _idem(idempotency_key)))
    except Exception as e:
        return _err(e)


# ── ACTIVITIES (CRM) ─────────────────────────────────────────────────────────

@mcp.tool()
def list_activities(
    limit: int = 50,
    skip: int = 0,
    activity_type: str | None = None,
    is_completed: bool | None = None,
    linked_to_type: str | None = None,
    linked_to_id: str | None = None,
    workspace: str = "primary",
) -> str:
    """List CRM activities with optional filters.

    Args:
        limit: Max results (1-100, default 50)
        skip: Offset for pagination
        activity_type: Filter by type (call, email, meeting, task, note, demo,
            proposal, whatsapp, follow_up, site_visit)
        is_completed: Filter by completion state (true/false)
        linked_to_type: Filter by linked record type (lead, contact, customer, deal)
        linked_to_id: Filter by linked record public UUID
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if activity_type:
        params["type"] = activity_type
    if is_completed is not None:
        params["isCompleted"] = is_completed
    if linked_to_type:
        params["linkedToType"] = linked_to_type
    if linked_to_id:
        params["linkedToId"] = linked_to_id
    try:
        return _ok(_get("/v1/activities", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_activity(
    activity_type: str,
    subject: str,
    description: str | None = None,
    due_date: str | None = None,
    scheduled_at: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    assigned_to: str | None = None,
    linked_to_type: str | None = None,
    linked_to_id: str | None = None,
    location: str | None = None,
    attendees: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a new CRM activity (call, email, meeting, task, note, etc.).

    Args:
        activity_type: Activity type (call, email, meeting, task, note, demo,
            proposal, whatsapp, follow_up, site_visit) (required)
        subject: Short subject/title (required)
        description: Longer description / body
        due_date: Due date (YYYY-MM-DD or ISO datetime)
        scheduled_at: Scheduled start (ISO datetime)
        priority: Priority (e.g., low, medium, high, urgent)
        status: Status (e.g., pending, in_progress, completed, cancelled)
        assigned_to: Assignee — user/employee public UUID
        linked_to_type: Type of record this links to (lead, contact, customer, deal)
        linked_to_id: Public UUID of the linked record (used with linked_to_type)
        location: Location (meeting/site_visit); stored in activity metadata
        attendees: Comma-separated attendees; stored in activity metadata
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {"type": activity_type, "subject": subject}
    if description:
        data["description"] = description
    if due_date:
        data["dueDate"] = due_date
    if scheduled_at:
        data["scheduledAt"] = scheduled_at
    if priority:
        data["priority"] = priority
    if status:
        data["status"] = status
    if assigned_to:
        data["assignedTo"] = assigned_to
    if linked_to_type and linked_to_id:
        data["linkedTo"] = {"type": linked_to_type, "id": linked_to_id}
    if location:
        data["location"] = location
    if attendees:
        data["attendees"] = _csv_list(attendees)
    try:
        return _ok(_post("/v1/activities", data, workspace))
    except Exception as e:
        return _err(e)


# ── EMPLOYEES ────────────────────────────────────────────────────────────────

@mcp.tool()
def list_employees(
    limit: int = 50,
    skip: int = 0,
    status: str | None = None,
    department: str | None = None,
    search: str | None = None,
    workspace: str = "primary",
) -> str:
    """List employees with optional filters.

    Args:
        limit: Max results (1-100)
        skip: Offset for pagination
        status: Filter by status (active, on_leave, suspended, terminated)
        department: Filter by department name
        search: Search by name, email, or employee ID
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if status:
        params["status"] = status
    if department:
        params["department"] = department
    if search:
        params["search"] = search
    try:
        return _ok(_get("/v1/employees", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_employee(employee_id: str, workspace: str = "primary") -> str:
    """Get a single employee. Includes sensitive fields (salary, bank details) since using secret key.

    Args:
        employee_id: The employee's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/employees/{employee_id}", workspace=workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_employee(
    first_name: str,
    last_name: str,
    email: str | None = None,
    phone: str | None = None,
    job_title: str | None = None,
    department: str | None = None,
    employment_type: str = "full_time",
    hire_date: str | None = None,
    salary: float | None = None,
    salary_currency: str = "USD",
    country: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a new employee.

    Args:
        first_name: First name (required)
        last_name: Last name (required)
        email: Email address
        phone: Phone number
        job_title: Job title
        department: Department name
        employment_type: Type: full_time, part_time, contract
        hire_date: Hire date (YYYY-MM-DD)
        salary: Monthly salary in dollars
        salary_currency: Currency (default USD)
        country: Country
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {
        "firstName": first_name,
        "lastName": last_name,
        "employmentType": employment_type,
        "salaryCurrency": salary_currency,
    }
    if email:
        data["email"] = email
    if phone:
        data["phone"] = phone
    if job_title:
        data["jobTitle"] = job_title
    if department:
        data["department"] = department
    if hire_date:
        data["hireDate"] = hire_date
    if salary is not None:
        data["salary"] = salary
    if country:
        data["country"] = country
    try:
        return _ok(_post("/v1/employees", data, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def update_employee(employee_id: str, updates: str, workspace: str = "primary") -> str:
    """Update an employee. Pass a JSON string of fields to update.

    Args:
        employee_id: The employee's public UUID
        updates: JSON string with fields to update, e.g. '{"jobTitle": "CTO", "salary": 10000}'
        workspace: Target business workspace (default "primary")
    """
    try:
        data = json.loads(updates)
        return _ok(_put(f"/v1/employees/{employee_id}", data, workspace))
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in updates parameter"})
    except Exception as e:
        return _err(e)


@mcp.tool()
def delete_employee(employee_id: str, workspace: str = "primary") -> str:
    """Soft-delete an employee (marks as terminated/inactive).

    Args:
        employee_id: The employee's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_delete(f"/v1/employees/{employee_id}", workspace))
    except Exception as e:
        return _err(e)


# ── LEAVE REQUESTS ───────────────────────────────────────────────────────────

@mcp.tool()
def list_leave_requests(employee_id: str, limit: int = 50, skip: int = 0, workspace: str = "primary") -> str:
    """List leave requests for a specific employee.

    Args:
        employee_id: The employee's public UUID
        limit: Max results (1-100)
        skip: Offset for pagination
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    try:
        return _ok(_get(f"/v1/employees/{employee_id}/leave", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_leave_request(
    employee_id: str,
    leave_type: str,
    start_date: str,
    end_date: str,
    days_requested: float,
    reason: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a leave request for an employee. Starts as 'pending'.

    Args:
        employee_id: The employee's public UUID
        leave_type: Type: annual, sick, maternity, paternity, unpaid, compassionate, other
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        days_requested: Number of days
        reason: Reason for leave
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {
        "leaveType": leave_type,
        "startDate": start_date,
        "endDate": end_date,
        "daysRequested": days_requested,
    }
    if reason:
        data["reason"] = reason
    try:
        return _ok(_post(f"/v1/employees/{employee_id}/leave", data, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def update_leave_request(leave_id: str, updates: str, workspace: str = "primary") -> str:
    """Update a leave request (approve, reject, cancel, or modify).

    Args:
        leave_id: The leave request's public UUID
        updates: JSON string, e.g. '{"status": "approved"}' or '{"status": "rejected", "rejectionReason": "..."}'
        workspace: Target business workspace (default "primary")
    """
    try:
        data = json.loads(updates)
        return _ok(_put(f"/v1/leave/{leave_id}", data, workspace))
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in updates parameter"})
    except Exception as e:
        return _err(e)


@mcp.tool()
def delete_leave_request(leave_id: str, workspace: str = "primary") -> str:
    """Delete a leave request.

    Args:
        leave_id: The leave request's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_delete(f"/v1/leave/{leave_id}", workspace))
    except Exception as e:
        return _err(e)


# ── WEBHOOKS ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_webhooks(limit: int = 50, skip: int = 0, workspace: str = "primary") -> str:
    """List all registered webhooks.

    Args:
        limit: Max results (1-100)
        skip: Offset for pagination
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get("/v1/webhooks", {"limit": limit, "skip": skip}, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_webhook(
    name: str,
    url: str,
    events: str,
    workspace: str = "primary",
) -> str:
    """Create a webhook to receive event notifications.

    Args:
        name: Webhook name (e.g., "My CRM Integration")
        url: HTTPS URL to POST events to
        events: Comma-separated events, e.g. "customer.created,invoice.created".
                Available: customer.created/updated/deleted, invoice.created/updated/deleted,
                product.created/updated/deleted, quote.created/updated/deleted,
                payment.created, employee.created/updated/deleted, leave.created/updated/deleted
        workspace: Target business workspace (default "primary")
    """
    event_list = _csv_list(events)
    try:
        return _ok(_post("/v1/webhooks", {"name": name, "url": url, "events": event_list}, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def delete_webhook(webhook_id: str, workspace: str = "primary") -> str:
    """Delete a webhook.

    Args:
        webhook_id: The webhook's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_delete(f"/v1/webhooks/{webhook_id}", workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def test_webhook(webhook_id: str, workspace: str = "primary") -> str:
    """Send a test event to a webhook endpoint.

    Args:
        webhook_id: The webhook's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_post(f"/v1/webhooks/{webhook_id}/test", {}, workspace))
    except Exception as e:
        return _err(e)


# ── WORKSPACES / STATUS ──────────────────────────────────────────────────────

@mcp.tool()
def list_workspaces() -> str:
    """List the business workspaces that have an API key configured (names only).

    Never returns key values. Workspaces come from UMBRA_API_KEY (-> "primary")
    and UMBRA_API_KEY_<NAME> env vars (or ~/.claude/scripts/.env).
    """
    registry = _build_registry()
    return json.dumps({
        "workspaces_configured": sorted(registry),
        "count": len(registry),
        "default": "primary",
    }, indent=2)


@mcp.tool()
def check_status(workspace: str = "primary") -> str:
    """Check Umbra ERP API connectivity and authentication status for a workspace.

    Args:
        workspace: Target business workspace to test (default "primary")
    """
    registry = _build_registry()
    try:
        api_key = _resolve_key(workspace)
        key_prefix = api_key[:12] + "..." if len(api_key) > 12 else api_key
        result = _get("/v1/customers", {"limit": 1}, workspace)
        return json.dumps({
            "status": "ok",
            "base_url": UMBRA_BASE_URL,
            "workspace": (workspace or "primary").strip().lower(),
            "workspaces_configured": sorted(registry),
            "api_key_prefix": key_prefix,
            "customers_accessible": "pagination" in result,
            "resources": [
                "customers", "invoices", "products", "quotes", "payments",
                "contacts", "leads", "activities",
                "employees", "leave_requests", "webhooks",
            ],
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "workspace": (workspace or "primary").strip().lower(),
            "workspaces_configured": sorted(registry),
            "error": str(e),
        })


# ============================================================================
# Run server
# ============================================================================

if __name__ == "__main__":
    mcp.run()
