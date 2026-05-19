import logging

from odoo import models

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _get_accounting_data_for_valuation(self):
        """
        For immediate POS deliveries of Kit (phantom-BoM) component moves,
        replace the debit (expense/COGS) account with the Kit product's
        expense account instead of the component's own expense account.

            Dr. Kit expense account   (kit template → kit category → company)
            Cr. Component valuation   (unchanged — standard Odoo behaviour)
        """
        result = super()._get_accounting_data_for_valuation()

        if not self.company_id.stock_auto_interim_close:
            return result

        picking = self.picking_id
        if not picking:
            return result

        if not picking._is_pos_delivery() or picking._is_pos_ship_later():
            return result

        Picking = self.env['stock.picking']
        kit = Picking._get_kit_product_for_component(self)
        if not kit:
            return result

        kit_cogs_acc = Picking._get_cogs_account_for_product(kit, self.company_id)
        if not kit_cogs_acc:
            return result

        # result is (journal_id, acc_src, acc_dest, acc_valuation)
        # acc_dest is the expense/COGS account (debit side for outgoing moves)
        journal_id, acc_src, acc_dest, acc_valuation = result
        return journal_id, acc_src, kit_cogs_acc, acc_valuation

    def _get_valued_qty(self, lot=None):
        """
        For FIFO: subtract qty already returned to vendor so that the specific
        returned receipt layer is skipped by _run_fifo_get_stack(), leaving the
        correct (non-returned) receipt as the remaining inventory.
        """
        result = super()._get_valued_qty(lot=lot)

        if (
            self.company_id.stock_auto_interim_close
            and self.product_id.categ_id.property_cost_method == 'fifo'
        ):
            returned_qty = 0.0
            for rm in self.returned_move_ids:
                if (
                    rm.state == 'done'
                    and rm.location_id.usage == 'internal'
                    and rm.location_dest_id.usage == 'supplier'
                    and rm.origin_returned_move_id == self
                ):
                    returned_qty += sum(
                        ml.product_uom_id._compute_quantity(
                            ml.qty_done, self.product_id.uom_id
                        )
                        for ml in rm.move_line_ids
                    )

            if returned_qty > 0:
                _logger.debug(
                    'Auto Stock Variation Closure: move %s (picking %s) '
                    'reducing valued qty by %.4f returned to vendor '
                    '(%.4f → %.4f)',
                    self.id,
                    self.picking_id.name,
                    returned_qty,
                    result,
                    max(0.0, result - returned_qty),
                )
                result = max(0.0, result - returned_qty)

        return result
