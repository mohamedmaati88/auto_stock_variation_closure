from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    stock_auto_interim_close = fields.Boolean(
        string='Auto Close Stock Interim',
        default=False,
    )
    stock_change_in_stock_account_id = fields.Many2one(
        'account.account',
        string='Change in Stock Account',
        domain="[('company_ids', 'in', [id])]",
        help="Account used as the counterpart in the automatic provisional "
             "entries created on receipt/delivery.",
    )
    stock_auto_interim_journal_id = fields.Many2one(
        'account.journal',
        string='Stock Interim Journal',
        domain="[('company_id', '=', id), ('type', '=', 'general')]",
        help="Journal for the automatic provisional/compensation entries. "
             "Defaults to the company stock journal when left empty.",
    )
    stock_sales_use_expense_interim = fields.Boolean(
        string='Sales Interim Expense Account',
        default=False,
        help="Post delivery cost entries to a designated expense account instead "
             "of the standard interim account. Priority: product expense account → "
             "product category expense account → general settings expense account.",
    )

    # ------------------------------------------------------------------
    # Shared helpers (used by stock_picking, account_move,
    #                  stock_landed_cost, mrp_production)
    # ------------------------------------------------------------------

    def get_stock_interim_journal(self):
        """
        Return the journal to use for all auto interim/compensation entries.
        Priority:
          1. Explicitly configured stock_auto_interim_journal_id
          2. Company stock journal (account_stock_journal_id)
          3. First general journal of the company
        """
        return (
            self.stock_auto_interim_journal_id
            or getattr(self, 'account_stock_journal_id', False)
            or self.env['account.journal'].search(
                [('type', '=', 'general'), ('company_id', '=', self.id)],
                limit=1,
            )
        )

    def get_default_stock_val_account(self):
        """
        Return the company-level default stock valuation account.
        Source: General Settings → Inventory Accounting → Valuation Account
        (ir.property default for product.category.property_stock_valuation_account_id).
        """
        try:
            return self.env['ir.property'].sudo()._get(
                'property_stock_valuation_account_id', 'product.category'
            )
        except Exception:
            return False
