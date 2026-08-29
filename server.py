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


def _write_err(e: Exception, idempotency_key: str, verify_with: str) -> str:
    """Error shaping for writes, where a failure does NOT mean nothing happened.

    Several create/convert endpoints commit the row and then raise while
    recording the idempotency response, so the caller sees a 500 for a write
    that landed. Blind-retrying that mints a second bill, or a second invoice
    number off the gapless sequence. On any 5xx the caller is told to verify
    before retrying, and handed the key it used so a deliberate retry replays
    instead of duplicating.
    """
    payload = json.loads(_err(e))
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500:
        payload["writeMayHaveLanded"] = True
        payload["idempotencyKeyUsed"] = idempotency_key
        payload["action"] = (
            f"DO NOT blindly retry. The record may already exist; check with "
            f"{verify_with} first. If you do retry, pass this same "
            f"idempotency_key so the call replays instead of creating a duplicate."
        )
    elif isinstance(e, (httpx.TimeoutException, httpx.TransportError)):
        payload["writeMayHaveLanded"] = True
        payload["idempotencyKeyUsed"] = idempotency_key
        payload["action"] = (
            f"The request did not complete cleanly, so the write may or may not "
            f"have landed. Check with {verify_with} before retrying, and reuse "
            f"this idempotency_key if you do."
        )
    return json.dumps(payload)


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        detail = e.response.text[:500]
        out: dict[str, Any] = {"error": f"HTTP {code}", "detail": detail}
        if code == 403 and "permission" in detail:
            out["hint"] = (
                "The API key is missing this scope. bills, journal, reports, employees "
                "and payroll only became grantable on 2026-08-29; a key minted before "
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
    """TAX-INCLUSIVE dollar total of one document line.

    Umbra's line convention is tax-inclusive: `total` is what the line is
    worth with its tax and discount already in it, and `unitPrice` is likewise
    a tax-inclusive price. Both branches here return the same kind of number.

    Prefers the caller's explicit `total`, because that is the figure they
    displayed. Falls back to quantity x unitPrice less discountPercent, which
    under this convention is also tax-inclusive.
    """
    if item.get("total") is not None:
        return float(item["total"])
    gross = float(item.get("quantity", 1) or 0) * float(item.get("unitPrice", 0) or 0)
    return round(gross * (1 - float(item.get("discountPercent", 0) or 0) / 100.0), 2)


def _line_tax(item: dict) -> float:
    """Tax contained WITHIN one line's tax-inclusive total, in dollars.

    Uses the caller's explicit per-line `taxAmount` when present. Otherwise
    backs the tax out of the inclusive total at `taxRate`:
    tax = total - total / (1 + rate/100). A 100.00 line at 15% carries 13.04
    of tax over an 86.96 net, NOT 15.00 on top of 100.00.

    The API stores a line's `taxAmount` but never computes one from `taxRate`,
    so without this a caller who sends only a rate gets a quote whose header
    tax silently reads zero.
    """
    if item.get("taxAmount") is not None:
        return float(item["taxAmount"])
    rate = float(item.get("taxRate", 0) or 0)
    if not rate:
        return 0.0
    total = _line_total(item)
    return round(total - total / (1 + rate / 100.0), 2)


def _derive_document_totals(items: list[dict]) -> dict[str, float]:
    """Header figures implied by a line array, in DOLLARS.

    Mirrors the API's own `_derive_totals_from_items` exactly, so the tool and
    the server never disagree about what a document is worth:

        total    = sum(line total)          (tax-inclusive)
        taxAmount= sum(line tax)            (contained within those totals)
        subtotal = total - taxAmount        (the pre-tax figure)

    `total` is NOT subtotal + tax. The tax is already inside the line totals,
    so adding it again would overstate the document by exactly the tax.
    """
    total = round(sum(_line_total(i) for i in items), 2)
    tax = round(sum(_line_tax(i) for i in items), 2)
    return {
        "subtotal": round(total - tax, 2),
        "taxAmount": tax,
        "total": total,
    }


# ============================================================================
# MCP Server
# ============================================================================

mcp = FastMCP(
    "Umbra ERP",
    instructions=(
        "Manage ERP + CRM + finance data (customers, invoices, quotes, products, payments, "
        "receipts, recurring invoice/quote schedules, bills and supplier payments, journal "
        "entries, aged receivables, customer statements, employees, leave requests, webhooks, "
        "and CRM contacts, leads, activities) via the Umbra ERP public API. Used by Mufasa for "
        "business operations. "
        "MONEY UNITS: every amount in and out of these tools is DOLLARS (150.00), never cents, "
        "including bill payments. Fields whose name ends in `Cents` are the only exception and "
        "are integer cents; prefer those for arithmetic and the dollar fields for display. "
        "IDS: every id you pass in a tool argument is a public UUID. A few NESTED response "
        "fields are numeric row ids instead (recurring*.customerId, bill.vendorId, "
        "statement.payments[].invoiceId, journal sourceId and lines[].accountId); never feed "
        "those back into a tool. "
        "Multi-workspace: every tool takes an optional `workspace` argument (default 'primary') "
        "to target a specific business; extra workspaces are configured via UMBRA_API_KEY_<NAME> "
        "env vars, with an optional UMBRA_API_URL_<NAME> when that workspace lives on another "
        "host. Use list_workspaces or check_status to see which workspaces are configured."
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

    MONEY SAFETY. Line totals are TAX-INCLUSIVE: a line's `total` already
    contains its tax, so the document total is the sum of the line totals and
    `subtotal` is that sum minus the tax, never plus it.

    Send `items` alone and the server restates `subtotal` / `taxAmount` /
    `total` from the lines for you. State any of them explicitly and yours
    wins, for a negotiated round number or a discount the lines do not carry.
    This tool does not second-guess either path: it never injects header
    figures you did not send.

    What it does add is per-line `taxAmount`. The server stores a line's tax
    but never computes one from `taxRate`, so a line carrying only a rate
    would leave the header tax reading zero. When a line has `taxRate` and no
    `taxAmount`, the tax is backed out of the inclusive total (a 100.00 line
    at 15% carries 13.04 of tax over an 86.96 net) and sent with the line.

    After any items write the quote is re-read and the tool returns the
    SERVER's state under `quote`, with `itemsSent` / `itemsStored` /
    `itemsRoundTripped` and a reconciliation of the stored header against the
    lines, so a dropped array or a stale total is visible instead of reading
    as a bare 200.

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
        # Backfill only the per-line tax the server cannot work out for itself.
        # Header figures are left to the server unless the caller stated them.
        items = [dict(i) for i in items]
        for item in items:
            if item.get("taxAmount") is None and item.get("taxRate"):
                item["taxAmount"] = _line_tax(item)
        data["items"] = items

    try:
        result = _put(f"/v1/quotes/{quote_id}", data, workspace)
        if items is None:
            return _ok(result)

        expected = _derive_document_totals(items)
        stated = {k: float(data[k]) for k in ("subtotal", "taxAmount", "total")
                  if k in data}
        expected.update(stated)

        # Re-read: the write is not done until the server says the lines moved.
        fresh = _get(f"/v1/quotes/{quote_id}", workspace=workspace).get("data") or {}
        stored = fresh.get("items") or []
        actual = {k: fresh.get(k) for k in ("subtotal", "taxAmount", "total")}

        drift = {k: {"expected": expected[k], "stored": actual[k]}
                 for k in expected
                 if actual.get(k) is not None
                 and round(float(actual[k]) - expected[k], 2) != 0}

        if drift and not stated:
            # An older server that replaces lines without restating the header
            # leaves the quote contradicting itself. One corrective write.
            log.warning("quote %s header did not restate; correcting %s", quote_id, drift)
            _put(f"/v1/quotes/{quote_id}", expected, workspace)
            fresh = _get(f"/v1/quotes/{quote_id}", workspace=workspace).get("data") or {}
            stored = fresh.get("items") or []
            actual = {k: fresh.get(k) for k in ("subtotal", "taxAmount", "total")}
            drift = {k: {"expected": expected[k], "stored": actual[k]}
                     for k in expected
                     if actual.get(k) is not None
                     and round(float(actual[k]) - expected[k], 2) != 0}

        out: dict[str, Any] = {
            "quote": fresh,
            "itemsSent": len(items),
            "itemsStored": len(stored),
            "itemsRoundTripped": len(stored) == len(items),
            "headerFromLines": expected,
            "headerStored": actual,
            "headerReconciles": not drift,
        }
        if not out["itemsRoundTripped"]:
            out["warning"] = (
                f"Sent {len(items)} line(s) but the quote now stores {len(stored)}. "
                "The line items did NOT land; do not send this quote to a customer."
            )
        elif drift:
            out["warning"] = (
                f"The stored header does not match the lines: {drift}. Do not send "
                "this quote to a customer until the figures agree."
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
    create a customer; use convert_lead_to_customer for that, or the lead is
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
    idem = _idem(idempotency_key)
    try:
        return _ok(_post(f"/v1/leads/{lead_id}/convert", data, workspace, idem))
    except Exception as e:
        return _write_err(e, idem, f"list_leads and checking whether lead {lead_id} "
                                   "now reads status 'converted'")


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


# ── RECEIPTS (customer money in) ─────────────────────────────────────────────
# Same underlying rows as list_payments/get_payment. Exposed under their own
# names because "receipt" is the document a user asks about.

@mcp.tool()
def list_receipts(limit: int = 50, skip: int = 0, workspace: str = "primary") -> str:
    """List customer receipts (money in), newest page first.

    Requires the `payments` permission. Reads the same rows as list_payments;
    both names are live and return identical shapes.

    Each receipt carries: id (public UUID), receiptNumber, receiptDate and
    paymentDate (YYYY-MM-DD), amount in DOLLARS, currency, status
    (draft|issued|paid|completed|final|voided), paymentMethod (cash,
    credit_card, ecocash, mobile_money, bank_transfer, other),
    paymentReference, customerId (public UUID or null), customerName,
    invoiceId (public UUID or null), invoiceNumber, unallocatedAmount in
    DOLLARS, onAccount, dateCreated/timeUpdated (ISO-8601 UTC with Z).

    `onAccount: true` means the receipt is not applied to any invoice;
    `unallocatedAmount` is how much of it is still unapplied.

    Args:
        limit: Max results (1-200, default 50)
        skip: Offset for pagination
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get("/v1/receipts", {"limit": limit, "skip": skip}, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_receipt(receipt_id: str, workspace: str = "primary") -> str:
    """Get one customer receipt by its public UUID.

    Requires the `payments` permission. Same shape as list_receipts, with
    `amount` and `unallocatedAmount` in DOLLARS.

    Args:
        receipt_id: The receipt's public UUID (the `id` from list_receipts)
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/receipts/{receipt_id}", workspace=workspace))
    except Exception as e:
        return _err(e)


# ── INVOICE PAY LINK ─────────────────────────────────────────────────────────

@mcp.tool()
def get_invoice_pay_link(invoice_id: str, workspace: str = "primary") -> str:
    """Get (or mint) the customer-facing card pay link for an invoice.

    Requires the `invoices` permission. Mints a token on first call, reuses a
    live one afterwards so a link already given to a customer stays valid, and
    regenerates a lapsed one. Token TTL is 30 days.

    NEVER construct a pay URL yourself; the token and its eligibility gates
    are server-owned. Read `payUrl` here or off the invoice object.

    Availability is not an error. When the rail is unavailable this returns
    HTTP 200 with `available: false`, `payUrl: null` and a `reason` you should
    report verbatim, one of:
      - "Card payments are not enabled for this business" -> the tenant is not
        opted in. The tenant CANNOT self-enable; a Lioncap admin must do it,
        because the money lands in the Lioncap merchant account.
      - "Pay links are USD only; this invoice is ZWG" -> currency gate.

    When available: {invoiceId, invoiceNumber, payUrl, available: true,
    amountDue (DOLLARS), currency, expiresAt (ISO-8601 UTC with Z)}.

    Args:
        invoice_id: The invoice's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        result = _get(f"/v1/invoices/{invoice_id}/pay-link", workspace=workspace)
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and not data.get("available"):
            return _ok({
                "available": False,
                "payUrl": None,
                "reason": data.get("reason", "Pay link unavailable"),
                "invoiceId": data.get("invoiceId"),
                "note": "Not an error. Report the reason to the user as written.",
            })
        return _ok(result)
    except Exception as e:
        return _err(e)


# ── QUOTE -> INVOICE ─────────────────────────────────────────────────────────

@mcp.tool()
def convert_quote_to_invoice(
    quote_id: str,
    idempotency_key: str | None = None,
    workspace: str = "primary",
) -> str:
    """Convert a quote into a numbered invoice.

    SIDE EFFECTS: mints an invoice from the quote's line items and marks the
    quote `accepted`. Requires the `quotes` permission.

    Safe to retry. A quote already converted returns its EXISTING invoice with
    HTTP 200 and `alreadyConverted: true` rather than erroring. It never mints
    a second invoice: that would draw another number from the gapless sequence
    and post the same sale's revenue twice.

    Returns {"data": <full invoice object>, "alreadyConverted": bool}. The
    invoice carries publicId, invoiceNumber, status, invoiceDate, dueDate,
    subtotal / taxAmount / total / amountPaid / balanceDue in DOLLARS,
    currency, and payUrl (null unless card payments are live for the tenant
    and the invoice is USD).

    409 means the quote was converted but its invoice has since been deleted.

    Args:
        quote_id: The quote's public UUID
        idempotency_key: Reuse the same key to replay a timed-out call instead
            of re-running it. Auto-generated when omitted.
        workspace: Target business workspace (default "primary")
    """
    idem = _idem(idempotency_key)
    try:
        return _ok(_post(f"/v1/quotes/{quote_id}/convert", {}, workspace, idem))
    except Exception as e:
        return _write_err(e, idem, f"get_quote on {quote_id} (a converted quote reads "
                                   "'accepted') and list_invoices for a new invoice")


# ── RECURRING INVOICES ───────────────────────────────────────────────────────

@mcp.tool()
def list_recurring_invoices(
    status: str | None = None,
    limit: int = 50,
    skip: int = 0,
    workspace: str = "primary",
) -> str:
    """List recurring-invoice templates (the schedules, not the invoices they emit).

    Requires the `invoices` permission.

    Each template: id (public UUID), customerId, title, frequency, status,
    startDate / endDate / nextInvoiceDate (YYYY-MM-DD), dayOfMonth, total in
    DOLLARS, currency, autoSend, invoicesGenerated, lastGeneratedAt (ISO-8601
    UTC with Z), paymentTermsDays, dateCreated.

    WARNING: `customerId` on this shape is a NUMERIC row id, not a public UUID.
    Do not feed it to get_customer or any path parameter.

    Args:
        status: Filter by status (active, paused, completed, cancelled)
        limit: Max results (1-200, default 50)
        skip: Offset for pagination
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if status:
        params["status"] = status
    try:
        return _ok(_get("/v1/recurring-invoices", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_recurring_invoice(
    customer_id: str,
    title: str,
    frequency: str,
    start_date: str,
    total: float,
    subtotal: float | None = None,
    tax_amount: float | None = None,
    currency: str = "USD",
    end_date: str | None = None,
    day_of_month: int | None = None,
    payment_terms_days: int = 14,
    auto_send: bool = False,
    notes: str | None = None,
    terms: str | None = None,
    reference: str | None = None,
    idempotency_key: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a recurring-invoice template that emits real invoices on a schedule.

    SIDE EFFECTS: from `start_date` onwards this bills the customer on every
    sweep without further approval, and `auto_send=True` emails each invoice.
    The FIRST run is `start_date` itself, so a template starting today fires on
    today's sweep. Requires the `invoices` permission.

    All money arguments are in DOLLARS (e.g. 500.00, never 50000).

    Args:
        customer_id: Customer public UUID
        title: Template title, e.g. "Monthly retainer"
        frequency: weekly, biweekly, monthly, quarterly, semi_annual, or annual
        start_date: First invoice date (YYYY-MM-DD). Fires on this date.
        total: Invoice total in DOLLARS
        subtotal: Subtotal in DOLLARS (defaults to `total`)
        tax_amount: Tax in DOLLARS (default 0)
        currency: Currency code (default USD)
        end_date: Stop after this date (YYYY-MM-DD); null runs indefinitely
        day_of_month: Day of month to bill on, for monthly-and-longer frequencies
        payment_terms_days: Days until each generated invoice is due (default 14)
        auto_send: Email each generated invoice to the customer automatically
        notes: Notes copied onto each generated invoice
        terms: Terms copied onto each generated invoice
        reference: Your own reference for the template
        idempotency_key: Reuse the same key to replay a timed-out call instead
            of creating a second schedule. Auto-generated when omitted.
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {
        "customerId": customer_id,
        "title": title,
        "frequency": frequency,
        "startDate": start_date,
        "total": total,
        "subtotal": total if subtotal is None else subtotal,
        "taxAmount": 0 if tax_amount is None else tax_amount,
        "currency": currency,
        "paymentTermsDays": payment_terms_days,
        "autoSend": auto_send,
    }
    if end_date:
        data["endDate"] = end_date
    if day_of_month is not None:
        data["dayOfMonth"] = day_of_month
    if notes:
        data["notes"] = notes
    if terms:
        data["terms"] = terms
    if reference:
        data["reference"] = reference
    idem = _idem(idempotency_key)
    try:
        return _ok(_post("/v1/recurring-invoices", data, workspace, idem))
    except Exception as e:
        return _write_err(e, idem, "list_recurring_invoices, matching on title")


@mcp.tool()
def update_recurring_invoice(recurring_id: str, updates: str, workspace: str = "primary") -> str:
    """Update a recurring-invoice template. Pass a JSON string of fields to change.

    Requires the `invoices` permission. Partial update: only the keys you send
    are touched.

    Accepted keys: title, reference, notes, terms, dayOfMonth, status,
    paymentTermsDays, frequency, endDate, nextInvoiceDate, autoSend, total.
    `total` is in DOLLARS. Dates are YYYY-MM-DD.

    Set `status` to "paused" to stop the schedule emitting without deleting its
    history, "active" to resume, "cancelled" to stop it for good.

    NOTE: sending an explicit null for endDate / nextInvoiceDate / frequency /
    dayOfMonth does NOT clear the stored value; the API skips null on those
    fields. There is no way to un-set them through this endpoint.

    Args:
        recurring_id: The template's public UUID
        updates: JSON string, e.g. '{"status": "paused"}' or '{"total": 750.00}'
        workspace: Target business workspace (default "primary")
    """
    try:
        data = json.loads(updates)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in updates parameter"})
    try:
        return _ok(_put(f"/v1/recurring-invoices/{recurring_id}", data, workspace))
    except Exception as e:
        return _err(e)


# ── RECURRING QUOTES ─────────────────────────────────────────────────────────

_RECURRING_QUOTE_ACTIONS = {
    "pause": "paused",
    "resume": "active",
    "cancel": "cancelled",
    "end": "ended",
}


@mcp.tool()
def list_recurring_quotes(
    status: str | None = None,
    limit: int = 50,
    skip: int = 0,
    workspace: str = "primary",
) -> str:
    """List recurring-quote templates (the schedules, not the quotes they emit).

    Requires the `quotes` permission.

    Each template: id (public UUID), customerId, title, frequency, status,
    startDate / endDate / nextQuoteDate (YYYY-MM-DD), dayOfMonth, total in
    DOLLARS, currency, autoSend, quotesGenerated, lastGeneratedAt (ISO-8601 UTC
    with Z), lastGeneratedQuoteId, validityDays, dateCreated.

    `validityDays` is canonical: how long each generated quote stays open.
    `paymentTermsDays` is accepted on write as an alias but is NEVER returned
    on a recurring quote; do not read it off this shape.

    Generating a quote books nothing. A quote is an offer, not a receivable;
    money only moves when it is converted to an invoice and paid.

    WARNING: `customerId` and `lastGeneratedQuoteId` here are NUMERIC row ids,
    not public UUIDs. Do not feed them to a path parameter.

    Args:
        status: Filter by status (active, paused, cancelled, ended)
        limit: Max results (1-200, default 50)
        skip: Offset for pagination
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if status:
        params["status"] = status
    try:
        return _ok(_get("/v1/recurring-quotes", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_recurring_quote(
    customer_id: str,
    title: str,
    frequency: str,
    start_date: str,
    total: float,
    subtotal: float | None = None,
    tax_amount: float | None = None,
    currency: str = "USD",
    end_date: str | None = None,
    day_of_month: int | None = None,
    validity_days: int = 30,
    auto_send: bool = False,
    notes: str | None = None,
    terms: str | None = None,
    reference: str | None = None,
    idempotency_key: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a recurring-quote template that emits quotes on a schedule.

    SIDE EFFECTS: emits a quote on every sweep from `start_date` onwards, and
    `auto_send=True` emails each one to the customer. The FIRST run is
    `start_date` itself. Requires the `quotes` permission.

    All money arguments are in DOLLARS. Generating a quote posts nothing to the
    ledger; a quote is an offer, not a receivable.

    Args:
        customer_id: Customer public UUID
        title: Template title, e.g. "Quarterly proposal"
        frequency: weekly, biweekly, monthly, quarterly, semi_annual, or annual
        start_date: First quote date (YYYY-MM-DD). Fires on this date.
        total: Quote total in DOLLARS
        subtotal: Subtotal in DOLLARS (defaults to `total`)
        tax_amount: Tax in DOLLARS (default 0)
        currency: Currency code (default USD)
        end_date: Stop after this date (YYYY-MM-DD); null runs indefinitely
        day_of_month: Day of month to generate on, for monthly-and-longer frequencies
        validity_days: How long each GENERATED quote stays open before expiring
            (default 30). This is the canonical field; paymentTermsDays is only
            an input alias and is never returned.
        auto_send: Email each generated quote to the customer automatically
        notes: Notes copied onto each generated quote
        terms: Terms copied onto each generated quote
        reference: Your own reference for the template
        idempotency_key: Reuse the same key to replay a timed-out call instead
            of creating a second schedule. Auto-generated when omitted.
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {
        "customerId": customer_id,
        "title": title,
        "frequency": frequency,
        "startDate": start_date,
        "total": total,
        "subtotal": total if subtotal is None else subtotal,
        "taxAmount": 0 if tax_amount is None else tax_amount,
        "currency": currency,
        "validityDays": validity_days,
        "autoSend": auto_send,
    }
    if end_date:
        data["endDate"] = end_date
    if day_of_month is not None:
        data["dayOfMonth"] = day_of_month
    if notes:
        data["notes"] = notes
    if terms:
        data["terms"] = terms
    if reference:
        data["reference"] = reference
    idem = _idem(idempotency_key)
    try:
        return _ok(_post("/v1/recurring-quotes", data, workspace, idem))
    except Exception as e:
        return _write_err(e, idem, "list_recurring_quotes, matching on title")


@mcp.tool()
def update_recurring_quote(recurring_id: str, updates: str, workspace: str = "primary") -> str:
    """Update a recurring-quote template. Pass a JSON string of fields to change.

    Requires the `quotes` permission. Partial update: only the keys you send
    are touched.

    Accepted keys: title, reference, notes, terms, dayOfMonth, status,
    frequency, endDate, nextQuoteDate, autoSend, total, and validityDays (or
    its input alias paymentTermsDays). `total` is in DOLLARS; dates are
    YYYY-MM-DD. Omitting both validity keys leaves the stored value alone
    rather than resetting it to the 30-day default.

    For pause / resume / cancel prefer set_recurring_quote_status, which
    validates the status string; this endpoint stores `status` verbatim into a
    free-text column, so a typo is accepted silently and the schedule then
    matches no filter.

    NOTE: an explicit null for endDate / nextQuoteDate / frequency /
    dayOfMonth does NOT clear the stored value; the API skips null on those.

    Args:
        recurring_id: The template's public UUID
        updates: JSON string, e.g. '{"validityDays": 45, "total": 1800.00}'
        workspace: Target business workspace (default "primary")
    """
    try:
        data = json.loads(updates)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in updates parameter"})
    try:
        return _ok(_put(f"/v1/recurring-quotes/{recurring_id}", data, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def set_recurring_quote_status(
    recurring_id: str,
    action: str,
    workspace: str = "primary",
) -> str:
    """Pause, resume, cancel or end a recurring-quote schedule.

    SIDE EFFECT: changes whether the template emits quotes on the next sweep.
    Requires the `quotes` permission.

    Actions: "pause" -> paused (stops emitting, keeps history and can be
    resumed), "resume" -> active, "cancel" -> cancelled (stopped for good),
    "end" -> ended (the schedule ran its course).

    The status column is free text server-side, so an unrecognised value would
    be stored silently and leave the schedule matching no status filter. This
    tool rejects anything outside the four actions rather than writing it.

    Recurring INVOICE templates have no equivalent action tool; use
    update_recurring_invoice with '{"status": "paused"}' (their status set is
    active, paused, completed, cancelled).

    Args:
        recurring_id: The template's public UUID
        action: One of pause, resume, cancel, end
        workspace: Target business workspace (default "primary")
    """
    key = (action or "").strip().lower()
    if key not in _RECURRING_QUOTE_ACTIONS:
        return json.dumps({
            "error": f"Unknown action '{action}'",
            "validActions": sorted(_RECURRING_QUOTE_ACTIONS),
        })
    try:
        return _ok(_put(f"/v1/recurring-quotes/{recurring_id}",
                        {"status": _RECURRING_QUOTE_ACTIONS[key]}, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def recurring_quote_history(recurring_id: str, workspace: str = "primary") -> str:
    """Generation history for one recurring-quote schedule.

    Requires the `quotes` permission.

    LIMITATION, read before relying on this: the public API exposes NO endpoint
    that lists the individual quote documents a schedule produced, and no
    get-by-id for a recurring quote either. This tool pages
    /v1/recurring-quotes to find the template and returns its generation
    counters: quotesGenerated, lastGeneratedAt (ISO-8601 UTC with Z),
    lastGeneratedQuoteId, nextQuoteDate, status, frequency, validityDays and
    total (DOLLARS).

    `lastGeneratedQuoteId` is a NUMERIC row id, NOT a public UUID; get_quote
    will not accept it. To see the actual documents, call list_quotes and match
    on customer and date.

    Args:
        recurring_id: The template's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        page, limit, scanned = 0, 200, 0
        while page < 5:
            result = _get("/v1/recurring-quotes",
                          {"limit": limit, "skip": page * limit}, workspace)
            rows = result.get("data") or []
            scanned += len(rows)
            for row in rows:
                if row.get("id") == recurring_id:
                    return _ok({
                        "id": row.get("id"),
                        "title": row.get("title"),
                        "status": row.get("status"),
                        "frequency": row.get("frequency"),
                        "quotesGenerated": row.get("quotesGenerated"),
                        "lastGeneratedAt": row.get("lastGeneratedAt"),
                        "lastGeneratedQuoteId": row.get("lastGeneratedQuoteId"),
                        "nextQuoteDate": row.get("nextQuoteDate"),
                        "validityDays": row.get("validityDays"),
                        "total": row.get("total"),
                        "currency": row.get("currency"),
                        "note": (
                            "Counters only. The API exposes no list of the quote "
                            "documents this schedule generated, and "
                            "lastGeneratedQuoteId is a numeric row id that get_quote "
                            "cannot take."
                        ),
                    })
            if len(rows) < limit:
                break
            page += 1
        return json.dumps({
            "error": "Recurring quote not found",
            "recurringId": recurring_id,
            "scanned": scanned,
        })
    except Exception as e:
        return _err(e)


# ── REPORTS ──────────────────────────────────────────────────────────────────

@mcp.tool()
def aged_receivables(
    as_of: str | None = None,
    customer_id: str | None = None,
    workspace: str = "primary",
) -> str:
    """Outstanding customer receivables bucketed by how far PAST DUE they are.

    Requires the `reports` permission (grantable only from 2026-08-29; an older
    key 403s and must be re-minted).

    Buckets are days past the DUE date, not days since issue: an invoice on
    30-day terms issued 20 days ago is `current`, not 30 days old. Boundaries:
    current = due today or later, days1To30 = 1-30 days late, then 31-60,
    61-90, days90Plus = 91+.

    Excludes draft (never issued), cancelled (reversed), paid, and bad_debt
    (already written off); none of them is money anyone expects to collect.

    Returns buckets and byCustomer rows in DOLLARS, plus `bucketsCents` and
    `totalCents` in integer CENTS. USE THE CENTS FIELDS for any comparison or
    summation and the dollar fields for display only; float sums drift.
    `byCustomer` is sorted by total descending and its `customerId` IS a public
    UUID.

    Args:
        as_of: Report date (YYYY-MM-DD, default today). Ageing is measured
            against this date, so pass a month-end to reproduce a past report.
        customer_id: Restrict to one customer's public UUID
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {}
    if as_of:
        params["asOf"] = as_of
    if customer_id:
        params["customerId"] = customer_id
    try:
        return _ok(_get("/v1/reports/aged-receivables", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def customer_statement(
    customer_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    workspace: str = "primary",
) -> str:
    """Statement of account for one customer: invoices, receipts, closing balance.

    Requires the `customers` permission.

    openingBalance + totalInvoiced - totalPaid = closingBalance always holds.
    Without `start_date` the opening balance is 0.00 and the statement covers
    all history. `closingBalance` can be NEGATIVE when the customer is in
    credit; do not clamp it to zero. Draft and cancelled invoices are excluded.

    Money fields are DOLLARS, with `closingBalanceCents` in integer CENTS for
    exact arithmetic. Dates are YYYY-MM-DD.

    WARNING: `payments[].invoiceId` is a NUMERIC row id here, not a public
    UUID. `invoices[].id` and `payments[].id` ARE public UUIDs.

    Args:
        customer_id: The customer's public UUID
        start_date: Period start (YYYY-MM-DD). Omit for all history with a
            0.00 opening balance.
        end_date: Period end (YYYY-MM-DD, default today)
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    try:
        return _ok(_get(f"/v1/customers/{customer_id}/statement", params, workspace))
    except Exception as e:
        return _err(e)


# ── BILLS (supplier money out) ───────────────────────────────────────────────

@mcp.tool()
def list_bills(
    status: str | None = None,
    limit: int = 50,
    skip: int = 0,
    workspace: str = "primary",
) -> str:
    """List supplier bills (accounts payable).

    Requires the `bills` permission (grantable only from 2026-08-29; an older
    key 403s and must be re-minted).

    Each bill: id (public UUID), billNumber, vendorBillNumber, vendorId,
    vendorName, reference, status, billDate / dueDate (YYYY-MM-DD), subtotal,
    taxAmount, total, amountPaid, balanceDue all in DOLLARS, currency,
    dateCreated (ISO-8601 UTC with Z).

    WARNING: `vendorId` in the RESPONSE is a NUMERIC row id, while create_bill
    takes a vendor PUBLIC UUID. Do not round-trip the response value.

    Args:
        status: Filter by status (draft, pending, approved, partial, paid,
            overdue, cancelled)
        limit: Max results (1-200, default 50)
        skip: Offset for pagination
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if status:
        params["status"] = status
    try:
        return _ok(_get("/v1/bills", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_bill(bill_id: str, workspace: str = "primary") -> str:
    """Get one supplier bill by its public UUID.

    Requires the `bills` permission. Money fields are DOLLARS. Check
    `balanceDue` before calling record_bill_payment: an overpayment is rejected,
    not clamped.

    Args:
        bill_id: The bill's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/bills/{bill_id}", workspace=workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_bill(
    vendor_id: str,
    total: float,
    bill_date: str | None = None,
    due_date: str | None = None,
    subtotal: float | None = None,
    tax_amount: float | None = None,
    vendor_bill_number: str | None = None,
    bill_number: str | None = None,
    reference: str | None = None,
    currency: str = "USD",
    notes: str | None = None,
    idempotency_key: str | None = None,
    workspace: str = "primary",
) -> str:
    """Create a supplier bill (a payable the business owes).

    SIDE EFFECTS: records a liability. Requires the `bills` permission.
    The bill starts `status: "pending"` with amountPaid 0 and balanceDue equal
    to `total`. Recording money against it is a separate call
    (record_bill_payment).

    All money arguments are in DOLLARS (2.33 means two dollars thirty-three).

    Args:
        vendor_id: The vendor's PUBLIC UUID (required; 400 without it). Note the
            response returns a numeric vendorId; do not feed that back here.
        total: Bill total in DOLLARS. Must be greater than zero.
        bill_date: Bill date (YYYY-MM-DD, default today)
        due_date: Payment due date (YYYY-MM-DD)
        subtotal: Subtotal in DOLLARS (defaults to `total`)
        tax_amount: Tax in DOLLARS (default 0)
        vendor_bill_number: The supplier's own invoice number, e.g. "INV-9912"
        bill_number: Your internal bill number. Auto-generated when omitted;
            let it auto-generate unless you are migrating existing records.
        reference: Your own reference
        currency: Currency code (default USD)
        notes: Free-text notes
        idempotency_key: Reuse the same key to replay a timed-out call instead
            of booking the same liability twice. Auto-generated when omitted.
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {
        "vendorId": vendor_id,
        "total": total,
        "subtotal": total if subtotal is None else subtotal,
        "taxAmount": 0 if tax_amount is None else tax_amount,
        "currency": currency,
    }
    if bill_date:
        data["billDate"] = bill_date
    if due_date:
        data["dueDate"] = due_date
    if vendor_bill_number:
        data["vendorBillNumber"] = vendor_bill_number
    if bill_number:
        data["billNumber"] = bill_number
    if reference:
        data["reference"] = reference
    if notes:
        data["notes"] = notes
    idem = _idem(idempotency_key)
    try:
        return _ok(_post("/v1/bills", data, workspace, idem))
    except Exception as e:
        return _write_err(e, idem, "list_bills, matching on vendorBillNumber or total")


@mcp.tool()
def record_bill_payment(
    bill_id: str,
    amount: float,
    payment_date: str | None = None,
    payment_method: str | None = None,
    reference: str | None = None,
    memo: str | None = None,
    idempotency_key: str | None = None,
    workspace: str = "primary",
) -> str:
    """Record a payment against a supplier bill. THIS MOVES REAL MONEY OUT.

    SIDE EFFECTS: creates the payment, updates the bill's amountPaid /
    balanceDue / status, and posts the DR Accounts Payable / CR Cash journal
    entry. Requires the `bills` permission.

    `amount` is in DOLLARS end to end; send 2.33 for two dollars thirty-three.
    Do NOT convert to cents: this is the one money column in the schema stored
    in dollars, and the API normalises it for you either way, so a
    cents-converted figure would pay the supplier 100x.

    OVERPAYMENT IS REJECTED WITH 400, not clamped:
    "Payment of 250.0 exceeds the outstanding balance of 233.0". Read
    `balanceDue` with get_bill first. (Customer receipts behave the opposite
    way; an overpayment there is recorded and flagged, because that cash has
    already arrived. Supplier money has not left yet and an excess is almost
    always a typo.)

    Returns {"data": {id, billId, amount, paymentDate, paymentMethod, bill:
    <updated bill>}}.

    Args:
        bill_id: The bill's public UUID
        amount: Payment amount in DOLLARS. Must be > 0 and <= the bill's balanceDue.
        payment_date: Payment date (YYYY-MM-DD, default today)
        payment_method: e.g. bank_transfer, cash, credit_card, ecocash, mobile_money
        reference: External reference, e.g. "MasterCard ...7305"
        memo: Free-text memo, e.g. "Meta ads Aug balance"
        idempotency_key: Reuse the same key to replay a timed-out call instead
            of paying the supplier twice. Auto-generated when omitted.
        workspace: Target business workspace (default "primary")
    """
    data: dict[str, Any] = {"amount": amount}
    if payment_date:
        data["paymentDate"] = payment_date
    if payment_method:
        data["paymentMethod"] = payment_method
    if reference:
        data["reference"] = reference
    if memo:
        data["memo"] = memo
    idem = _idem(idempotency_key)
    try:
        return _ok(_post(f"/v1/bills/{bill_id}/payments", data, workspace, idem))
    except Exception as e:
        return _write_err(e, idem, f"get_bill on {bill_id} and reading amountPaid / "
                                   "balanceDue before paying again")


# ── JOURNAL ENTRIES (read-only) ──────────────────────────────────────────────
# Deliberately no create/update/delete. Entries are posted by auto_journal from
# real events (invoice issued, invoice paid, bill paid); a hand-made entry
# would be a figure in the ledger with no document behind it.

@mcp.tool()
def list_journal_entries(
    start_date: str | None = None,
    end_date: str | None = None,
    status: str | None = None,
    limit: int = 50,
    skip: int = 0,
    workspace: str = "primary",
) -> str:
    """List general-ledger journal entries, newest first. READ ONLY.

    Requires the `journal` permission (grantable only from 2026-08-29; an older
    key 403s and must be re-minted).

    Each entry: id (public UUID), entryNumber, entryType (standard, adjusting,
    closing, reversing, opening), status, entryDate (YYYY-MM-DD), description,
    reference, sourceType, sourceId, totalDebit / totalCredit in DOLLARS,
    currency, isReversed, postedAt / dateCreated (ISO-8601 UTC with Z). Call
    get_journal_entry for the debit/credit lines.

    Only `posted` entries affect the ledger. Statuses: draft, pending,
    approved, posted, rejected, reversed.

    WARNING: `sourceId` is a NUMERIC row id of the source document, not a
    public UUID; it cannot be passed to get_invoice or get_bill.

    There is no write path by design. Do not look for one.

    Args:
        start_date: Only entries on/after this date (YYYY-MM-DD)
        end_date: Only entries on/before this date (YYYY-MM-DD)
        status: Filter by status (posted, draft, pending, approved, rejected, reversed)
        limit: Max results (1-200, default 50)
        skip: Offset for pagination
        workspace: Target business workspace (default "primary")
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if status:
        params["status"] = status
    try:
        return _ok(_get("/v1/journal-entries", params, workspace))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_journal_entry(entry_id: str, workspace: str = "primary") -> str:
    """Get one journal entry including its debit/credit lines. READ ONLY.

    Requires the `journal` permission. Same shape as list_journal_entries plus
    a `lines` array of {accountId, accountNumber, description, debit, credit}
    with debit/credit in DOLLARS.

    `lines[].accountId` is a NUMERIC chart-of-accounts row id, not a public
    UUID. In a balanced entry the line debits equal the line credits and both
    equal totalDebit / totalCredit.

    Args:
        entry_id: The journal entry's public UUID
        workspace: Target business workspace (default "primary")
    """
    try:
        return _ok(_get(f"/v1/journal-entries/{entry_id}", workspace=workspace))
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
                "receipts", "recurring_invoices", "recurring_quotes",
                "contacts", "leads", "activities",
                "employees", "leave_requests", "webhooks",
                "bills", "journal_entries", "reports",
            ],
            "permissions_note": (
                "Each resource needs its scope on the API key. bills -> `bills`, "
                "journal_entries -> `journal`, aged_receivables -> `reports`, "
                "receipts -> `payments`. Those three plus `employees` and `payroll` "
                "only became grantable on 2026-08-29, so a key minted before that "
                "date 403s on them and must be re-minted."
            ),
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
