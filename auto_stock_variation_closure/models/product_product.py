import logging

from odoo import models

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    # ------------------------------------------------------------------
    # Intercept standard_price writes for FIFO products
    # ------------------------------------------------------------------

    def write(self, vals):
        """
        Intercept standard_price writes for FIFO products.

        Problem (Odoo 19)
        -----------------
        After every stock operation (receipt, vendor return, etc.), Odoo 19
        updates standard_price using:

            standard_price = sum(ALL ever-received values) / qty_on_hand

        This is wrong for FIFO because consumed layers are still counted.
        Example: 3 receipts (120+80+100=300), 1 returned, 1 re-received:
            All values = 120+80+100+100+100 = 500  (3 on hand)
            Odoo sets standard_price = 500/3 = 166.67 SR  ← WRONG
            Correct = (120+80+100_new) / 3 = 100 SR      ← RIGHT

        Fix
        ---
        Whenever Odoo writes standard_price for a FIFO product we compute
        the correct value via _asvc_fifo_standard_price() which manually
        builds the FIFO stack from stock.quant (qty) + stock.move.value
        (cost), without relying on the unreliable remaining_qty computed
        field.

        Context guards
        --------------
        - asvc_fixing_fifo_price : recursive guard — pass through unchanged

        Note: we intentionally do NOT guard on disable_auto_svl=True.
        Odoo 19 itself sets disable_auto_svl=True when it writes
        standard_price after a FIFO receipt (_update_fifo_svl / similar).
        If we honoured that guard we would skip the very writes we need
        to intercept, leaving the inflated price in place.
        """
        ctx = self.env.context
        if (
            'standard_price' in vals
            and not ctx.get('asvc_fixing_fifo_price')
        ):
            fifo_self = self.filtered(
                lambda p: p.categ_id.property_cost_method == 'fifo'
            )
            for product in fifo_self:
                correct_price = product._asvc_fifo_standard_price()
                if correct_price < 0.001:
                    # No remaining stock yet — let Odoo set its value
                    continue
                _logger.info(
                    'ASVC: intercepting standard_price write for FIFO %s: '
                    'Odoo=%.4f  correct=%.4f',
                    product.display_name,
                    vals['standard_price'],
                    correct_price,
                )
                product.with_context(
                    disable_auto_svl=True,
                    asvc_fixing_fifo_price=True,
                ).sudo().write({'standard_price': correct_price})
                # Remove from the main write (already handled above)
                self = self - product

        if not self:
            return True
        return super().write(vals)

    def _asvc_fifo_standard_price(self):
        """
        Compute the correct FIFO standard_price for this product.

        Does NOT use stock.move.remaining_qty / remaining_value because
        those computed fields depend on product.qty_available which can
        return 0 when ORM caches are stale (e.g. during _action_done before
        the current transaction is flushed, or in shell context).

        Algorithm
        ---------
        1. Read qty-on-hand directly from stock.quant (always fresh).
        2. Fetch all done supplier→internal moves ordered newest-first.
           FIFO: oldest items are consumed first, so the remaining stock
           lives in the newest moves.
        3. Distribute qty_on_hand across moves (newest-first) and sum the
           weighted cost, walking origin_returned_move_id chains for moves
           that have value = 0 (Odoo 19 return-of-vendor-return pattern).
        4. Return total_weighted_value / qty_on_hand.

        Returns 0.0 when there is no remaining stock or the cost chain
        cannot be resolved.
        """
        self.ensure_one()
        company = self.env.company

        # ── Step 1: qty on hand from quants (bypasses qty_available cache) ──
        quants = self.env['stock.quant'].sudo().search([
            ('product_id', '=', self.id),
            ('location_id.usage', '=', 'internal'),
            ('company_id', '=', company.id),
        ])
        qty_avail = sum(q.quantity for q in quants)
        if qty_avail < 0.001:
            return 0.0

        # ── Step 2: incoming moves, newest first ─────────────────────────────
        in_moves = self.env['stock.move'].search([
            ('product_id', '=', self.id),
            ('company_id', '=', company.id),
            ('state', '=', 'done'),
            ('location_id.usage', '=', 'supplier'),
            ('location_dest_id.usage', '=', 'internal'),
        ], order='date desc, id desc')
        # ── Step 3: net capacity per incoming move ────────────────────────────
        vendor_returns = self.env['stock.move'].search([
            ('product_id', '=', self.id),
            ('company_id', '=', company.id),
            ('state', '=', 'done'),
            ('origin_returned_move_id', 'in', in_moves.ids),
            ('location_id.usage', '=', 'internal'),
            ('location_dest_id.usage', '=', 'supplier'),
        ])
        returned_qty_by_move = {}
        for ret in vendor_returns:
            mid = ret.origin_returned_move_id.id
            returned_qty_by_move[mid] = (
                returned_qty_by_move.get(mid, 0.0) + ret.quantity
            )
        # ── Step 3b: anchor cost for broken-chain fallback ───────────────────────
        # When a re-receive's origin chain is broken (historical data where
        # origin_returned_move_id was never set), its .value is stamped at the
        # inflated standard_price and cannot be trusted.
        #
        # Strategy: walk in_moves oldest-first and find the first move that has
        # a confirmed PO price (purchase_line_id.price_unit).  This value is
        # stored on the purchase order line and is NEVER affected by
        # standard_price inflation — it is the agreed supplier price.
        # If no PO link exists at all, fall back to the oldest move's .value.
        oldest_unit_cost = 0.0
        for cand in reversed(list(in_moves)):   # oldest first
            pol = getattr(cand, 'purchase_line_id', None)
            if pol:
                pu = pol.price_unit or 0.0
                if pu > 0.001:
                    oldest_unit_cost = pu
                    _logger.warning(
                        'ASVC FIFO: anchor cost for %s = %.4f '
                        '(from PO line %s, move %s)',
                        self.display_name, pu, pol.id, cand.id)
                    break
            # No PO line on this move — try move.value
            ov = abs(cand.value or 0.0)
            oq = cand.quantity or 0.0
            if ov > 0.001 and oq > 0.001:
                oldest_unit_cost = ov / oq
                break
        # ── Step 4: distribute qty to moves and sum weighted cost ─────────────
        remaining = qty_avail
        total_value = 0.0
        for move in in_moves:
            if remaining < 0.001:
                break
            move_qty = move.quantity or 0.0
            returned = returned_qty_by_move.get(move.id, 0.0)
            net_capacity = max(0.0, move_qty - returned)
            if net_capacity < 0.001:
                continue
            assigned = min(net_capacity, remaining)
            unit_cost = ProductProduct._asvc_resolve_move_unit_cost(
                move, oldest_unit_cost=oldest_unit_cost)
            total_value += assigned * unit_cost
            remaining -= assigned

        if total_value < 0.001:
            return 0.0
        return total_value / qty_avail

    @staticmethod
    def _asvc_resolve_move_unit_cost(move, oldest_unit_cost=0.0):
        """
        Walk origin_returned_move_id chain to the ORIGINAL receipt and
        return its unit cost.

        Broken-chain fallback
        ---------------------
        If the chain ends at a move with no PO link (purchase_line_id /
        picking.purchase_id = None), the move's .value may be stamped at
        inflated standard_price (historical data issue).

        Fallback order:
        1. price_unit of that end-of-chain move, if it differs from val/qty
           (sometimes correctly propagated through the Return wizard).
        2. oldest_unit_cost: the unit cost of the OLDEST in_move passed in
           by the caller — the most FIFO-consistent anchor available.
        3. abs(value)/quantity as last resort.
        """
        visited = set()
        cur = move
        while cur and cur.id not in visited:
            visited.add(cur.id)
            origin = getattr(cur, 'origin_returned_move_id', None)
            po_id = None
            pol = getattr(cur, 'purchase_line_id', None)
            if pol:
                po_id = pol.id
            elif getattr(cur, 'picking_id', None):
                po = getattr(cur.picking_id, 'purchase_id', None)
                if po:
                    po_id = 'PO:%s' % po.id
            if not origin:
                # End of chain
                val = abs(cur.value or 0.0)
                qty = cur.quantity or 0.0
                has_po = bool(po_id)
                val_per_qty = val / qty if qty > 0.001 else 0.0

                if has_po:
                    # Real PO receipt — prefer purchase_line_id.price_unit
                    # (stored on the PO line, immune to std_price inflation)
                    if pol and (pol.price_unit or 0.0) > 0.001:
                        return pol.price_unit
                    return val_per_qty if val > 0.001 and qty > 0.001 else 0.0

                # ── Broken chain: no PO link ─────────────────────────────
                # The move's .value was stamped at inflated standard_price.
                # Use anchors that are NOT affected by that inflation.
                price_unit = getattr(cur, 'price_unit', 0.0) or 0.0

                # Fallback 1: price_unit if meaningfully different from val
                if price_unit > 0.001 and abs(price_unit - val_per_qty) > 0.01:
                    return price_unit

                # Fallback 2: oldest PO-anchored cost (most reliable anchor)
                if oldest_unit_cost > 0.001:
                    _logger.warning(
                        'ASVC resolve_chain: move %s broken chain → '
                        'using anchor cost %.4f (val_per_qty was %.4f)',
                        move.id, oldest_unit_cost, val_per_qty)
                    return oldest_unit_cost

                # Fallback 3: last resort
                return val_per_qty if val > 0.001 and qty > 0.001 else 0.0

            cur = origin
        return 0.0

    # ------------------------------------------------------------------
    # _get_remaining_moves override (FIFO vendor-return correction)
    # ------------------------------------------------------------------

    def _get_remaining_moves(self):
        """
        Override: correct the FIFO remaining-qty stack for vendor returns.

        Odoo 19 algorithm (_run_fifo_get_stack)
        ----------------------------------------
        Distributes qty_available to the NEWEST incoming moves (newest-first).
        A vendor return lowers qty_available but does NOT mark the specific
        returned receipt as consumed.  Result: the wrong receipt can appear in
        the "Remaining" filter while the correct older receipt shows 0.

        Example (user's scenario)
        -------------------------
        Receipts: A(120 SAR, oldest) / B(100 SAR) / C(80 SAR, newest)
        Return 1 unit linked to C via origin_returned_move_id.
        qty_available = 2.
        Odoo fills: C→1, B→1, A→0   ← WRONG (C was returned)
        Correct:    C→0, B→1, A→1

        Fix (post-processing)
        ---------------------
        For each done vendor return with origin_returned_move_id = R:
            correct_remaining(R) = max(0, R.quantity − returned_qty)
            over_credit = odoo_remaining(R) − correct_remaining(R)

        If over_credit > 0:
          • Reduce R's remaining by over_credit.
          • Restore that qty to the oldest incoming moves that still have
            capacity (their received qty > current remaining), oldest-first.

        Safe for:
          • Partial returns: received 3 from R, returned 1 → R stays at 2.
          • Multiple returns from the same receipt.
          • Returns from the oldest receipt (already excluded by Odoo → no-op).
          • Non-FIFO products (skipped entirely).
        """
        result = super()._get_remaining_moves()

        # Only FIFO products are relevant
        fifo_products = self.filtered(
            lambda p: p.categ_id.property_cost_method == 'fifo'
        )
        if not fifo_products:
            return result

        # ── Batch-fetch all done vendor returns for FIFO products ──────────
        all_vendor_returns = self.env['stock.move'].search([
            ('product_id', 'in', fifo_products.ids),
            ('company_id', '=', self.env.company.id),
            ('state', '=', 'done'),
            ('origin_returned_move_id', '!=', False),
            ('location_id.usage', '=', 'internal'),
            ('location_dest_id.usage', '=', 'supplier'),
        ])
        if not all_vendor_returns:
            return result

        # Group: product_id → {origin_move: total_returned_qty}
        returns_by_product = {}
        for ret in all_vendor_returns:
            origin = ret.origin_returned_move_id
            if not origin:
                continue
            pid = ret.product_id.id
            if pid not in returns_by_product:
                returns_by_product[pid] = {}
            returns_by_product[pid][origin] = (
                returns_by_product[pid].get(origin, 0.0) + ret.quantity
            )

        if not returns_by_product:
            return result

        # ── Batch-fetch all incoming done moves for affected products ──────
        # (needed to find oldest moves for qty restoration)
        affected_ids = list(returns_by_product.keys())
        all_in_moves = self.env['stock.move'].search(
            [
                ('product_id', 'in', affected_ids),
                ('company_id', '=', self.env.company.id),
                ('state', '=', 'done'),
                ('is_in', '=', True),
            ],
            order='date asc, id asc',
        )
        in_moves_by_product = {}
        for move in all_in_moves:
            pid = move.product_id.id
            in_moves_by_product.setdefault(pid, []).append(move)

        # ── Adjust each FIFO product that has vendor returns ───────────────
        for product in fifo_products:
            pid = product.id
            if pid not in returns_by_product:
                continue

            qty_by_move = result.get(product)
            if qty_by_move is None:
                qty_by_move = {}

            returned_by_origin = returns_by_product[pid]

            # Step 1: find over-credited receipts and compute how much to free
            total_to_restore = 0.0
            for origin_move, returned_qty in returned_by_origin.items():
                in_qty = origin_move.quantity          # total received qty
                correct_remaining = max(0.0, in_qty - returned_qty)
                odoo_remaining = qty_by_move.get(origin_move, 0.0)
                over_credit = odoo_remaining - correct_remaining
                if over_credit < 0.001:
                    continue

                new_remaining = max(0.0, correct_remaining)
                if new_remaining < 0.001:
                    qty_by_move.pop(origin_move, None)
                else:
                    qty_by_move[origin_move] = new_remaining
                total_to_restore += over_credit
                _logger.debug(
                    'ASVC FIFO: product=%s receipt_move=%s '
                    'odoo_remaining=%.4f correct_remaining=%.4f '
                    'over_credit=%.4f',
                    product.display_name, origin_move.id,
                    odoo_remaining, correct_remaining, over_credit,
                )

            # Step 2: restore freed qty to oldest incoming moves with capacity.
            corrected_origins = set(returned_by_origin.keys())
            if total_to_restore > 0.001:
                for move in in_moves_by_product.get(pid, []):
                    if total_to_restore < 0.001:
                        break
                    if move in corrected_origins:
                        continue
                    current = qty_by_move.get(move, 0.0)
                    capacity = move.quantity - current
                    if capacity < 0.001:
                        continue
                    restore = min(capacity, total_to_restore)
                    qty_by_move[move] = current + restore
                    total_to_restore -= restore
                    _logger.debug(
                        'ASVC FIFO: restored %.4f to move %s (product=%s)',
                        restore, move.id, product.display_name,
                    )

            result[product] = qty_by_move

        return result
