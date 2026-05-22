from odoo import models, _
from odoo.exceptions import UserError


def _check_vendor_return_source(picking):
    """Return list of product names if picking is a vendor return, else []."""
    return [
        m.product_id.display_name
        for m in picking.move_ids
        if m.state != 'cancel'
        and m.location_id.usage == 'internal'
        and m.location_dest_id.usage == 'supplier'
    ]


class StockReturnPicking(models.TransientModel):
    _inherit = 'stock.return.picking'

    def default_get(self, fields_list):
        """Block the wizard from opening when source is a vendor return."""
        res = super().default_get(fields_list)
        picking_id = res.get('picking_id') or self.env.context.get('active_id')
        if picking_id:
            picking = self.env['stock.picking'].browse(picking_id)
            products = _check_vendor_return_source(picking)
            if products:
                raise UserError(_(
                    "Cannot return a vendor return.\n\n"
                    "Affected products:\n• %(products)s\n\n"
                    "Solution: create a new purchase order instead.",
                    products='\n• '.join(products),
                ))
        return res

    def create_returns(self):
        """Secondary block in case default_get was bypassed."""
        products = _check_vendor_return_source(self.picking_id)
        if products:
            raise UserError(_(
                "Cannot return a vendor return.\n\n"
                "Affected products:\n• %(products)s\n\n"
                "Solution: create a new purchase order instead.",
                products='\n• '.join(products),
            ))
        return super().create_returns()


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def action_return_picking(self):
        """Block at button level for older Odoo versions."""
        for picking in self:
            products = _check_vendor_return_source(picking)
            if products:
                raise UserError(_(
                    "Cannot return a vendor return.\n\n"
                    "Affected products:\n• %(products)s\n\n"
                    "Solution: create a new purchase order instead.",
                    products='\n• '.join(products),
                ))
        return super().action_return_picking()

    def button_validate(self):
        """Last-resort block at validation time."""
        for picking in self:
            is_rereceive = any(
                m.location_id.usage == 'supplier'
                and m.location_dest_id.usage == 'internal'
                and m.origin_returned_move_id
                and m.origin_returned_move_id.location_id.usage == 'internal'
                and m.origin_returned_move_id.location_dest_id.usage == 'supplier'
                for m in picking.move_ids
                if m.state != 'cancel'
            )
            if is_rereceive:
                products = [
                    m.product_id.display_name
                    for m in picking.move_ids
                    if m.state != 'cancel'
                ]
                raise UserError(_(
                    "Cannot validate a return of a vendor return.\n\n"
                    "Affected products:\n• %(products)s\n\n"
                    "Solution: create a new purchase order instead.",
                    products='\n• '.join(products),
                ))
        return super().button_validate()
