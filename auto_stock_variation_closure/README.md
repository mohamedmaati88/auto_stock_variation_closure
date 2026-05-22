# Auto Stock Variation Closure

**Odoo 19** · LGPL-3

---

## The Problem

In Odoo 19 perpetual inventory (`real_time` valuation) the accounting entry is
deferred until billing time.  Between the goods movement and the invoice a
temporary **Stock Variation balance** appears in the Inventory Valuation report,
creating a gap between the physical and the accounting stock value.

## The Solution

This module automatically posts a **provisional journal entry** at the moment a
receipt or delivery is validated, so the Inventory Valuation report always shows
zero Stock Variation.  When the corresponding bill or invoice is posted, a
**compensation entry** is created to close the provisional entry cleanly.

### Purchase flow

| Event | Dr | Cr |
|---|---|---|
| Receipt validated (provisional) | Stock Account | Stock Variation Account |
| Vendor bill posted — Odoo's entry | Stock Account | Accounts Payable |
| Vendor bill posted — compensation | Stock Variation Account | Stock Account |
| **Net result** | **Stock Variation Account** | **Accounts Payable** |

### Sale flow

| Event | Dr | Cr |
|---|---|---|
| Delivery validated (provisional) | Stock Variation Account | Stock Account |
| Invoice posted — Odoo's COGS entry | COGS | Stock Account |
| Invoice posted — compensation | Stock Account | Stock Variation Account |
| **Net result** | **COGS** | **Stock Variation Account** |

Credit notes, refunds, and return pickings are handled with reversed signs.

---

## Installation

1. Copy the module folder into your Odoo addons path.
2. Update the apps list and install **Auto Stock Variation Closure**.
3. Go to **Accounting → Settings → Auto Stock Variation Closure**:
   - Enable *Auto Close on Receipt / Delivery*
   - Select the *Stock Variation Account* (the Change-in-Stock account in your chart)
   - Optionally select a *Journal* (defaults to the company stock journal)

---

## Configuration Options

| Setting | Description | Default |
|---|---|---|
| Auto Close on Receipt / Delivery | Enable/disable the entire feature | Off |
| Stock Variation Account | Account credited on receipt, debited on delivery | — |
| Journal | Journal for all provisional/compensation entries | Company stock journal |

---

## Dependencies

- `stock_account` (Odoo built-in)
- `stock_landed_costs` (Odoo built-in)
- `mrp_subcontracting` (Odoo built-in)
- No OCA or third-party dependencies

---

## ⚠ Accounting Warnings

**Read before enabling in production.**

### 1. Provisional amount is an approximation for AVCO and FIFO

The provisional entry at receipt/delivery time is computed from
`move.price_unit × qty`.  This is **exact for Standard costing** but is an
**approximation** for AVCO and FIFO:

- **AVCO:** `price_unit` is the purchase price, not the running-average cost
  that Odoo will ultimately book at billing time.
- **FIFO:** `price_unit` is the PO price, not the FIFO layer cost.

The difference is absorbed by the compensation entry at billing time so the
**net accounting effect is always correct**, but two entries (provisional +
compensation) may carry slightly different unit amounts.

### 2. Customer invoice compensation requires COGS lines

The module compensates sales invoices only when Odoo generates
`display_type = 'cogs'` lines on the invoice (i.e. the product category has
perpetual valuation and all required stock accounts are configured).  If COGS
lines are absent the compensation is skipped silently — this is correct
behaviour, not a bug.

### 3. Multi-currency

No currency conversion is applied to the provisional entry.  For multi-currency
companies, verify entries manually before go-live.

### 4. Reconciliation

The provisional entries are **not reconciled** with the compensation entries.
They are two independent posted journal entries that algebraically cancel each
other.  This is intentional — reconciling them would require matching by amount,
which breaks for partial deliveries and price differences.

---

## Compatibility

| Odoo Version | Status |
|---|---|
| Odoo 19 (Community & Enterprise) | ✅ Supported |

---

## Running Tests

```bash
python odoo-bin -d <your_db> --test-enable --stop-after-init \
    -i auto_stock_variation_closure
```

---

## License

LGPL-3 — see [LICENSE](LICENSE).
