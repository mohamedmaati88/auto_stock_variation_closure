import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    auto_subcontracting_entry_id = fields.Many2one(
        'account.move',
        string='Auto Subcontracting Entry',
        readonly=True,
        copy=False,
    )

    def action_cancel(self):
        for production in self:
            entry = production.auto_subcontracting_entry_id
            if entry and entry.state == 'posted':
                try:
                    entry.with_context(skip_stock_interim_comp=True).button_draft()
                    entry.with_context(skip_stock_interim_comp=True).button_cancel()
                except Exception:
                    _logger.exception(
                        'Auto Stock Variation Closure: failed to cancel '
                        'subcontracting entry %s when cancelling MO %s',
                        entry.name, production.name,
                    )
            # Also cancel the service-fee interim on the receipt picking.
            # In Odoo 19, move_finished_ids[0].picking_id is always empty for
            # subcontracting, so fall back to looking up the picking via origin.
            receipt_picking = production._get_subcontracting_receipt_picking()
            if receipt_picking:
                interim = receipt_picking.auto_interim_move_id
                if interim and interim.state == 'posted':
                    try:
                        interim.with_context(skip_stock_interim_comp=True).button_draft()
                        interim.with_context(skip_stock_interim_comp=True).button_cancel()
                    except Exception:
                        _logger.exception(
                            'Auto Stock Variation Closure: failed to cancel '
                            'receipt interim entry %s when cancelling MO %s',
                            interim.name, production.name,
                        )
        return super().action_cancel()

    def _post_inventory(self, cancel_backorder=False):
        result = super()._post_inventory(cancel_backorder=cancel_backorder)
        for production in self.filtered(
            lambda p: p.state == 'done'
            and p.company_id.stock_auto_interim_close
            and p.bom_id
            and getattr(p.bom_id, 'type', '') == 'subcontract'
        ):
            production._create_subcontracting_raw_to_fg_entry()
            production._create_subcontracting_receipt_interim_entry()
        return result

    def _create_subcontracting_raw_to_fg_entry(self):
        """
        Dr. Finished Goods Stock Account  (total raw material cost)
        Cr. Raw Materials Stock Account(s) (per component)
        """
        # Idempotency: skip if a valid entry was already created.
        existing = self.auto_subcontracting_entry_id
        if existing and existing.state != 'cancel':
            return

        finished_categ = self.product_id.categ_id
        if finished_categ.property_valuation != 'real_time':
            return
        finished_acc = finished_categ.property_stock_valuation_account_id
        if not finished_acc:
            return

        raw_amounts = {}
        for move in self.move_raw_ids.filtered(lambda m: m.state == 'done'):
            categ = move.product_id.categ_id
            if categ.property_valuation != 'real_time':
                continue
            raw_acc = categ.property_stock_valuation_account_id
            if not raw_acc or raw_acc == finished_acc:
                continue
            amount = abs(move.value or 0.0)
            if amount < 0.01:
                layers = getattr(move, 'stock_valuation_layer_ids', None)
                if layers:
                    amount = abs(sum(layers.mapped('value')))
            if amount > 0.01:
                raw_amounts[raw_acc] = raw_amounts.get(raw_acc, 0.0) + amount

        if not raw_amounts:
            return

        journal = self.company_id.get_stock_interim_journal()
        if not journal:
            return

        total = sum(raw_amounts.values())
        lines_vals = [
            {'account_id': finished_acc.id, 'debit': total, 'credit': 0.0, 'name': self.name},
        ]
        for raw_acc, amount in raw_amounts.items():
            lines_vals.append({
                'account_id': raw_acc.id, 'debit': 0.0, 'credit': amount, 'name': self.name,
            })

        entry = (
            self.env['account.move']
            .with_context(skip_stock_interim_comp=True)
            .with_company(self.company_id)
            .create({
                'company_id': self.company_id.id,
                'journal_id': journal.id,
                'date': fields.Date.context_today(self),
                'ref': 'Subcontracting: %s' % self.name,
                'move_type': 'entry',
                'line_ids': [(0, 0, v) for v in lines_vals],
            })
        )
        entry.with_context(skip_stock_interim_comp=True).action_post()
        self.auto_subcontracting_entry_id = entry.id
        _logger.info(
            'Auto Stock Variation Closure: subcontracting entry %s for MO %s (%.4f)',
            entry.name, self.name, total,
        )

    def _get_subcontracting_receipt_picking(self):
        """Return the receipt (incoming) picking for this subcontracting MO.

        In Odoo 19, move_finished_ids[0].picking_id is always empty because the
        finished move targets a virtual subcontracting location. The receipt
        picking name is stored in mo.origin instead.
        """
        # Odoo 16/17: finished move has a real picking_id
        done_fin_moves = self.move_finished_ids.filtered(
            lambda m: m.state == 'done' and m.picking_id
        )
        if done_fin_moves:
            return done_fin_moves[0].picking_id

        # Odoo 19: look up via origin
        if self.origin:
            return self.env['stock.picking'].search([
                ('name', '=', self.origin),
                ('picking_type_id.code', '=', 'incoming'),
                ('state', '=', 'done'),
                ('company_id', '=', self.company_id.id),
            ], limit=1)

        return self.env['stock.picking'].browse()

    def _create_subcontracting_receipt_interim_entry(self):
        """
        Provisional entry for the subcontracting service fee at receipt time.

            Dr. Finished Goods Stock Valuation Account  (service-fee amount)
            Cr. Change in Stock Account

        Amount = PO price_unit × qty (service fee only, not raw material cost).
        Falls back to fg_total − raw_total when no purchase line is available.
        Reversed by _create_interim_compensation() when the vendor bill is posted.
        """
        finished_categ = self.product_id.categ_id
        if finished_categ.property_valuation != 'real_time':
            return

        receipt_picking = self._get_subcontracting_receipt_picking()
        if not receipt_picking:
            return

        if receipt_picking.auto_interim_move_id:
            return

        stock_val_acc = finished_categ.property_stock_valuation_account_id
        if not stock_val_acc:
            return

        journal = self.company_id.get_stock_interim_journal()
        if not journal:
            return

        change_acc = receipt_picking._get_change_account()
        if not change_acc or change_acc == stock_val_acc:
            return

        # Service fee = PO price_unit × qty (most accurate)
        amount = 0.0
        for move in self.move_finished_ids.filtered(lambda m: m.state == 'done'):
            pl = getattr(move, 'purchase_line_id', None)
            if pl:
                qty = move.product_uom_qty or pl.product_qty or 0.0
                amount += pl.price_unit * qty

        # Fallback: fg_total − raw_total
        if amount < 0.01:
            fg_total = sum(
                abs(m.value or 0.0)
                for m in self.move_finished_ids.filtered(lambda m: m.state == 'done')
            )
            raw_total = sum(
                abs(m.value or 0.0)
                for m in self.move_raw_ids.filtered(lambda m: m.state == 'done')
            )
            amount = fg_total - raw_total

        if amount < 0.01:
            _logger.info(
                'Auto Stock Variation Closure: subcontracting receipt interim '
                'skipped for picking %s (MO %s) — zero service fee',
                receipt_picking.name, self.name,
            )
            return

        entry = (
            self.env['account.move']
            .with_context(skip_stock_interim_comp=True)
            .with_company(self.company_id)
            .create({
                'company_id': self.company_id.id,
                'journal_id': journal.id,
                'date': fields.Date.context_today(self),
                'ref': 'Subcontracting Interim: %s' % receipt_picking.name,
                'move_type': 'entry',
                'line_ids': [
                    (0, 0, {
                        'account_id': stock_val_acc.id,
                        'debit': amount,
                        'credit': 0.0,
                        'name': receipt_picking.name,
                    }),
                    (0, 0, {
                        'account_id': change_acc.id,
                        'debit': 0.0,
                        'credit': amount,
                        'name': receipt_picking.name,
                    }),
                ],
            })
        )
        entry.with_context(skip_stock_interim_comp=True).action_post()
        receipt_picking.write({'auto_interim_move_id': entry.id})
        _logger.info(
            'Auto Stock Variation Closure: subcontracting receipt interim '
            'entry %s for picking %s (MO %s, amount %.4f)',
            entry.name, receipt_picking.name, self.name, amount,
        )
