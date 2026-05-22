import logging

from odoo import models, fields

_logger = logging.getLogger(__name__)


# ===========================================================================
# Suppress Odoo 19 inline LC accounting
# ===========================================================================
# In Odoo 19 stock.landed.cost.button_validate creates the Dr.Stock/Cr.LC
# account.move INLINE — there is no separate _create_account_move helper.
# The entry lines are produced by StockValuationAdjustmentLines.
# _create_accounting_entries().  Returning [] from that method causes
# button_validate to skip account.move.create() entirely.
# ===========================================================================

class StockValuationAdjustmentLines(models.Model):
    _inherit = 'stock.valuation.adjustment.lines'

    def _create_accounting_entries(self, remaining_qty):
        """
        When Auto Stock Variation Closure is active, return an empty list so
        that button_validate builds no journal lines and creates no
        account.move.  The module creates its own on-hand-based entry via
        StockLandedCost._create_lc_interim_entry().
        """
        cost = self.cost_id
        if cost and cost.company_id.stock_auto_interim_close:
            return []
        return super()._create_accounting_entries(remaining_qty)


# ===========================================================================
# Landed cost — module-owned entry creation
# ===========================================================================

class StockLandedCost(models.Model):
    _inherit = 'stock.landed.cost'

    auto_interim_move_id = fields.Many2one(
        'account.move',
        string='Auto Interim Entry',
        readonly=True,
        copy=False,
    )
    auto_cogs_move_id = fields.Many2one(
        'account.move',
        string='LC COGS Entry',
        readonly=True,
        copy=False,
    )
