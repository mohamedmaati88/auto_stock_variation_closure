from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    stock_auto_interim_close = fields.Boolean(
        related='company_id.stock_auto_interim_close',
        readonly=False,
    )
    stock_change_in_stock_account_id = fields.Many2one(
        related='company_id.stock_change_in_stock_account_id',
        readonly=False,
        domain="[('company_ids', 'in', [company_id])]",
    )
    stock_auto_interim_journal_id = fields.Many2one(
        related='company_id.stock_auto_interim_journal_id',
        readonly=False,
        domain="[('company_id', '=', company_id), ('type', '=', 'general')]",
    )
    stock_sales_use_expense_interim = fields.Boolean(
        related='company_id.stock_sales_use_expense_interim',
        readonly=False,
    )
