{
    'name': 'Stock Variation Manager',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Accounting',
    'summary': 'Instantly zero stock variation on receipt/delivery; reconciles automatically at billing time',
    'description': """
Auto Stock Variation Closure
=============================

Odoo 19 – Perpetual Inventory (real_time)
------------------------------------------

**The problem**

In Odoo 19 perpetual-inventory mode the accounting entry for a purchase receipt
is deferred until the supplier bill is posted.  Between the goods movement and the
bill there is a temporary Stock Variation balance in the Inventory Valuation report.
The same gap occurs on the sales side between a delivery and the customer invoice.

**What this module adds**

On purchase receipt validation a provisional entry is automatically posted:
    Dr. Stock Account  /  Cr. Stock Variation Account

This zeroes the pending balance in the report immediately.

When the supplier bill is posted a compensation entry is created:
    Dr. Stock Variation Account  /  Cr. Stock Account

Combined with Odoo's own bill entry the net result is:
    Dr. Stock Variation Account  /  Cr. Accounts Payable

The same logic applies in reverse for sales deliveries and customer invoices.

**Handled scenarios**

* Purchase receipts and sale deliveries
* Vendor bills, customer invoices, credit notes, refunds
* Partial deliveries / receipts and backorder confirmation
* Return pickings (automatically produce the correct reversed entry)
* Reset to draft: compensation entry is cancelled and re-generated on re-post
* Multi-company: settings and entries are isolated per company

**No third-party dependencies** – requires only standard Odoo modules
(``stock_account``, ``stock_landed_costs``, ``mrp_subcontracting``).

**Known limitation – multi-currency**

Provisional and compensation entries are posted in the company currency only.
Pickings linked to purchase or sale orders in a foreign currency are
automatically skipped; a warning is logged and the entry must be posted
manually if needed.

Configuration
-------------
Accounting > Settings > Auto Stock Variation Closure
  * Enable the feature
  * Choose the Stock Variation account
  * Choose the journal (defaults to the company stock journal)
    """,
    'author': 'VRS',
    'license': 'LGPL-3',
    'depends': ['stock_account', 'stock_landed_costs', 'mrp_subcontracting'],
    'data': [
        'views/res_config_settings_views.xml',
        'views/product_views.xml',
        'views/stock_picking_views.xml',
        'views/stock_landed_cost_views.xml',
        'views/account_move_views.xml',
        'views/stock_valuation_layer_views.xml',
    ],
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
