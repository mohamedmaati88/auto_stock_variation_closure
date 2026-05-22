from odoo import api, models
from odoo.exceptions import ValidationError


class ProductCategory(models.Model):
    _inherit = 'product.category'

    @api.constrains('property_valuation', 'property_account_expense_categ_id')
    def _check_expense_account_required(self):
        """
        When a product category uses real_time (perpetual) inventory valuation
        and Auto Stock Variation Closure is active, the expense account
        (property_account_expense_categ_id) is required because the module
        relies on it as a fallback Change-in-Stock account.
        """
        for categ in self:
            if categ.property_valuation != 'real_time':
                continue
            if not self.env.company.stock_auto_interim_close:
                continue
            expense_acc = getattr(categ, 'property_account_expense_categ_id', False)
            if not expense_acc:
                raise ValidationError(
                    'Product Category "%s": the Expense Account '
                    '(property_account_expense_categ_id) is required when '
                    'perpetual inventory valuation is enabled and '
                    'Auto Stock Variation Closure is active.' % categ.name
                )
