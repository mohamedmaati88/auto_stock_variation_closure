import logging
from datetime import timedelta

from odoo import fields, models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    auto_interim_move_id = fields.Many2one(
        'account.move',
        string='Auto Interim Entry',
        readonly=True,
        copy=False,
    )

    # ------------------------------------------------------------------
    # Override
    # ------------------------------------------------------------------

    def _action_done(self):
        """
        Override _action_done to:
        1. After super(), correct stock.move.value for vendor returns and
           reconcile FIFO remaining_qty by full recomputation (not by trying
           to undo what Odoo's vacuum did).
        2. Create the provisional interim entry (or a COGS entry for POS ship-later).
        3. Create a stock-valuation-diff entry when a receipt covers negative
           AVCO stock (sold before received — price difference).
        """
        neg_stock_snapshot = self._snap_negative_avco_stock()

        # Collect (product_id, company_id) pairs for FIFO vendor returns
        # BEFORE super() so we know which products to reconcile afterward.
        fifo_return_keys = self._collect_fifo_return_keys()

        snap_ts = fields.Datetime.now() - timedelta(seconds=1)

        result = super()._action_done()

        # ── FIFO full reconciliation ──────────────────────────────────────
        # Rather than trying to detect and undo what Odoo's FIFO vacuum did
        # (which varies by Odoo version and is triggered lazily), we recompute
        # remaining_qty for every SVL of each affected product from scratch.
        # This is idempotent and version-agnostic.
        for product_id, company_id in fifo_return_keys:
            try:
                self._reconcile_fifo_layers(product_id, company_id)
            except Exception:
                _logger.exception(
                    'Auto Stock Variation Closure: FIFO reconciliation failed '
                    'for product_id=%s company_id=%s — skipped.',
                    product_id, company_id,
                )

        # Fix FIFO standard_price for every done picking — NOT gated by
        # stock_auto_interim_close because the price correction is independent
        # of the interim-close journal entry feature.
        for picking in self.filtered(lambda p: p.state == 'done'):
            picking._fix_fifo_standard_price()

        for picking in self.filtered(
            lambda p: p.state == 'done' and p.company_id.stock_auto_interim_close
        ):
            picking._fix_vendor_return_move_value()

        # _post_inventory() (called inside super()._action_done()) may have set
        # auto_interim_move_id on subcontracting receipt pickings.  Force a DB
        # read so the cache reflects those writes before we filter below.
        self.invalidate_recordset(['auto_interim_move_id'])

        for picking in self.filtered(
            lambda p: p.state == 'done'
            and p.company_id.stock_auto_interim_close
            and not p.auto_interim_move_id
        ):
            if picking._is_pos_ship_later():
                picking._create_pos_ship_later_cogs_entry()
            else:
                picking._create_auto_interim_entry(snap_ts=snap_ts)
                picking._create_neg_stock_price_diff_entry(
                    neg_stock_snapshot=neg_stock_snapshot,
                )

        return result

    def _snap_negative_avco_stock(self):
        """
        Capture, for every AVCO real_time product in incoming pickings of
        this recordset, the negative-stock position BEFORE the receipt is
        processed.

        Detection strategy
        ------------------
        SUM of stock.valuation.layer.quantity for the product/company equals
        the net stock position (receipts positive, deliveries negative).
        This is the only reliable indicator in Odoo 19 AVCO because:

          • stock.quant.quantity is clamped to 0 when negative stock is not
            explicitly enabled at the warehouse/category level — even if stock
            is actually negative.
          • SVL.remaining_qty < 0 is a FIFO concept; for AVCO, outgoing layers
            always have remaining_qty = 0 regardless of the stock position.
          • SVL.quantity is always written for every movement; summing it gives
            the correct net position whether or not Odoo allows negative quants.

        Returns a dict keyed by product.id:
            {product_id: {'neg_qty': float,   # absolute negative position
                          'old_avco': float}}  # standard_price at snapshot time
        """
        SVL = self.env.get('stock.valuation.layer')
        snapshot = {}

        for picking in self:
            if picking.picking_type_id.code != 'incoming':
                continue
            if not picking.company_id.stock_auto_interim_close:
                continue
            for move in picking.move_ids:
                product = move.product_id
                categ = product.categ_id
                if categ.property_cost_method not in ('average', 'fifo'):
                    continue
                if categ.property_valuation != 'real_time':
                    continue
                if product.id in snapshot:
                    continue

                # Net stock position = sum of all SVL quantities (positive for
                # receipts, negative for deliveries). Reliable for both AVCO
                # and FIFO; unlike stock.quant.quantity which Odoo 19 clamps to 0.
                if SVL is not None:
                    all_layers = SVL.sudo().search([
                        ('product_id', '=', product.id),
                        ('company_id', '=', picking.company_id.id),
                    ])
                    net_qty = sum(all_layers.mapped('quantity'))
                else:
                    quants = self.env['stock.quant'].sudo().search([
                        ('product_id', '=', product.id),
                        ('location_id.usage', '=', 'internal'),
                        ('company_id', '=', picking.company_id.id),
                    ])
                    net_qty = sum(q.quantity for q in quants)

                if net_qty >= -0.001:
                    continue

                snapshot[product.id] = {
                    'neg_qty': abs(net_qty),
                    'old_avco': product.standard_price,
                }
                _logger.info(
                    'Auto Stock Variation Closure: negative-stock snapshot — '
                    'product=%s  net_qty=%.4f  old_cost=%.4f',
                    product.display_name, net_qty, product.standard_price,
                )
        return snapshot

    # ------------------------------------------------------------------
    # Smart button
    # ------------------------------------------------------------------

    def action_view_auto_interim_entry(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Auto Interim Entry',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.auto_interim_move_id.id,
            'target': 'current',
        }

    def _collect_fifo_return_keys(self):
        """
        Collect (product_id, company_id) pairs for FIFO vendor return moves
        in this picking set.  Called BEFORE super()._action_done().

        Returns a set of (product_id, company_id) tuples.
        """
        keys = set()
        for picking in self:
            for move in picking.move_ids:
                if not (
                    move.origin_returned_move_id
                    and move.location_id.usage == 'internal'
                    and move.location_dest_id.usage == 'supplier'
                    and move.product_id.categ_id.property_cost_method == 'fifo'
                ):
                    continue
                keys.add((move.product_id.id, picking.company_id.id))
        return keys

    def _reconcile_fifo_layers(self, product_id, company_id):
        """
        Full FIFO remaining_qty reconciliation for one product/company.

        Why full recomputation?
        -----------------------
        Odoo's _run_fifo_vacuum is triggered lazily (not always synchronously
        during _action_done).  Trying to detect and undo what vacuum did (or
        didn't do) is fragile and version-dependent.  Instead we recompute the
        correct remaining_qty for every positive SVL from first principles,
        using a single SQL read and then targeted SQL writes.

        Odoo 19+ compatibility
        ----------------------
        In Odoo 19, stock.valuation.layer was removed entirely.  remaining_qty
        is now a computed (non-stored) field on stock.move, derived from
        qty_available via product._run_fifo_get_stack().  No manual
        reconciliation is needed or possible — return immediately.

        Algorithm (Odoo ≤ 18 only)
        ---------
        1. Read ALL SVLs for the product ordered by (create_date, id).
        2. For each negative SVL (outgoing move) in chronological order:
           a. If it is a vendor return (stock_move.origin_returned_move_id
              points to a specific receipt): consume from THAT receipt's SVL.
           b. Otherwise (regular sale/delivery): consume from the oldest
              positive SVL with remaining_qty > 0 (standard FIFO).
           c. If the vendor return SVL cannot be fully satisfied from the
              intended layer (partial remaining), consume the remainder from
              the next oldest positive SVL.
        3. Write the recomputed remaining_qty to every positive SVL via
           direct SQL (bypasses Odoo's write() override which re-triggers
           vacuum and causes the "stuck until second return" symptom).
        4. Zero remaining_qty on all negative SVLs so Odoo's vacuum does
           not reprocess them and undo our reconciliation.
        """
        # Odoo 19+ removed stock.valuation.layer; remaining_qty is now a
        # computed field on stock.move (derived from qty_available).
        # Querying the non-existent table would abort the DB transaction and
        # cascade-fail all subsequent SQL in the same request — so we bail out
        # immediately.
        if self.env.get('stock.valuation.layer') is None:
            return

        cr = self.env.cr

        # ── Step 1: read all SVLs in FIFO order ──────────────────────────
        cr.execute(
            """
            SELECT
                svl.id,
                svl.quantity,
                svl.unit_cost,
                sm.origin_returned_move_id   AS origin_move_id,
                orig.id                      AS intended_pos_svl_id
            FROM  stock_valuation_layer svl
            LEFT  JOIN stock_move sm
                   ON  sm.id = svl.stock_move_id
            LEFT  JOIN stock_valuation_layer orig
                   ON  orig.stock_move_id = sm.origin_returned_move_id
                  AND  orig.quantity > 0
            WHERE svl.product_id  = %s
              AND svl.company_id  = %s
            ORDER BY svl.create_date, svl.id
            """,
            (product_id, company_id),
        )
        rows = cr.fetchall()
        # rows: (id, quantity, unit_cost, origin_move_id, intended_pos_svl_id)

        # Build structures
        pos_svls   = []   # [(id, original_qty, unit_cost), ...]  for qty > 0
        neg_svls   = []   # [(id, abs_qty, intended_pos_svl_id), ...]  for qty < 0
        pos_avail  = {}   # {svl_id: available_qty}
        pos_uc     = {}   # {svl_id: unit_cost}

        for svl_id, qty, uc, _orig_move, intended_id in rows:
            if qty > 0:
                pos_svls.append((svl_id, qty, uc))
                pos_avail[svl_id] = qty
                pos_uc[svl_id]    = uc
            elif qty < 0:
                neg_svls.append((svl_id, abs(qty), intended_id))

        pos_order = [svl_id for svl_id, _, _ in pos_svls]   # FIFO order

        # ── Step 2: simulate FIFO consumption ────────────────────────────
        for neg_id, to_consume, intended_id in neg_svls:
            remaining = to_consume

            # 2a. Vendor return → consume from the INTENDED SVL first
            if intended_id and intended_id in pos_avail:
                take = min(pos_avail[intended_id], remaining)
                pos_avail[intended_id] -= take
                remaining -= take
                if pos_avail[intended_id] < 0.001:
                    pos_avail[intended_id] = 0.0

            # 2b. Remaining (or full amount for regular deliveries) → FIFO
            if remaining > 0.001:
                for pos_id in pos_order:
                    if pos_id == intended_id:
                        continue           # already handled above
                    if pos_avail[pos_id] < 0.001:
                        continue
                    take = min(pos_avail[pos_id], remaining)
                    pos_avail[pos_id] -= take
                    remaining -= take
                    if pos_avail[pos_id] < 0.001:
                        pos_avail[pos_id] = 0.0
                    if remaining < 0.001:
                        break

        # ── Step 3: write corrected remaining_qty to positive SVLs ───────
        SVL = self.env.get('stock.valuation.layer')
        updated = 0
        for pos_id in pos_order:
            correct = max(0.0, pos_avail[pos_id])
            correct_value = correct * pos_uc[pos_id]
            cr.execute(
                """
                UPDATE stock_valuation_layer
                   SET remaining_qty   = %s,
                       remaining_value = %s
                 WHERE id = %s
                """,
                (correct, correct_value, pos_id),
            )
            updated += 1

        # ── Step 4: zero remaining_qty on negative SVLs ──────────────────
        # Prevents Odoo's lazy vacuum from reprocessing these SVLs and
        # overwriting the correct values we just wrote.
        if neg_svls:
            neg_ids = [svl_id for svl_id, _, _ in neg_svls]
            cr.execute(
                """
                UPDATE stock_valuation_layer
                   SET remaining_qty   = 0.0,
                       remaining_value = 0.0
                 WHERE id = ANY(%s)
                   AND remaining_qty != 0
                """,
                (neg_ids,),
            )

        if SVL is not None:
            SVL.invalidate_model(['remaining_qty', 'remaining_value'])

        _logger.info(
            'Auto Stock Variation Closure: FIFO reconciliation — '
            'product_id=%s  positive SVLs updated=%d  '
            'negative SVLs zeroed=%d',
            product_id, updated, len(neg_svls),
        )

    # ------------------------------------------------------------------
    # Vendor return move value fix (AVCO + move.value correction only)
    # FIFO remaining_qty is now handled by _reconcile_fifo_layers above.
    # ------------------------------------------------------------------

    def _fix_vendor_return_move_value(self):
        """
        For FIFO and AVCO vendor returns: correct stock.move.value and the
        related SVL value/unit_cost to the original receipt cost.

        FIFO remaining_qty reconciliation is done separately via
        _reconcile_fifo_layers (full recomputation, called from _action_done).

        AVCO: after correcting the move value, recalculate standard_price.
        """
        _logger.info(
            'Auto Stock Variation Closure: _fix_vendor_return_move_value '
            'called for picking %s (move count: %d)',
            self.name, len(self.move_ids),
        )
        for move in self.move_ids.filtered(
            lambda m: m.state == 'done'
            and m.origin_returned_move_id
            and m.location_id.usage == 'internal'
            and m.location_dest_id.usage == 'supplier'
            and m.product_id.categ_id.property_cost_method in ('fifo', 'average')
        ):
            _logger.info(
                'Auto Stock Variation Closure: processing vendor return move %s '
                '(product: %s, method: %s)',
                move.id, move.product_id.display_name,
                move.product_id.categ_id.property_cost_method,
            )
            original = move.origin_returned_move_id

            # Resolve original unit cost
            original_unit_cost = 0.0
            orig_layers = getattr(original, 'stock_valuation_layer_ids', None)
            if orig_layers:
                svl_uc = orig_layers[0].unit_cost
                if svl_uc and svl_uc > 0.001:
                    original_unit_cost = svl_uc

            if not original_unit_cost:
                orig_done_qty = sum(
                    ml.product_uom_id._compute_quantity(
                        ml.qty_done, original.product_id.uom_id
                    )
                    for ml in original.move_line_ids
                ) or original.product_uom_qty
                orig_value = abs(original.value or 0.0)
                if orig_done_qty and orig_value:
                    original_unit_cost = orig_value / orig_done_qty
                else:
                    # The original move has value=0 (e.g. it is itself a
                    # return-of-vendor-return in a multi-cycle chain).
                    # Trace back through origin_returned_move_id until we
                    # reach the original receipt which has a non-zero value.
                    original_unit_cost = self._resolve_fifo_move_unit_cost(original)
                    if not original_unit_cost:
                        _logger.warning(
                            'Auto Stock Variation Closure: cannot resolve original '
                            'unit cost for return move %s — skipping.', move.id,
                        )
                        continue

            qty_returned = sum(
                ml.product_uom_id._compute_quantity(ml.qty_done, move.product_id.uom_id)
                for ml in move.move_line_ids
            )
            if not qty_returned:
                continue

            correct_value = qty_returned * original_unit_cost

            _logger.info(
                'Auto Stock Variation Closure: vendor return %s move %s — '
                'current_value=%.4f  correct_value=%.4f  unit_cost=%.4f',
                move.picking_id.name, move.id,
                move.value, correct_value, original_unit_cost,
            )

            if abs(abs(move.value) - correct_value) >= 0.01:
                move.sudo().write({
                    'value': correct_value,
                    'price_unit': original_unit_cost,
                })

            # Correct SVL value/unit_cost (remaining_qty handled by reconciliation)
            _svl_model = self.env.get('stock.valuation.layer')
            layers = (
                _svl_model.sudo().search([('stock_move_id', '=', move.id)])
                if _svl_model is not None else None
            )
            if not layers:
                layers = getattr(move, 'stock_valuation_layer_ids', None)

            if layers:
                layer_ids = layers.ids
                self.env.cr.execute(
                    """
                    UPDATE stock_valuation_layer
                       SET value     = %s,
                           unit_cost = %s
                     WHERE id = ANY(%s)
                    """,
                    (correct_value, original_unit_cost, layer_ids),
                )
                if _svl_model is not None:
                    _svl_model.invalidate_model(['value', 'unit_cost'])
                _logger.info(
                    'Auto Stock Variation Closure: SVL value/unit_cost fixed '
                    '(SQL) — value=%.4f  unit_cost=%.4f  (ids: %s)',
                    correct_value, original_unit_cost, layer_ids,
                )
            else:
                # In Odoo 19, stock.valuation.layer does not exist — no SVL
                # is expected and no warning is needed.
                if self.env.get('stock.valuation.layer') is not None:
                    _logger.warning(
                        'Auto Stock Variation Closure: no SVL found for '
                        'return move %s.', move.id,
                    )

            if move.product_id.categ_id.property_cost_method == 'average':
                self._recalculate_avco_after_return(
                    move, original_unit_cost, qty_returned
                )

    def _recalculate_avco_after_return(self, move, original_unit_cost, qty_returned):
        """
        After a vendor return, recompute the product's weighted average cost
        (standard_price) and update remaining SVL values accordingly.

        In Odoo 19 AVCO, vendor returns do NOT create a new negative SVL.
        Instead Odoo decrements remaining_qty/remaining_value on the existing
        incoming SVLs using the current AVCO.  We must therefore calculate the
        correct new AVCO mathematically rather than summing SVL values.

        Formula
        -------
        Incoming SVLs: receipts at 120, 80, 100  →  sum = 300, qty = 3
        Return 1 unit at original cost 120:
            correct_total_value = 300 − (1 × 120) = 180
            correct_total_qty   = 3   − 1          = 2
            new_avco            = 180 / 2           = 90

        After calculation:
          • remaining_value on open incoming SVLs updated to qty × new_avco
          • product.standard_price updated with disable_auto_svl=True
            (stock-only revaluation, no accounting entries created)
        """
        _logger.info(
            'Auto Stock Variation Closure: _recalculate_avco_after_return '
            'ENTRY move=%s original_unit_cost=%.4f qty_returned=%.4f',
            move.id, original_unit_cost, qty_returned,
        )
        try:
            self._recalculate_avco_after_return_impl(
                move, original_unit_cost, qty_returned
            )
        except Exception:
            _logger.exception(
                'Auto Stock Variation Closure: AVCO recalculation failed '
                'for move %s — skipped.', move.id,
            )

    def _recalculate_avco_after_return_impl(self, move, original_unit_cost, qty_returned):
        """
        Recalculate AVCO after a vendor return using stock.quant (no SVL needed).

        When Odoo processes a vendor return at current AVCO, the standard_price
        stays unchanged (removing stock at avg cost doesn't alter the average).

        Formula:
          old_total_value = (qty_on_hand + qty_returned) × old_avco
          new_total_value = old_total_value − qty_returned × original_unit_cost
          new_avco        = new_total_value / qty_on_hand

        Example (receipts at 120, 80, 100 → AVCO=100, return unit at 120):
          old_total = (2+1) × 100 = 300
          new_total = 300 − 1×120 = 180
          new_avco  = 180 / 2     = 90  ✓
        """
        product = move.product_id

        # Current qty on hand (after Odoo already processed the return)
        quants = self.env['stock.quant'].sudo().search([
            ('product_id', '=', product.id),
            ('location_id.usage', '=', 'internal'),
            ('company_id', '=', move.company_id.id),
        ])
        qty_on_hand = sum(q.quantity for q in quants)

        if qty_on_hand < 0.001:
            _logger.info(
                'Auto Stock Variation Closure: AVCO recalc skipped for %s — '
                'no remaining stock (qty_on_hand=%.4f)',
                product.display_name, qty_on_hand,
            )
            return

        old_avco = product.standard_price
        old_total_value = (qty_on_hand + qty_returned) * old_avco
        new_total_value = old_total_value - (qty_returned * original_unit_cost)
        new_avco = new_total_value / qty_on_hand

        _logger.info(
            'Auto Stock Variation Closure: AVCO recalculated for %s: '
            '%.4f → %.4f  '
            '(qty_on_hand=%.4f, old_total=%.4f, new_total=%.4f)',
            product.display_name, old_avco, new_avco,
            qty_on_hand, old_total_value, new_total_value,
        )

        if abs(new_avco - old_avco) < 0.0001:
            _logger.info(
                'Auto Stock Variation Closure: AVCO already correct for %s',
                product.display_name,
            )
            return

        # Update standard_price without triggering accounting entries.
        product.with_context(disable_auto_svl=True).sudo().write(
            {'standard_price': new_avco}
        )

        # Best-effort: update SVL remaining_values if the model exists.
        svl_model = self.env.get('stock.valuation.layer')
        if svl_model is not None:
            try:
                open_layers = svl_model.sudo().search([
                    ('product_id', '=', product.id),
                    ('company_id', '=', move.company_id.id),
                    ('remaining_qty', '>', 0.001),
                ])
                for layer in open_layers:
                    layer.sudo().write(
                        {'remaining_value': layer.remaining_qty * new_avco}
                    )
            except Exception:
                _logger.warning(
                    'Auto Stock Variation Closure: could not update SVL '
                    'remaining_values for %s', product.display_name,
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_default_stock_val_account(self):
        return self.company_id.get_default_stock_val_account()

    def _get_price_diff_account_for_product(self, product):
        """
        Return the price-difference account for negative-stock AVCO adjustment.
        Priority:
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
        for fname in ('property_account_expense_categ_id', 'account_expense_categ_id'):
            acc = getattr(categ, fname, False)
            if acc:
                return acc
        try:
            return self.env['ir.property'].sudo()._get(
                'property_account_expense_id', 'product.template'
            )
        except Exception:
            return False

    def _create_neg_stock_price_diff_entry(self, neg_stock_snapshot=None):
        """
        Stock valuation difference when a purchase receipt covers previously
        negative AVCO stock (sold before received).

        Entry:
            Cost went UP   (sold 100, received 120, diff=20):
                Dr. Price Diff Account (expense)  20
                    Cr. Stock Valuation Account   20

            Cost went DOWN (sold 120, received 100, diff=20):
                Dr. Stock Valuation Account       20
                    Cr. Price Diff Account        20
        """
        if not neg_stock_snapshot or self.picking_type_id.code != 'incoming':
            return

        journal = self._get_interim_journal()
        if not journal:
            return

        lines_vals = []

        for move in self.move_ids.filtered(lambda m: m.state == 'done'):
            product = move.product_id
            snap = neg_stock_snapshot.get(product.id)
            if not snap:
                continue

            categ = product.categ_id
            if categ.property_cost_method not in ('average', 'fifo'):
                continue
            if categ.property_valuation != 'real_time':
                continue

            stock_val_acc = (
                categ.property_stock_valuation_account_id
                or self._get_default_stock_val_account()
            )
            if not stock_val_acc:
                continue

            # receipt unit cost: prefer SVL unit_cost, fall back to move.price_unit
            SVL = self.env.get('stock.valuation.layer')
            receipt_cost = move.price_unit
            if SVL is not None:
                svl = SVL.sudo().search(
                    [('stock_move_id', '=', move.id)], limit=1
                )
                if svl and svl.unit_cost:
                    receipt_cost = svl.unit_cost

            old_avco = snap['old_avco']
            price_diff = receipt_cost - old_avco
            if abs(price_diff) < 0.0001:
                continue

            covered_qty = min(move.product_uom_qty, abs(snap['neg_qty']))
            if covered_qty < 0.001:
                continue

            diff_amount = abs(price_diff * covered_qty)
            if diff_amount < 0.01:
                continue

            price_diff_acc = self._get_price_diff_account_for_product(product)
            if not price_diff_acc:
                _logger.warning(
                    'Auto Stock Variation Closure: no expense account for '
                    '%s — price-diff entry skipped.', product.display_name,
                )
                continue

            if price_diff > 0:
                # Cost went UP → Dr. Price Diff Account / Cr. Stock Valuation
                lines_vals += [
                    {'account_id': price_diff_acc.id,
                     'debit': diff_amount, 'credit': 0.0, 'name': self.name},
                    {'account_id': stock_val_acc.id,
                     'debit': 0.0, 'credit': diff_amount, 'name': self.name},
                ]
            else:
                # Cost went DOWN → Dr. Stock Valuation / Cr. Price Diff Account
                lines_vals += [
                    {'account_id': stock_val_acc.id,
                     'debit': diff_amount, 'credit': 0.0, 'name': self.name},
                    {'account_id': price_diff_acc.id,
                     'debit': 0.0, 'credit': diff_amount, 'name': self.name},
                ]

        _logger.info('ASVC price-diff: lines_vals count=%d', len(lines_vals))
        if not lines_vals:
            return

        entry = (
            self.env['account.move']
            .with_context(skip_stock_interim_comp=True)
            .with_company(self.company_id)
            .create({
                'company_id': self.company_id.id,
                'journal_id': journal.id,
                'date': self.date_done or fields.Date.context_today(self),
                'ref': 'Stock Price Diff: %s' % self.name,
                'move_type': 'entry',
                'line_ids': [(0, 0, v) for v in lines_vals],
            })
        )
        entry.with_context(skip_stock_interim_comp=True).action_post()
        _logger.info(
            'ASVC price-diff: entry %s created for picking %s',
            entry.name, self.name,
        )

    def _get_change_account(self):
        """
        Return the stock interim/variation account for this picking.
        Priority:
          1. Product template  property_account_expense_id / account_expense_id
          2. Product category  account_stock_variation_id
          3. Product category  property_account_expense_id
          4. Product category  property_account_expense_categ_id
          5. Company-level     stock_change_in_stock_account_id (Settings)
          6. General Settings → Product Accounts → Expense Account
        """
        # 1. Product template
        for move in self.move_ids:
            tmpl = move.product_id.product_tmpl_id
            for fname in ('property_account_expense_id', 'account_expense_id'):
                acc = getattr(tmpl, fname, False)
                if acc:
                    return acc
        # 2-4. Product category
        for move in self.move_ids:
            categ = move.product_id.categ_id
            acc = (
                getattr(categ, 'account_stock_variation_id', False)
                or getattr(categ, 'property_account_expense_id', False)
                or getattr(categ, 'property_account_expense_categ_id', False)
            )
            if acc:
                return acc
        # 5. Company-level
        acc = self.company_id.stock_change_in_stock_account_id
        if acc:
            return acc
        # 6. General Settings
        try:
            return self.env['ir.property'].sudo()._get(
                'property_account_expense_id', 'product.template'
            )
        except Exception:
            return False

    def _get_interim_journal(self):
        return self.company_id.get_stock_interim_journal()

    def _compute_stock_val_amounts(self, snap_ts=None):
        """
        Return {stock_valuation_account: amount} for all done moves in this
        picking that use real_time (perpetual) valuation.

        Value resolution order (Odoo 19 compatible):
        1. AML on move.account_move_id  – only for non-deferred flows
        2. Stock Valuation Layer sum    – Odoo ≤18 perpetual flow
        3. move.price_unit × qty        – approximation (AVCO/FIFO)

        When snap_ts is provided, AVCO revaluation SVLs created by Odoo
        during _action_done (stock_move_id=False, no account entry yet) are
        also included so the provisional covers the full inventory value change.
        """
        picking_code = self.picking_type_id.code
        if picking_code not in ('incoming', 'outgoing'):
            return {}

        change_acc = self._get_change_account()
        result = {}

        for move in self.move_ids.filtered(lambda m: m.state == 'done'):
            categ = move.product_id.categ_id
            if categ.property_valuation == 'real_time':
                stock_val_acc = categ.property_stock_valuation_account_id
            else:
                # Product has no category or category not set to real_time:
                # fall back to General Settings → Inventory Accounting → Valuation Account
                stock_val_acc = self._get_default_stock_val_account()
            if not stock_val_acc or stock_val_acc == change_acc:
                continue

            # Skip Kit (phantom BoM) moves — component moves in this same
            # picking already carry the correct accounts and values.
            if self._move_is_kit(move):
                continue

            amount = self._get_move_value(move, stock_val_acc, picking_code)

            if amount > 0.01:
                result[stock_val_acc] = result.get(stock_val_acc, 0.0) + amount

        # Include AVCO revaluation SVLs (no stock_move_id) created by Odoo
        # during _action_done — these represent changes in remaining inventory
        # value (e.g. AVCO drops from 150 to 100 after a vendor return) that
        # are not attached to any specific stock.move.
        if snap_ts:
            SVL = self.env.get('stock.valuation.layer')
            if SVL is not None:
                done_products = self.move_ids.filtered(
                    lambda m: m.state == 'done'
                ).product_id
                reval_layers = SVL.search([
                    ('stock_move_id', '=', False),
                    ('account_move_id', '=', False),
                    ('company_id', '=', self.company_id.id),
                    ('product_id', 'in', done_products.ids),
                    ('create_date', '>=', snap_ts),
                ])
                for layer in reval_layers:
                    categ = layer.product_id.categ_id
                    if categ.property_valuation == 'real_time':
                        stock_val_acc = categ.property_stock_valuation_account_id
                    else:
                        stock_val_acc = self._get_default_stock_val_account()
                    if not stock_val_acc or stock_val_acc == change_acc:
                        continue
                    reval_amount = abs(layer.value)
                    if reval_amount > 0.01:
                        result[stock_val_acc] = result.get(stock_val_acc, 0.0) + reval_amount

        return result

    @staticmethod
    def _resolve_fifo_move_unit_cost(start_move):
        """
        Trace back through the origin_returned_move_id chain to find the
        true unit cost for a FIFO move.

        Problem: when a return-of-vendor-return is itself returned and
        received back multiple times, a chain of moves forms where every
        move except the original receipt has value = 0 (Odoo 19 does not
        set price_unit on any return-of-vendor-return move).

        Example chain (all intermediate moves have value = 0):
            WH/IN/00052 (value=0)
              → VendorReturn3  (value=0)
              → WH/IN/00051   (value=0)
              → VendorReturn2  (value=0)
              → WH/IN/00050   (value=0)
              → VendorReturn1  (value=0)
              → WH/IN/00049   (value=100 SAR) ← first non-zero found

        The original receipt always has a non-zero value because it was
        created from a PO with a known price_unit.

        Returns unit cost (float) or 0.0 if not resolvable.
        """
        visited = set()
        current = start_move
        while current and current.id not in visited:
            visited.add(current.id)
            val = abs(current.value or 0.0)
            qty = current.quantity or 0.0
            if val > 0.001 and qty > 0.001:
                return val / qty
            current = getattr(current, 'origin_returned_move_id', None)
        return 0.0

    @staticmethod
    def _move_is_kit(move):
        """Return True if the move's product has a phantom (Kit) BoM."""
        BOM = move.env.get('mrp.bom')
        if BOM is None:
            return False
        return bool(BOM.search([
            ('type', '=', 'phantom'),
            '|',
            ('product_id', '=', move.product_id.id),
            ('product_tmpl_id', '=', move.product_id.product_tmpl_id.id),
        ], limit=1))

    def _is_pos_delivery(self):
        """
        Return True if this picking's operation type belongs to a POS
        configuration.  pos.config.picking_type_id is the most reliable
        link between a picking and POS in Odoo 19.
        """
        POS_CONFIG = self.env.get('pos.config')
        if POS_CONFIG is None:
            return False
        return bool(POS_CONFIG.search(
            [('picking_type_id', '=', self.picking_type_id.id)], limit=1
        ))

    def _has_foreign_currency(self):
        """
        Return True if any move in this picking is linked to a purchase or
        sale order that uses a currency different from the company currency.
        Provisional entries are skipped for foreign-currency pickings because
        no currency conversion is applied.
        """
        company_currency = self.company_id.currency_id
        for move in self.move_ids:
            pl = getattr(move, 'purchase_line_id', None)
            if pl:
                order_currency = (
                    getattr(pl, 'currency_id', None)
                    or getattr(pl.order_id, 'currency_id', None)
                )
                if order_currency and order_currency != company_currency:
                    return True
            sl = getattr(move, 'sale_line_id', None)
            if sl:
                order_currency = (
                    getattr(sl, 'currency_id', None)
                    or getattr(sl.order_id, 'currency_id', None)
                )
                if order_currency and order_currency != company_currency:
                    return True
        return False

    def _fix_fifo_standard_price(self):
        """
        Correct standard_price for FIFO products after any stock movement.

        Called explicitly from _action_done() after super() so it always
        runs regardless of whether the product.product.write() override
        intercepted Odoo's internal price update.

        Delegates to product._asvc_fifo_standard_price() which builds the
        correct FIFO cost from stock.quant (qty) + stock.move.value (cost),
        walking the origin chain for zero-value return-of-return moves.
        Does NOT use stock.move.remaining_qty which returns 0 in Odoo 19
        when ORM caches are stale.
        """
        fifo_products = self.move_ids.filtered(
            lambda m: m.state == 'done'
            and m.product_id.categ_id.property_cost_method == 'fifo'
        ).product_id

        if not fifo_products:
            return

        for product in fifo_products:
            correct_price = product._asvc_fifo_standard_price()

            if correct_price < 0.001:
                _logger.info(
                    'Auto Stock Variation Closure: FIFO standard_price — '
                    'no remaining stock for %s, skipping.',
                    product.display_name,
                )
                continue

            current_price = product.standard_price
            if abs(correct_price - current_price) < 0.001:
                _logger.debug(
                    'Auto Stock Variation Closure: FIFO standard_price already '
                    'correct for %s (%.4f)', product.display_name, current_price,
                )
                continue

            _logger.info(
                'Auto Stock Variation Closure: fixing FIFO standard_price for '
                '%s: %.4f → %.4f',
                product.display_name, current_price, correct_price,
            )
            product.with_context(
                disable_auto_svl=True,
                asvc_fixing_fifo_price=True,
            ).sudo().write({'standard_price': correct_price})

    def _is_subcontracting_resupply(self):
        """
        Return True if this picking moves raw materials from an internal warehouse
        location to a supplier/subcontracting virtual location.
        """
        return (
            self.location_id.usage == 'internal'
            and self.location_dest_id.usage == 'supplier'
        )

    def _is_pos_ship_later(self):
        """
        Return True if this picking is a POS "Ship Later" outgoing delivery.

        Ship-later pickings are created by Odoo POS when the "Ship Later"
        feature is activated.  Unlike immediate POS pickings (which use the
        POS operation type), ship-later pickings use the warehouse's normal
        OUT operation type and are linked to a pos.order via
        pos.order.picking_ids.
        """
        if self.picking_type_id.code != 'outgoing':
            return False
        POS_ORDER = self.env.get('pos.order')
        if POS_ORDER is None:
            return False
        return bool(POS_ORDER.search(
            [('picking_ids', 'in', [self.id])], limit=1
        ))

    def _is_return_of_vendor_return(self):
        """
        Return True if this picking is a "return of vendor return" — i.e. an
        incoming picking (supplier → internal) where the returned move's
        origin is itself a vendor return (internal → supplier).

        Example flow:
            WH/IN  (receipt)            — supplier → internal
            WH/OUT (vendor return)      — internal → supplier  [origin = WH/IN]
            WH/IN  (return of return)   — supplier → internal  [origin = WH/OUT]
                                                                 ↑ this picking

        Odoo 19 FIFO creates the stock journal entry immediately during
        _action_done for this type of picking.  If we also create our
        Auto-Interim-Close entry, the stock valuation account is debited
        twice, inflating the inventory balance by the move amount.
        """
        if self.picking_type_id.code != 'incoming':
            return False
        for move in self.move_ids:
            origin = getattr(move, 'origin_returned_move_id', None)
            if not origin:
                continue
            if (
                origin.location_id.usage == 'internal'
                and origin.location_dest_id.usage == 'supplier'
            ):
                return True
        return False

    @staticmethod
    def _get_move_value(move, stock_val_acc, picking_code):
        """
        Compute the monetary value for a single done stock.move.
        Returns a positive float; 0.0 if the value cannot be determined.
        Never calls private Odoo internals (_get_aml_value, etc.).
        """
        # 0. Standard Price: always use the product's standard cost.
        #    move.value (and any existing AML) may reflect the purchase/sale
        #    price instead of the standard cost in Odoo 19, so we bypass all
        #    other steps for Standard Price products.
        if move.product_id.categ_id.property_cost_method == 'standard':
            ctx = {'to_date': move.date, 'company': move.company_id}
            std_price = move.product_id.with_context(**ctx).standard_price
            return abs(move.product_uom_qty * std_price)

        # 1. Existing account.move on the stock move (non-deferred flows)
        if move.account_move_id:
            for aml in move.account_move_id.line_ids:
                if aml.account_id == stock_val_acc:
                    raw = aml.debit if picking_code == 'incoming' else aml.credit
                    if raw > 0:
                        return raw

        # 1.5 FIFO vendor return: use the original receipt value so the
        #     provisional matches the credit note amount and prevents a
        #     phantom price-difference line in the compensation entry.
        #     AVCO returns are left at AVCO cost (standard behaviour).
        #     Detection: move goes from internal stock → supplier location,
        #     regardless of the operation type code (vendor return pickings
        #     in Odoo keep the 'incoming' operation type of the original receipt).
        origin = getattr(move, 'origin_returned_move_id', None)
        if (
            move.location_id.usage == 'internal'
            and move.location_dest_id.usage == 'supplier'
            and origin
            and move.product_id.categ_id.property_cost_method == 'fifo'
        ):
            orig_val = abs(origin.value or 0.0)
            orig_qty = origin.product_uom_qty or 0.0
            if orig_val and orig_qty:
                returned_qty = (
                    sum(
                        ml.product_uom_id._compute_quantity(
                            ml.qty_done, move.product_id.uom_id
                        )
                        for ml in move.move_line_ids
                    )
                    or move.product_uom_qty
                )
                if returned_qty:
                    return orig_val / orig_qty * returned_qty

        # 1.6 FIFO return-of-vendor-return: supplier → internal, where the
        #     origin move is itself a vendor return (internal → supplier).
        #     Odoo 19 does NOT set price_unit on these moves (stays NULL → 0.0
        #     in ORM), so move.value is 0.  We derive the correct unit cost
        #     from the vendor return move's value (already corrected by
        #     _fix_vendor_return_move_value to match the original receipt cost).
        if (
            move.location_id.usage == 'supplier'
            and move.location_dest_id.usage == 'internal'
            and origin
            and origin.location_id.usage == 'internal'
            and origin.location_dest_id.usage == 'supplier'
            and move.product_id.categ_id.property_cost_method == 'fifo'
        ):
            # Trace back through the full return chain to find the original
            # receipt cost.  Direct origin.value may be 0 when the vendor
            # return itself originated from a prior return-of-vendor-return
            # (multi-cycle chains: receive→return→receive→return→…).
            unit_cost = StockPicking._resolve_fifo_move_unit_cost(move)
            if unit_cost > 0.001:
                return abs(move.product_uom_qty * unit_cost)

        # 2. move.value (Odoo 19 stored field — most authoritative, already
        #    corrected by _fix_vendor_return_move_value for vendor returns)
        move_val = getattr(move, 'value', None)
        if move_val and abs(move_val) > 0.01:
            return abs(move_val)

        # 3. Stock Valuation Layers (Odoo ≤18 perpetual)
        layers = getattr(move, 'stock_valuation_layer_ids', None)
        if layers:
            svl_value = abs(sum(layers.mapped('value')))
            if svl_value > 0.01:
                return svl_value

        # 4. standard_price × done qty  (approximation — always use product
        #    cost, never the purchase/sale price_unit)
        ctx = {'to_date': move.date, 'company': move.company_id}
        price = move.product_id.with_context(**ctx).standard_price
        amount = abs(move.product_uom_qty * price)
        if not amount:
            _logger.info(
                'Auto Stock Variation Closure: zero value for move id=%s '
                '(product: %s, price_unit=%.4f, standard_price=%.4f, qty=%.4f). '
                'No provisional entry will be created for this line.',
                move.id,
                move.product_id.display_name,
                move.price_unit,
                move.product_id.standard_price,
                move.product_uom_qty,
            )
        return amount

    @staticmethod
    def _get_cogs_account_for_product(product, company):
        """
        Return the COGS / expense account for *product*.
        Priority:
          1. Expense account on the product template
          2. Expense account on the product category
          3. Company-level expense / price-difference account
        """
        tmpl = product.product_tmpl_id
        for fname in ('property_account_expense_id', 'account_expense_id'):
            acc = getattr(tmpl, fname, False)
            if acc:
                return acc
        categ = product.categ_id
        for fname in ('property_account_expense_categ_id', 'account_expense_categ_id'):
            acc = getattr(categ, fname, False)
            if acc:
                return acc
        for fname in ('account_expense_id', 'account_expense_categ_id',
                      'stock_price_diff_account_id'):
            acc = getattr(company, fname, False)
            if acc:
                return acc
        return False

    @staticmethod
    def _get_kit_product_for_component(move):
        """
        If move.product_id is a component of a phantom (Kit) BoM,
        return the Kit product.  Otherwise return None.
        """
        BOM_LINE = move.env.get('mrp.bom.line')
        if BOM_LINE is None:
            return None
        bom_lines = BOM_LINE.search([('product_id', '=', move.product_id.id)])
        for bl in bom_lines:
            if getattr(bl.bom_id, 'type', '') == 'phantom':
                kit = bl.bom_id.product_id
                if not kit and bl.bom_id.product_tmpl_id:
                    kit = bl.bom_id.product_tmpl_id.product_variant_id
                if kit:
                    return kit
        return None

    # ------------------------------------------------------------------
    # Entry creation
    # ------------------------------------------------------------------

    def _append_pos_kit_cogs_lines(self, kit_move, change_acc, lines_vals):
        """
        Append Dr./Cr. journal line pairs for a phantom-BoM (Kit) product move
        in a POS ship-later picking.

        POS ship-later pickings carry the Kit product move directly — Odoo does
        not pre-explode it into component moves.  We resolve the BoM here:

            Dr. Kit expense account          (kit product → kit category → company)
            Cr. Component valuation account  (real_time categories only)

        Amount per component:
            (kit_qty / bom.product_qty) × bom_line.product_qty × comp.standard_price
        """
        BOM = self.env.get('mrp.bom')
        if BOM is None:
            return

        product = kit_move.product_id
        kit_qty = kit_move.product_uom_qty

        bom = BOM.search([
            ('type', '=', 'phantom'),
            '|',
            ('product_id', '=', product.id),
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
        ], limit=1)
        if not bom:
            _logger.warning(
                'Auto Stock Variation Closure: kit %s has no phantom BoM — '
                'skipping ship-later COGS for move %s.',
                product.display_name, kit_move.id,
            )
            return

        kit_cogs_acc = self._get_cogs_account_for_product(product, self.company_id)
        if not kit_cogs_acc:
            _logger.warning(
                'Auto Stock Variation Closure: no COGS account for kit %s '
                'in POS ship-later picking %s — skipping.',
                product.display_name, self.name,
            )
            return

        bom_factor = kit_qty / (bom.product_qty or 1.0)

        for bom_line in bom.bom_line_ids:
            comp = bom_line.product_id
            if not comp:
                continue
            comp_categ = comp.categ_id
            if comp_categ.property_valuation == 'real_time':
                stock_val_acc = comp_categ.property_stock_valuation_account_id
            else:
                stock_val_acc = self._get_default_stock_val_account()
            if not stock_val_acc or stock_val_acc == change_acc:
                continue

            comp_qty = bom_factor * bom_line.product_qty
            amount = abs(comp_qty * comp.standard_price)
            if amount < 0.01:
                continue

            lines_vals += [
                {
                    'account_id': kit_cogs_acc.id,
                    'debit': amount,
                    'credit': 0.0,
                    'name': self.name,
                },
                {
                    'account_id': stock_val_acc.id,
                    'debit': 0.0,
                    'credit': amount,
                    'name': self.name,
                },
            ]

    def _create_pos_ship_later_cogs_entry(self):
        """
        Create a COGS journal entry for a POS "Ship Later" delivery.

        When the Odoo POS "Ship Later" feature is used, the sale and the
        physical delivery happen at different times.  The standard provisional
        Stock-Variation entry must NOT be created (there is no pending bill to
        reverse it against).  Instead we post a direct COGS entry at the
        moment of delivery:

            Dr. COGS / Expense account
            Cr. Stock Valuation account

        Account-resolution priority
        ---------------------------
        Dr (COGS):
            1. Expense account on the product template
            2. Expense account on the product category
            3. Company-level expense account (general settings)

        Cr (Stock Valuation):
            For regular products:
                product category stock-valuation account
            For Kit (phantom BoM) products, the picking carries the Kit move
            directly (not pre-exploded).  _append_pos_kit_cogs_lines() resolves
            the BoM and creates one Dr./Cr. pair per component.
        """
        # Skip foreign-currency pickings — no currency conversion is applied.
        if self._has_foreign_currency():
            _logger.warning(
                'Auto Stock Variation Closure: POS ship-later picking %s '
                'involves a foreign currency — COGS entry skipped. '
                'Post manually if needed.',
                self.name,
            )
            return

        journal = self._get_interim_journal()
        if not journal:
            return

        change_acc = self._get_change_account()
        lines_vals = []

        for move in self.move_ids.filtered(lambda m: m.state == 'done'):
            product = move.product_id
            categ = product.categ_id

            # Kit (phantom BoM) move: POS ship-later pickings carry the Kit
            # product move directly — Odoo does NOT pre-explode them into
            # component moves.  Delegate to the BoM-based helper so each
            # component gets its own Dr./Cr. pair.
            if self._move_is_kit(move):
                self._append_pos_kit_cogs_lines(move, change_acc, lines_vals)
                continue

            # ---- Cr: component's own stock-valuation account ----
            if categ.property_valuation == 'real_time':
                stock_val_acc = categ.property_stock_valuation_account_id
            else:
                stock_val_acc = self._get_default_stock_val_account()
            if not stock_val_acc or stock_val_acc == change_acc:
                continue

            # ---- Dr: expense account ----
            # If this product is a component of a Kit, debit the Kit's
            # expense account.  Otherwise debit the product's own expense account.
            kit = self._get_kit_product_for_component(move)
            cogs_acc = self._get_cogs_account_for_product(
                kit if kit else product, self.company_id
            )
            if not cogs_acc:
                _logger.warning(
                    'Auto Stock Variation Closure: no COGS account found for '
                    '%s in POS ship-later picking %s — skipping move.',
                    (kit or product).display_name, self.name,
                )
                continue

            amount = self._get_move_value(move, stock_val_acc, 'outgoing')
            if amount < 0.01:
                continue

            lines_vals += [
                {
                    'account_id': cogs_acc.id,
                    'debit': amount,
                    'credit': 0.0,
                    'name': self.name,
                },
                {
                    'account_id': stock_val_acc.id,
                    'debit': 0.0,
                    'credit': amount,
                    'name': self.name,
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
                'date': self.date_done or fields.Date.context_today(self),
                'ref': 'POS Ship-Later COGS: %s' % self.name,
                'move_type': 'entry',
                'line_ids': [(0, 0, v) for v in lines_vals],
            })
        )
        entry.with_context(skip_stock_interim_comp=True).action_post()
        self.auto_interim_move_id = entry.id
        _logger.info(
            'Auto Stock Variation Closure: POS ship-later COGS entry %s '
            'created for picking %s',
            entry.name, self.name,
        )

    def _create_auto_interim_entry(self, snap_ts=None):
        """
        Post a provisional journal entry right after the picking is validated.

        Purchase receipt  →  Dr. Stock Account   /  Cr. Change in Stock
        Sale delivery     →  Dr. Change in Stock  /  Cr. Stock Account

        This keeps the Inventory Valuation report balanced (Stock Variation = 0)
        immediately after the movement, without waiting for the bill/invoice.
        The entry is offset when the corresponding bill or invoice is posted.
        """
        if self.auto_interim_move_id:
            return

        change_acc = self._get_change_account()
        if not change_acc:
            return

        picking_code = self.picking_type_id.code
        # Normalise non-standard operation-type codes by location usage.
        if picking_code not in ('incoming', 'outgoing'):
            src_usage = self.location_id.usage
            dst_usage = self.location_dest_id.usage
            if src_usage == 'internal' and dst_usage == 'supplier':
                picking_code = 'outgoing'
            elif src_usage == 'supplier' and dst_usage == 'internal':
                picking_code = 'incoming'

        if picking_code not in ('incoming', 'outgoing'):
            return

        # Subcontracting resupply (internal → supplier): raw cost is booked
        # directly Dr. FG Val / Cr. Raw Mat Val at MO completion — no interim here.
        if picking_code == 'outgoing' and self._is_subcontracting_resupply():
            return

        # Skip all POS deliveries — variation entries are not used for POS
        # because the sale and delivery happen simultaneously in the order.
        if picking_code == 'outgoing' and self._is_pos_delivery():
            return

        journal = self._get_interim_journal()
        if not journal:
            return

        is_expense_based = (
            picking_code == 'outgoing'
            and self.company_id.stock_sales_use_expense_interim
        )

        lines_vals = []

        if is_expense_based:
            # Per-move: Dr. Product Expense Account / Cr. Stock Val
            # Priority: product template → category → general settings
            for move in self.move_ids.filtered(lambda m: m.state == 'done'):
                categ = move.product_id.categ_id
                if categ.property_valuation != 'real_time':
                    continue
                stock_val_acc = categ.property_stock_valuation_account_id
                if not stock_val_acc:
                    continue
                if self._move_is_kit(move):
                    continue
                expense_acc = self._get_cogs_account_for_product(
                    move.product_id, self.company_id
                )
                if not expense_acc or expense_acc == stock_val_acc:
                    continue
                amount = self._get_move_value(move, stock_val_acc, 'outgoing')
                if amount < 0.01:
                    continue
                lines_vals += [
                    {'account_id': expense_acc.id, 'debit': amount, 'credit': 0.0, 'name': self.name},
                    {'account_id': stock_val_acc.id, 'debit': 0.0, 'credit': amount, 'name': self.name},
                ]
        else:
            amounts = self._compute_stock_val_amounts(snap_ts=snap_ts)
            for stock_val_acc, amount in amounts.items():
                if picking_code == 'incoming':
                    # Dr. Stock Account  /  Cr. Change in Stock
                    lines_vals += [
                        {'account_id': stock_val_acc.id, 'debit': amount, 'credit': 0.0, 'name': self.name},
                        {'account_id': change_acc.id, 'debit': 0.0, 'credit': amount, 'name': self.name},
                    ]
                else:
                    # Dr. Change in Stock  /  Cr. Stock Account
                    lines_vals += [
                        {'account_id': change_acc.id, 'debit': amount, 'credit': 0.0, 'name': self.name},
                        {'account_id': stock_val_acc.id, 'debit': 0.0, 'credit': amount, 'name': self.name},
                    ]

        if not lines_vals:
            return

        auto_move = (
            self.env['account.move']
            .with_context(skip_stock_interim_comp=True)
            .with_company(self.company_id)
            .create({
                'company_id': self.company_id.id,
                'journal_id': journal.id,
                'date': self.date_done or fields.Date.context_today(self),
                'ref': 'Auto Interim Close: %s' % self.name,
                'move_type': 'entry',
                'auto_interim_is_expense_based': is_expense_based,
                'line_ids': [(0, 0, v) for v in lines_vals],
            })
        )
        auto_move.with_context(skip_stock_interim_comp=True).action_post()
        self.auto_interim_move_id = auto_move.id