# ------------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------------

    def button_validate(self):
        """
        Snapshot AVCO standard prices BEFORE super() potentially modifies
        them via SVL updates so that our on-hand recalculation uses the
        original pre-LC cost.
        """
        # ── Snapshot: standard_price for every AVCO product in LC pickings ──
        avco_snapshots = {}      # {product.id: standard_price_before_lc}
        for lc in self:
            if not lc.company_id.stock_auto_interim_close:
                continue
            for picking in lc.picking_ids:
                for move in picking.move_ids.filtered(lambda m: m.state == 'done'):
                    p = move.product_id
                    if (p.categ_id.property_cost_method == 'average'
                            and p.id not in avco_snapshots):
                        avco_snapshots[p.id] = p.standard_price

        result = super().button_validate()

        for lc in self.filtered(
            lambda l: l.state == 'done'
            and l.company_id.stock_auto_interim_close
            and not l.auto_interim_move_id
        ):
            try:
                lc._create_lc_interim_entry(avco_snapshots=avco_snapshots)
            except Exception:
                _logger.exception(
                    'Auto Stock Variation Closure: failed to create LC '
                    'interim entry for landed cost %s', lc.name,
                )

        # ── COGS reclassification for sold / delivered portion ──────────
        for lc in self.filtered(
            lambda l: l.state == 'done'
            and l.company_id.stock_auto_interim_close
            and not l.auto_cogs_move_id
        ):
            try:
                lc._create_lc_cogs_entry()
            except Exception:
                _logger.exception(
                    'Auto Stock Variation Closure: failed to create LC COGS '
                    'entry for landed cost %s', lc.name,
                )

        return result

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def button_cancel(self):
        for lc in self:
            for entry in (lc.auto_interim_move_id, lc.auto_cogs_move_id):
                if entry and entry.state == 'posted':
                    try:
                        entry.with_context(skip_stock_interim_comp=True).button_draft()
                        entry.with_context(skip_stock_interim_comp=True).button_cancel()
                    except Exception:
                        _logger.exception(
                            'Auto Stock Variation Closure: failed to cancel '
                            'entry %s when cancelling landed cost %s',
                            entry.name, lc.name,
                        )
        return super().button_cancel()

    # ------------------------------------------------------------------
    # Smart buttons
    # ------------------------------------------------------------------

    def action_view_lc_interim_entry(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'LC Allocation Entry',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.auto_interim_move_id.id,
            'target': 'current',
        }

    def action_view_lc_cogs_entry(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'LC COGS Entry',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.auto_cogs_move_id.id,
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Entry creation  (on-hand based)
    # ------------------------------------------------------------------

    def _create_lc_interim_entry(self, avco_snapshots=None):
        """
        After landed cost validation create the allocation entry:

            Dr. Stock Valuation Account(s)   ← proportional to relevant value
            Cr. Landed Cost Account(s)        ← closes the LC account

        Receipt LC  — distribute proportionally to ON-HAND quantities
                      (pre-LC snapshot price).  AVCO standard_price is
                      recalculated using only the on-hand fraction of the LC.

        Delivery LC — distribute proportionally to the DELIVERY ORDER
                      quantities/values only (the units that were actually
                      shipped).  No AVCO update (goods are no longer on hand).

        :param avco_snapshots: dict {product.id: standard_price_before_lc}
                               captured before super().button_validate() so
                               the recalculation is not distorted by any
                               AVCO update Odoo performed internally via SVL.
        """
        if avco_snapshots is None:
            avco_snapshots = {}

        journal = self.company_id.get_stock_interim_journal()
        if not journal:
            _logger.warning(
                'Auto Stock Variation Closure: no journal found for LC '
                'interim entry on %s', self.name,
            )
            return

        # ── Cr side: cost lines (LC expense / holding accounts) ──────
        cr_by_account = {}
        for cl in self.cost_lines:
            amount = cl.price_unit or 0.0
            if amount < 0.01 or not cl.account_id:
                continue
            cr_by_account[cl.account_id] = (
                cr_by_account.get(cl.account_id, 0.0) + amount
            )

        if not cr_by_account:
            _logger.warning(
                'Auto Stock Variation Closure: no cost lines with account '
                'on LC %s — entry skipped.', self.name,
            )
            return

        total_lc_amount = sum(cr_by_account.values())

        # ── Detect LC type ────────────────────────────────────────────
        is_delivery_lc = any(
            p.picking_type_id.code == 'outgoing'
            for p in self.picking_ids
        )

        # ── Collect real_time products from LC pickings ───────────────
        lc_products = {}         # {product: categ}
        receipt_qty_per_product = {}  # {product: total receipt qty for AVCO}
        for picking in self.picking_ids:
            for move in picking.move_ids.filtered(lambda m: m.state == 'done'):
                product = move.product_id
                categ = product.categ_id
                if categ.property_valuation != 'real_time':
                    continue
                if not categ.property_stock_valuation_account_id:
                    continue
                if product not in lc_products:
                    lc_products[product] = categ
                if picking.picking_type_id.code == 'incoming':
                    qty = move.product_uom_qty or 0.0
                    receipt_qty_per_product[product] = (
                        receipt_qty_per_product.get(product, 0.0) + qty
                    )

        if not lc_products:
            _logger.warning(
                'Auto Stock Variation Closure: no real-time valued products '
                'found in LC %s pickings — entry skipped.', self.name,
            )
            return

        # ── Build product_data depending on LC type ───────────────────
        product_data = {}  # {product: {'qty': float, 'value': float, ...}}

        if is_delivery_lc:
            # ── Delivery LC: use the delivery order quantities/values ──
            # Only the units that physically left the warehouse in these
            # specific pickings are considered — on-hand is irrelevant.
            for picking in self.picking_ids:
                if picking.picking_type_id.code != 'outgoing':
                    continue
                for move in picking.move_ids.filtered(
                    lambda m: m.state == 'done'
                ):
                    product = move.product_id
                    categ = product.categ_id
                    if categ.property_valuation != 'real_time':
                        continue
                    if not categ.property_stock_valuation_account_id:
                        continue
                    qty = move.product_uom_qty or 0.0
                    value = abs(move.value or 0.0) or (
                        qty * product.standard_price
                    )
                    if product not in product_data:
                        product_data[product] = {
                            'qty': 0.0,
                            'value': 0.0,
                            'categ': categ,
                            'pre_lc_price': 0.0,
                        }
                    product_data[product]['qty'] += qty
                    product_data[product]['value'] += value

        else:
            # ── Receipt LC: use on-hand quantities (pre-LC snapshot) ──
            for product, categ in lc_products.items():
                quants = self.env['stock.quant'].sudo().search([
                    ('product_id', '=', product.id),
                    ('location_id.usage', '=', 'internal'),
                    ('company_id', '=', self.company_id.id),
                ])
                qty_onhand = sum(q.quantity for q in quants)
                if qty_onhand < 0.001:
                    continue
                pre_lc_price = avco_snapshots.get(
                    product.id, product.standard_price
                )
                product_data[product] = {
                    'qty': qty_onhand,
                    'value': qty_onhand * pre_lc_price,
                    'categ': categ,
                    'pre_lc_price': pre_lc_price,
                }

        total_basis_value = sum(d['value'] for d in product_data.values())

        # ── Dr side: distribute LC proportionally ────────────────────
        dr_by_account = {}
        avco_allocation = {}  # {product: lc_amount} — receipt LC only

        if total_basis_value > 0.01:
            for product, data in product_data.items():
                stock_acc = data['categ'].property_stock_valuation_account_id
                ratio = data['value'] / total_basis_value
                allocated = total_lc_amount * ratio
                dr_by_account[stock_acc] = (
                    dr_by_account.get(stock_acc, 0.0) + allocated
                )
                # AVCO recalculation only applies to receipt LC
                if (not is_delivery_lc
                        and data['categ'].property_cost_method == 'average'):
                    # Use only the on-hand fraction of the LC so that
                    # standard_price reflects the actual in-stock cost:
                    #   receipt=3, on_hand=2, LC=30
                    #   avco_net = 30 × (2/3) = 20 → new_avco=(200+20)/2=110
                    on_hand_qty = data['qty']
                    receipt_qty = receipt_qty_per_product.get(product, 0.0)
                    if receipt_qty > on_hand_qty + 0.001:
                        avco_net = allocated * (on_hand_qty / receipt_qty)
                    else:
                        avco_net = allocated
                    avco_allocation[product] = (
                        avco_allocation.get(product, 0.0) + avco_net
                    )
        else:
            # No basis value found — close LC against first stock account.
            _logger.info(
                'Auto Stock Variation Closure: LC %s — no basis value; '
                'LC amount booked against first available stock account.',
                self.name,
            )
            for product, categ in lc_products.items():
                stock_acc = categ.property_stock_valuation_account_id
                dr_by_account[stock_acc] = total_lc_amount
                break

        if not dr_by_account:
            _logger.warning(
                'Auto Stock Variation Closure: could not determine stock '
                'valuation accounts for LC %s — entry skipped.', self.name,
            )
            return

        # ── Build journal lines ───────────────────────────────────────
        lines_vals = []
        for stock_acc, amount in dr_by_account.items():
            lines_vals.append({
                'account_id': stock_acc.id,
                'debit': amount,
                'credit': 0.0,
                'name': self.name,
            })
        for lc_acc, amount in cr_by_account.items():
            lines_vals.append({
                'account_id': lc_acc.id,
                'debit': 0.0,
                'credit': amount,
                'name': self.name,
            })

        # ── Post entry ────────────────────────────────────────────────
        entry = (
            self.env['account.move']
            .with_context(skip_stock_interim_comp=True)
            .with_company(self.company_id)
            .create({
                'company_id': self.company_id.id,
                'journal_id': journal.id,
                'date': self.date or fields.Date.context_today(self),
                'ref': 'LC Interim: %s' % self.name,
                'move_type': 'entry',
                'line_ids': [(0, 0, v) for v in lines_vals],
            })
        )
        entry.with_context(skip_stock_interim_comp=True).action_post()
        self.auto_interim_move_id = entry.id

        _logger.info(
            'Auto Stock Variation Closure: LC interim entry %s created '
            'for %s (total_lc=%.4f, on_hand_value=%.4f)',
            entry.name, self.name, total_lc_amount, total_onhand_value,
        )

        # ── AVCO cost price recalculation (on-hand based) ─────────────
        for product, lc_allocated in avco_allocation.items():
            pre_lc_price = product_data[product]['pre_lc_price']
            self._recalculate_avco_after_lc(
                product,
                lc_allocated,
                pre_lc_price=pre_lc_price,
            )

    # ------------------------------------------------------------------
    # COGS reclassification entry (sold / delivered portion)
    # ------------------------------------------------------------------

    def _create_lc_cogs_entry(self):
        """
        Create a reclassification entry for the LC portion attributable to
        goods that have already LEFT the warehouse (sold or delivered).

        The Dr account depends on whether the delivery has been invoiced:

          Invoiced delivery   → Dr. COGS Account        / Cr. Stock Val
          Uninvoiced delivery → Dr. Stock Output/Interim / Cr. Stock Val

        When the invoice is eventually posted for an uninvoiced delivery,
        Odoo's standard COGS entry (Dr. COGS / Cr. Stock Output) will
        naturally include the LC amount — no extra entry needed.

        Receipt LC  — only the sold fraction is reclassified.
                      sold_qty = max(0, receipt_qty − on_hand_qty)
        Delivery LC — full LC amount is reclassified (all goods are sold).
        """
        journal = self.company_id.get_stock_interim_journal()
        if not journal:
            _logger.warning(
                'Auto Stock Variation Closure: no journal for LC COGS '
                'entry on %s', self.name,
            )
            return

        total_lc_amount = sum(
            cl.price_unit or 0.0
            for cl in self.cost_lines
            if (cl.price_unit or 0.0) > 0.01 and cl.account_id
        )
        if total_lc_amount < 0.01:
            return

        # ── Is this LC on deliveries or receipts? ────────────────────
        is_delivery_lc = any(
            p.picking_type_id.code == 'outgoing'
            for p in self.picking_ids
        )

        # ── Per-product split: {product: {'invoiced': x, 'not_invoiced': y}}
        if is_delivery_lc:
            split_by_product = self._lc_cogs_amounts_delivery(total_lc_amount)
        else:
            split_by_product = self._lc_cogs_amounts_receipt(total_lc_amount)

        if not split_by_product:
            _logger.info(
                'Auto Stock Variation Closure: LC COGS entry skipped for '
                '%s — no sold/delivered portion found.', self.name,
            )
            return

        # ── Build journal lines ───────────────────────────────────────
        lines_vals = []
        for product, split in split_by_product.items():
            invoiced_amt = split.get('invoiced', 0.0)
            not_inv_amt = split.get('not_invoiced', 0.0)

            stock_val_acc = product.categ_id.property_stock_valuation_account_id
            if not stock_val_acc:
                _logger.warning(
                    'Auto Stock Variation Closure: no stock val account for '
                    '%s in LC %s — skipping product.',
                    product.display_name, self.name,
                )
                continue

            # ── Invoiced portion → Dr. COGS ──────────────────────────
            if invoiced_amt > 0.01:
                cogs_acc = self._lc_expense_account(product)
                if not cogs_acc:
                    _logger.warning(
                        'Auto Stock Variation Closure: no COGS account for '
                        '%s in LC %s — skipping invoiced portion.',
                        product.display_name, self.name,
                    )
                else:
                    lines_vals += [
                        {
                            'account_id': cogs_acc.id,
                            'debit': invoiced_amt,
                            'credit': 0.0,
                            'name': '%s (invoiced)' % self.name,
                        },
                        {
                            'account_id': stock_val_acc.id,
                            'debit': 0.0,
                            'credit': invoiced_amt,
                            'name': '%s (invoiced)' % self.name,
                        },
                    ]

            # ── Uninvoiced portion → Dr. Stock Output/Interim ────────
            if not_inv_amt > 0.01:
                output_acc = self._lc_interim_account(product)
                if not output_acc:
                    _logger.warning(
                        'Auto Stock Variation Closure: no stock output account '
                        'for %s in LC %s — skipping uninvoiced portion.',
                        product.display_name, self.name,
                    )
                else:
                    lines_vals += [
                        {
                            'account_id': output_acc.id,
                            'debit': not_inv_amt,
                            'credit': 0.0,
                            'name': '%s (pending invoice)' % self.name,
                        },
                        {
                            'account_id': stock_val_acc.id,
                            'debit': 0.0,
                            'credit': not_inv_amt,
                            'name': '%s (pending invoice)' % self.name,
                        },
                    ]

        if not lines_vals:
            return

        entry = (
            self.env['account.move']
            .with_context(skip_stock_interim_comp=True)
            .with_company(self.company_id)
            .create({
                'company_id': self.company_id.id,
                'journal_id': journal.id,
                'date': self.date or fields.Date.context_today(self),
                'ref': 'LC COGS: %s' % self.name,
                'move_type': 'entry',
                'line_ids': [(0, 0, v) for v in lines_vals],
            })
        )
        entry.with_context(skip_stock_interim_comp=True).action_post()
        self.auto_cogs_move_id = entry.id

        _logger.info(
            'Auto Stock Variation Closure: LC COGS entry %s created '
            'for %s (is_delivery=%s, uninvoiced_products=%d)',
            entry.name, self.name, is_delivery_lc, len(uninvoiced_data),
        )

    def _lc_picking_is_invoiced(self, picking):
        """
        Return True if the given outgoing picking has at least one
        posted customer invoice (via its sale order).
        """
        sale = getattr(picking, 'sale_id', False)
        if not sale:
            return False
        return bool(
            sale.invoice_ids.filtered(
                lambda m: m.state == 'posted'
                and m.move_type in ('out_invoice', 'out_refund')
            )
        )

    def _lc_sold_split_by_invoice(self, product, sold_lc):
        """
        Split sold_lc into (invoiced_amount, not_invoiced_amount) by looking
        at ALL outgoing done moves for the product and computing the fraction
        whose sale order has a posted customer invoice.

        Used for receipt-type LC where the sold units came from various
        deliveries that cannot be directly traced to this specific receipt.
        """
        outgoing_moves = self.env['stock.move'].sudo().search([
            ('product_id', '=', product.id),
            ('state', '=', 'done'),
            ('location_id.usage', '=', 'internal'),
            ('location_dest_id.usage', '=', 'customer'),
            ('company_id', '=', self.company_id.id),
        ])

        invoiced_qty = 0.0
        total_qty = 0.0
        for move in outgoing_moves:
            qty = move.product_uom_qty or 0.0
            total_qty += qty
            if move.picking_id and self._lc_picking_is_invoiced(move.picking_id):
                invoiced_qty += qty

        if total_qty < 0.001:
            # No outgoing history — assume invoiced
            return sold_lc, 0.0

        inv_frac = min(invoiced_qty / total_qty, 1.0)
        return sold_lc * inv_frac, sold_lc * (1.0 - inv_frac)

    def _lc_cogs_amounts_receipt(self, total_lc_amount):
        """
        For receipt-type LC: compute the LC amount to reclassify for the
        sold fraction of each product, split by invoice status.

        sold_qty = max(0, receipt_qty − on_hand_qty)
        sold_lc  = lc_for_product × (sold_qty / receipt_qty)

        Each product's sold_lc is then split into invoiced / not-invoiced
        portions based on the historical outgoing delivery invoice ratio.

        Returns {product: {'invoiced': amount, 'not_invoiced': amount}}
        """
        product_receipt = {}   # {product: {'qty': float, 'value': float}}
        for picking in self.picking_ids:
            if picking.picking_type_id.code != 'incoming':
                continue
            for move in picking.move_ids.filtered(lambda m: m.state == 'done'):
                product = move.product_id
                categ = product.categ_id
                if categ.property_valuation != 'real_time':
                    continue
                if not categ.property_stock_valuation_account_id:
                    continue
                qty = move.product_uom_qty or 0.0
                value = abs(move.value or 0.0) or (qty * product.standard_price)
                if product not in product_receipt:
                    product_receipt[product] = {'qty': 0.0, 'value': 0.0}
                product_receipt[product]['qty'] += qty
                product_receipt[product]['value'] += value

        if not product_receipt:
            return {}

        total_receipt_value = sum(
            d['value'] for d in product_receipt.values()
        )
        if total_receipt_value < 0.01:
            return {}

        result = {}
        for product, data in product_receipt.items():
            receipt_qty = data['qty']
            if receipt_qty < 0.001:
                continue

            quants = self.env['stock.quant'].sudo().search([
                ('product_id', '=', product.id),
                ('location_id.usage', '=', 'internal'),
                ('company_id', '=', self.company_id.id),
            ])
            on_hand_qty = sum(q.quantity for q in quants)

            sold_qty = max(0.0, receipt_qty - on_hand_qty)
            if sold_qty < 0.001:
                continue

            lc_for_product = total_lc_amount * (
                data['value'] / total_receipt_value
            )
            sold_lc = lc_for_product * (sold_qty / receipt_qty)
            if sold_lc < 0.01:
                continue

            inv_amt, not_inv_amt = self._lc_sold_split_by_invoice(
                product, sold_lc
            )
            result[product] = {
                'invoiced': inv_amt,
                'not_invoiced': not_inv_amt,
            }

        return result

    def _lc_cogs_amounts_delivery(self, total_lc_amount):
        """
        For delivery-type LC: full LC distributed across products by their
        delivery value, then split per product into invoiced / not-invoiced
        portions based on each picking's invoice status.

        Returns {product: {'invoiced': amount, 'not_invoiced': amount}}
        """
        # Accumulate delivery value per product per invoice status
        prod_inv = {}      # {product: value with posted invoice}
        prod_not_inv = {}  # {product: value without posted invoice}

        for picking in self.picking_ids:
            if picking.picking_type_id.code != 'outgoing':
                continue
            is_invoiced = self._lc_picking_is_invoiced(picking)
            for move in picking.move_ids.filtered(lambda m: m.state == 'done'):
                product = move.product_id
                categ = product.categ_id
                if categ.property_valuation != 'real_time':
                    continue
                if not categ.property_stock_valuation_account_id:
                    continue
                value = abs(move.value or 0.0) or (
                    move.product_uom_qty * product.standard_price
                )
                if is_invoiced:
                    prod_inv[product] = prod_inv.get(product, 0.0) + value
                else:
                    prod_not_inv[product] = (
                        prod_not_inv.get(product, 0.0) + value
                    )

        all_products = set(list(prod_inv.keys()) + list(prod_not_inv.keys()))
        total_value = sum(
            prod_inv.get(p, 0.0) + prod_not_inv.get(p, 0.0)
            for p in all_products
        )
        if total_value < 0.01:
            return {}

        result = {}
        for product in all_products:
            inv_val = prod_inv.get(product, 0.0)
            not_inv_val = prod_not_inv.get(product, 0.0)
            total_prod_val = inv_val + not_inv_val
            lc_for_product = total_lc_amount * (total_prod_val / total_value)
            result[product] = {
                'invoiced': lc_for_product * (inv_val / total_prod_val),
                'not_invoiced': lc_for_product * (not_inv_val / total_prod_val),
            }

        return result

    def _lc_expense_account(self, product):
        """
        COGS / Expense account priority:
          1. Product template  property_account_expense_id / account_expense_id
          2. Product category  property_account_expense_categ_id
          3. General Settings  ir.property expense account
        """
        tmpl = product.product_tmpl_id
        for fname in ('property_account_expense_id', 'account_expense_id'):
            acc = getattr(tmpl, fname, False)
            if acc:
                return acc
        categ = product.categ_id
        for fname in (
            'property_account_expense_categ_id',
            'account_expense_categ_id',
        ):
            acc = getattr(categ, fname, False)
            if acc:
                return acc
        try:
            return self.env['ir.property'].sudo()._get(
                'property_account_expense_id', 'product.template'
            )
        except Exception:
            return False

    def _lc_interim_account(self, product):
        """
        Interim / Change-in-Stock account for uninvoiced deliveries.
        Follows the same priority as stock_picking._get_change_account():
          1. Product template  property_account_expense_id / account_expense_id
          2. Product category  account_stock_variation_id
          3. Product category  property_account_expense_id / categ expense
          4. Company           stock_change_in_stock_account_id
          5. General Settings  ir.property expense account
        """
        tmpl = product.product_tmpl_id
        for fname in ('property_account_expense_id', 'account_expense_id'):
            acc = getattr(tmpl, fname, False)
            if acc:
                return acc
        categ = product.categ_id
        acc = (
            getattr(categ, 'account_stock_variation_id', False)
            or getattr(categ, 'property_account_expense_id', False)
            or getattr(categ, 'property_account_expense_categ_id', False)
        )
        if acc:
            return acc
        acc = self.company_id.stock_change_in_stock_account_id
        if acc:
            return acc
        try:
            return self.env['ir.property'].sudo()._get(
                'property_account_expense_id', 'product.template'
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # AVCO recalculation  (on-hand based)
    # ------------------------------------------------------------------

    def _recalculate_avco_after_lc(self, product, lc_amount, pre_lc_price=None):
        """
        Recalculate the weighted average cost after a landed cost is applied,
        using ONLY the on-hand quantity:

            new_total_value = (qty_on_hand × old_avco) + lc_amount
            new_avco        = new_total_value / qty_on_hand

        :param pre_lc_price: standard_price captured BEFORE super() ran.
                             When provided this prevents double-counting if
                             Odoo's SVL logic already updated standard_price.
        """
        quants = self.env['stock.quant'].sudo().search([
            ('product_id', '=', product.id),
            ('location_id.usage', '=', 'internal'),
            ('company_id', '=', self.company_id.id),
        ])
        qty_on_hand = sum(q.quantity for q in quants)

        if qty_on_hand < 0.001:
            return

        old_avco = pre_lc_price if pre_lc_price is not None else product.standard_price
        new_total_value = (qty_on_hand * old_avco) + lc_amount
        new_avco = new_total_value / qty_on_hand

        if abs(new_avco - product.standard_price) < 0.0001:
            return

        _logger.info(
            'Auto Stock Variation Closure: AVCO updated after LC for %s: '
            '%.4f → %.4f  (qty_on_hand=%.4f, lc_allocated=%.4f)',
            product.display_name, product.standard_price, new_avco,
            qty_on_hand, lc_amount,
        )

        product.with_context(disable_auto_svl=True).sudo().write(
            {'standard_price': new_avco}
        )
