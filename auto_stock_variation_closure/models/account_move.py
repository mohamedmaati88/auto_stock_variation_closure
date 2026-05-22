import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    stock_interim_comp_move_id = fields.Many2one(
        'account.move',
        string='Stock Interim Compensation Entry',
        readonly=True,
        copy=False,
    )
    auto_interim_is_expense_based = fields.Boolean(
        string='Expense-Based Interim',
        readonly=True,
        copy=False,
        help='True when this provisional entry used product expense accounts '
             'instead of the standard stock variation account.',
    )

    # ------------------------------------------------------------------
    # Overrides
    # ------------------------------------------------------------------

    def _post(self, soft=True):
        """
        Override _post() to remap Kit product COGS lines.
        POS and some other flows call _post() directly (bypassing action_post),
        so this is the reliable hook for the Kit COGS remapping only.
        Compensation entry creation remains in action_post() as before.
        """
        result = super()._post(soft=soft)
        if self.env.context.get('skip_stock_interim_comp'):
            return result
        for move in self.filtered(lambda m: m.state == 'posted'):
            if not move.company_id.stock_auto_interim_close:
                continue
            if move.move_type in ('out_invoice', 'out_refund'):
                try:
                    move._remap_kit_cogs_accounts()
                except Exception:
                    _logger.exception(
                        'Auto Stock Variation Closure: failed to remap Kit COGS '
                        'accounts for %s — skipping.',
                        move.name,
                    )
                try:
                    move._remap_kit_cogs_debit_accounts()
                except Exception:
                    _logger.exception(
                        'Auto Stock Variation Closure: failed to remap Kit COGS '
                        'debit accounts for %s — skipping.',
                        move.name,
                    )
            elif move.move_type == 'entry':
                try:
                    move._remap_kit_cogs_debit_from_pos_session()
                except Exception:
                    _logger.exception(
                        'Auto Stock Variation Closure: failed to remap Kit COGS '
                        'debit accounts (POS session entry) for %s — skipping.',
                        move.name,
                    )
        return result

    def action_post(self):
        result = super().action_post()
        if self.env.context.get('skip_stock_interim_comp'):
            return result
        for move in self.filtered(lambda m: m.state == 'posted'):
            if move.move_type not in ('in_invoice', 'in_refund', 'out_invoice', 'out_refund'):
                continue
            if not move.company_id.stock_auto_interim_close:
                continue
            comp = move.stock_interim_comp_move_id
            if comp and comp.state == 'posted':
                continue  # already posted — nothing to do
            if comp and comp.state == 'draft':
                # bill re-posted after draft reset → cancel old entry and
                # recreate so amounts reflect any changes made to the bill
                try:
                    comp.with_context(skip_stock_interim_comp=True).button_cancel()
                except Exception:
                    _logger.exception(
                        'Auto Stock Variation Closure: failed to cancel draft '
                        'compensation entry %s before recreating for invoice %s',
                        comp.name, move.name,
                    )
                move._create_interim_compensation()
            else:
                # cancelled or never created → create new
                move._create_interim_compensation()


    def button_draft(self):
        for move in self:
            comp = move.stock_interim_comp_move_id
            if comp and comp.state == 'posted':
                try:
                    # reset to draft only — keep the entry so it can be
                    # re-posted automatically when the bill is re-posted
                    comp.with_context(skip_stock_interim_comp=True).button_draft()
                except Exception:
                    _logger.exception(
                        'Auto Stock Variation Closure: failed to reset compensation '
                        'entry %s to draft when resetting invoice %s to draft',
                        comp.name, move.name,
                    )
        return super().button_draft()

    def button_cancel(self):
        for move in self:
            comp = move.stock_interim_comp_move_id
            if comp and comp.state == 'posted':
                try:
                    comp.with_context(skip_stock_interim_comp=True).button_draft()
                    comp.with_context(skip_stock_interim_comp=True).button_cancel()
                except Exception:
                    _logger.exception(
                        'Auto Stock Variation Closure: failed to cancel compensation '
                        'entry %s during button_cancel on invoice %s',
                        comp.name, move.name,
                    )
        return super().button_cancel()

    # ------------------------------------------------------------------
    # Smart button
    # ------------------------------------------------------------------

    def action_view_stock_interim_comp(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Interim Compensation Entry',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.stock_interim_comp_move_id.id,
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_stock_val_accounts(self):
        """
        Return the set of stock-valuation accounts for real_time products on
        this invoice, excluding the configured Change-in-Stock account.
        Uses property_valuation instead of product.type (changed in Odoo 18/19).
        """
        change_acc = self._get_change_account()
        accs = set()
        for line in self.invoice_line_ids:
            product = line.product_id
            if not product:
                continue
            categ = product.categ_id
            if categ.property_valuation == 'real_time':
                acc = categ.property_stock_valuation_account_id
            else:
                acc = self._get_default_stock_val_account()
            if acc and acc != change_acc:
                accs.add(acc)
        return accs

    def _get_costing_method_for_stock_acc(self, stock_val_acc):
        """
        Return the costing method ('standard', 'average', 'fifo') for the
        product category that uses *stock_val_acc* as its valuation account.
        Returns False if no matching product is found on this invoice.
        """
        for line in self.invoice_line_ids:
            product = line.product_id
            if not product:
                continue
            categ = product.categ_id
            if categ.property_stock_valuation_account_id == stock_val_acc:
                return categ.property_cost_method
        return False

    def _get_default_stock_val_account(self):
        return self.company_id.get_default_stock_val_account()

    def _get_change_account(self):
        """
        Return the stock interim/variation account for this invoice/bill.
        Priority:
          1. Product template  property_account_expense_id / account_expense_id
          2. Product category  account_stock_variation_id
          3. Product category  property_account_expense_id
          4. Product category  property_account_expense_categ_id
          5. Company-level     stock_change_in_stock_account_id (Settings)
          6. General Settings → Product Accounts → Expense Account
        """
        # 1. Product template
        for line in self.invoice_line_ids:
            if line.product_id:
                tmpl = line.product_id.product_tmpl_id
                for fname in ('property_account_expense_id', 'account_expense_id'):
                    acc = getattr(tmpl, fname, False)
                    if acc:
                        return acc
        # 2-4. Product category
        for line in self.invoice_line_ids:
            if line.product_id:
                categ = line.product_id.categ_id
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

    def _split_invoice_price_diff(self, stock_val_acc, total_diff):
        """
        For AVCO: split the invoice price difference proportionally between
        stock revaluation (units still in stock) and expense (units sold).

        Returns (stock_portion, expense_portion).
        Both values carry the same sign as total_diff.
        """
        qty_invoiced = 0.0
        qty_in_stock = 0.0

        for line in self.invoice_line_ids:
            product = line.product_id
            if not product:
                continue
            categ = product.categ_id
            if categ.property_stock_valuation_account_id != stock_val_acc:
                continue
            qty_invoiced += line.quantity
            qty_in_stock += product.qty_available

        if not qty_invoiced or qty_in_stock <= 0:
            return (0.0, total_diff)

        if qty_in_stock >= qty_invoiced:
            return (total_diff, 0.0)

        stock_ratio = qty_in_stock / qty_invoiced
        stock_portion = total_diff * stock_ratio
        return (stock_portion, total_diff - stock_portion)

    def _get_price_diff_account_for_stock_acc(self, stock_val_acc):
        """
        Return the account for posting price differences.

        Priority:
          1. property_price_difference_account_id on product category (Standard Price)
          2. property_account_creditor_price_difference / debtor (Standard Price)
          3. account_stock_variation_id on product category
          4. (reserved)
          5. Expense account on the product template
          6. Expense account on the product category
          7. Company-level expense / price-difference account

        For AVCO / FIFO the Standard Price accounts are absent so the lookup
        falls naturally through to the generic expense account (steps 3-5).
        """
        is_purchase = self.move_type in ('in_invoice', 'in_refund')

        # Step 1 — property_price_difference_account_id (Standard Price only).
        # Must run BEFORE the stock_val_acc filter below because Standard Price
        # products may use the default stock account (not the category-specific
        # one), causing the filter to skip them entirely.
        for line in self.invoice_line_ids:
            product = line.product_id
            if not product:
                continue
            categ = product.categ_id
            if categ.property_cost_method != 'standard':
                continue
            acc = getattr(categ, 'property_price_difference_account_id', False)
            if acc:
                return acc

        for line in self.invoice_line_ids:
            product = line.product_id
            if not product:
                continue
            categ = product.categ_id
            if categ.property_stock_valuation_account_id != stock_val_acc:
                continue

            # Step 2 — Odoo-standard price-diff account (Standard Price)
            if categ.property_cost_method == 'standard':
                fname = (
                    'property_account_creditor_price_difference'
                    if is_purchase
                    else 'property_account_debtor_price_difference'
                )
                acc = getattr(categ, fname, False)
                if acc:
                    return acc

            # Step 3 — account_stock_variation_id on product category
            acc = getattr(categ, 'account_stock_variation_id', False)
            if acc:
                return acc

            # Step 5 — Expense account on the product template
            tmpl = product.product_tmpl_id
            for fname in ('property_account_expense_id', 'account_expense_id'):
                acc = getattr(tmpl, fname, False)
                if acc:
                    return acc

            # Step 6 — Expense account on the product category
            for fname in ('property_account_expense_categ_id', 'account_expense_categ_id'):
                acc = getattr(categ, fname, False)
                if acc:
                    return acc

        # Step 7 — Company-level fallback
        company = self.company_id
        for fname in ('account_expense_id', 'account_expense_categ_id',
                      'stock_price_diff_account_id'):
            acc = getattr(company, fname, False)
            if acc:
                return acc
        return False

    def _remap_kit_cogs_accounts(self):
        """
        After a customer invoice is posted, remap COGS credit lines from the
        Kit product's stock account to the component products' stock account.
        Uses direct SQL to bypass the ORM lock on posted moves.
        """
        BOM = self.env.get('mrp.bom')
        if BOM is None:
            return

        change_acc = self._get_change_account()
        remap = {}

        for inv_line in self.invoice_line_ids:
            product = inv_line.product_id
            if not product:
                continue
            bom = BOM.search([
                ('type', '=', 'phantom'),
                '|',
                ('product_id', '=', product.id),
                ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ], limit=1)
            if not bom:
                continue

            categ = product.categ_id
            kit_accs = set()
            for fname in (
                'property_stock_valuation_account_id',
                'property_stock_account_output_categ_id',
                'property_stock_account_input_categ_id',
            ):
                acc = getattr(categ, fname, False)
                if acc and acc != change_acc:
                    kit_accs.add(acc)

            if not kit_accs:
                continue

            comp_accounts = {}
            for bom_line in bom.bom_line_ids:
                comp = bom_line.product_id
                if not comp:
                    continue
                c = comp.categ_id
                if c.property_valuation != 'real_time':
                    continue
                acc = c.property_stock_valuation_account_id
                if not acc or acc == change_acc:
                    continue
                weight = abs(comp.standard_price) * bom_line.product_qty
                comp_accounts[acc] = comp_accounts.get(acc, 0.0) + weight

            if not comp_accounts:
                continue

            target_acc = max(comp_accounts, key=lambda a: comp_accounts[a])
            for kit_acc in kit_accs:
                if target_acc.id != kit_acc.id:
                    remap[kit_acc.id] = target_acc

        if not remap:
            return

        self.env.flush_all()

        remapped_ids = []
        for line in self.line_ids:
            target_acc = remap.get(line.account_id.id)
            if not target_acc:
                continue
            self.env.cr.execute(
                'UPDATE account_move_line SET account_id = %s WHERE id = %s',
                (target_acc.id, line.id),
            )
            remapped_ids.append(line.id)

        if remapped_ids:
            self.env['account.move.line'].invalidate_model(['account_id'])
            self.invalidate_recordset(['line_ids'])
            _logger.info(
                'Auto Stock Variation Closure: remapped %d COGS lines for Kit invoice %s',
                len(remapped_ids), self.name,
            )

    def _remap_kit_cogs_debit_accounts(self):
        """
        After a customer invoice is posted, remap COGS debit lines from
        component expense accounts to the Kit product's expense account.

        For phantom-BoM (Kit) products, Odoo expands the kit into components
        when computing COGS lines.  The resulting debit lines use each
        component's own expense account instead of the kit's expense account.
        This method corrects that:

            Dr. Component expense  →  Dr. Kit expense
            Cr. unchanged  (already handled by _remap_kit_cogs_accounts)

        Uses direct SQL to bypass the ORM lock on posted moves.
        """
        BOM = self.env.get('mrp.bom')
        if BOM is None:
            return

        Picking = self.env['stock.picking']
        remap = {}  # {component_expense_acc_id: kit_expense_acc}

        for inv_line in self.invoice_line_ids:
            product = inv_line.product_id
            if not product:
                continue
            bom = BOM.search([
                ('type', '=', 'phantom'),
                '|',
                ('product_id', '=', product.id),
                ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ], limit=1)
            if not bom:
                continue

            kit_expense_acc = Picking._get_cogs_account_for_product(product, self.company_id)
            if not kit_expense_acc:
                continue

            for bom_line in bom.bom_line_ids:
                comp = bom_line.product_id
                if not comp:
                    continue
                comp_expense_acc = Picking._get_cogs_account_for_product(comp, self.company_id)
                if comp_expense_acc and comp_expense_acc.id != kit_expense_acc.id:
                    remap[comp_expense_acc.id] = kit_expense_acc

        if not remap:
            return

        self.env.flush_all()

        remapped_ids = []
        for line in self.line_ids:
            if line.debit <= 0:
                continue
            target_acc = remap.get(line.account_id.id)
            if not target_acc:
                continue
            self.env.cr.execute(
                'UPDATE account_move_line SET account_id = %s WHERE id = %s',
                (target_acc.id, line.id),
            )
            remapped_ids.append(line.id)

        if remapped_ids:
            self.env['account.move.line'].invalidate_model(['account_id'])
            self.invalidate_recordset(['line_ids'])
            _logger.info(
                'Auto Stock Variation Closure: remapped %d Kit COGS debit lines '
                'for invoice %s',
                len(remapped_ids), self.name,
            )

    def _remap_kit_cogs_debit_from_pos_session(self):
        """
        For POS session closing journal entries (move_type='entry'), remap
        COGS debit lines that use a component's expense account to use the
        Kit product's expense account instead.

        The link used is pos.session.move_id → pos.order.lines → kit product.
        For each kit product sold in the session, all component expense accounts
        are mapped to the kit's expense account, and matching debit lines in
        this entry are updated via direct SQL.

            Dr. Component expense  →  Dr. Kit expense
            Cr. unchanged
        """
        PosSession = self.env.get('pos.session')
        if PosSession is None:
            return

        BOM = self.env.get('mrp.bom')
        if BOM is None:
            return

        session = PosSession.search([('move_id', '=', self.id)], limit=1)
        if not session:
            return

        Picking = self.env['stock.picking']
        remap = {}  # {component_expense_acc_id: kit_expense_acc}

        for order in session.order_ids:
            for line in order.lines:
                product = line.product_id
                if not product:
                    continue
                bom = BOM.search([
                    ('type', '=', 'phantom'),
                    '|',
                    ('product_id', '=', product.id),
                    ('product_tmpl_id', '=', product.product_tmpl_id.id),
                ], limit=1)
                if not bom:
                    continue
                kit_expense_acc = Picking._get_cogs_account_for_product(
                    product, self.company_id
                )
                if not kit_expense_acc:
                    continue
                for bom_line in bom.bom_line_ids:
                    comp = bom_line.product_id
                    if not comp:
                        continue
                    comp_expense_acc = Picking._get_cogs_account_for_product(
                        comp, self.company_id
                    )
                    if comp_expense_acc and comp_expense_acc.id != kit_expense_acc.id:
                        remap[comp_expense_acc.id] = kit_expense_acc

        if not remap:
            return

        self.env.flush_all()

        remapped_ids = []
        for line in self.line_ids:
            if line.debit <= 0:
                continue
            target_acc = remap.get(line.account_id.id)
            if not target_acc:
                continue
            self.env.cr.execute(
                'UPDATE account_move_line SET account_id = %s WHERE id = %s',
                (target_acc.id, line.id),
            )
            remapped_ids.append(line.id)

        if remapped_ids:
            self.env['account.move.line'].invalidate_model(['account_id'])
            self.invalidate_recordset(['line_ids'])
            _logger.info(
                'Auto Stock Variation Closure: remapped %d Kit COGS debit lines '
                'in POS session entry %s',
                len(remapped_ids), self.name,
            )

    def _get_provisional_pickings(self):
        """
        Return the stock.picking records that have a provisional entry
        related to this invoice/bill.  All strategies always run so that
        a non-empty Strategy-1 result does not block Strategy-2/3 picks
        needed after the expected_code filter.

        1. SVL.account_move_id links (Odoo 19 standard)
        2. Purchase order → picking_ids  (in_invoice / in_refund return)
        3. Sale order → picking_ids      (out_invoice / out_refund)
        """
        candidates = self.env['stock.picking'].browse()

        # Strategy 1: via Stock Valuation Layer
        SVL = self.env.get('stock.valuation.layer')
        if SVL is not None:
            layers = SVL.search([('account_move_id', '=', self.id)])
            candidates |= layers.mapped('stock_move_id.picking_id')

        # Strategy 2: via purchase order lines (always runs)
        for line in self.invoice_line_ids:
            pl = getattr(line, 'purchase_line_id', None)
            if pl:
                po = getattr(pl, 'order_id', None)
                if po:
                    po_picks = getattr(po, 'picking_ids', None)
                    if po_picks:
                        candidates |= po_picks

        # Strategy 3: via sale order lines (always runs)
        # Support both Odoo ≤16 (sale_line_ids Many2many) and
        # Odoo 17+ (sale_line_id Many2one) field names.
        for line in self.invoice_line_ids:
            sale_line = (
                getattr(line, 'sale_line_ids', None)
                or getattr(line, 'sale_line_id', None)
            )
            if not sale_line:
                continue
            for sl in (sale_line if hasattr(sale_line, '__iter__') else [sale_line]):
                so = getattr(sl, 'order_id', None)
                if so:
                    so_picks = getattr(so, 'picking_ids', None)
                    if so_picks:
                        candidates |= so_picks

        expected_code = {
            'in_invoice': 'incoming',
            'in_refund': 'outgoing',
            'out_invoice': 'outgoing',
            'out_refund': 'incoming',
        }.get(self.move_type)

        return candidates.filtered(
            lambda p: (
                p.auto_interim_move_id
                and p.auto_interim_move_id.state == 'posted'
                and (not expected_code or p.picking_type_id.code == expected_code)
            )
        )

    # ------------------------------------------------------------------
    # Compensation helpers
    # ------------------------------------------------------------------

    def _collect_prov_amounts(self, change_acc, pickings=None):
        """
        Collect signed balances from provisional entries of related pickings.
        Excludes the change-in-stock account (only stock accounts matter here).
        Returns {stock_account: signed_balance}.
        Positive = Dr. Stock (incoming receipt), negative = Cr. Stock (outgoing).

        When *pickings* is provided, only those pickings are considered;
        otherwise all provisional pickings for this invoice are used.
        """
        if pickings is None:
            pickings = self._get_provisional_pickings()
        prov = {}
        for picking in pickings:
            for aml in picking.auto_interim_move_id.line_ids:
                # Exclude the Change-in-Stock account — take all stock accounts.
                # For Kit products the provisional entry uses component accounts,
                # not the Kit's own stock account, so we must not filter by
                # stock_val_accs here.
                if aml.account_id == change_acc:
                    continue
                bal = aml.debit - aml.credit
                if abs(bal) > 0.01:
                    prov[aml.account_id] = prov.get(aml.account_id, 0.0) + bal
        return prov

    def _collect_inv_amounts(self, stock_val_accs):
        """
        Collect signed balances from this invoice/bill on stock valuation accounts.
        For vendor documents: all AMLs on stock accounts.
        For customer documents: only COGS lines (display_type='cogs').
        Returns {stock_account: signed_balance}.
        """
        inv = {}
        if self.move_type in ('in_invoice', 'in_refund'):
            for aml in self.line_ids:
                if aml.account_id in stock_val_accs and abs(aml.balance) > 0.01:
                    inv[aml.account_id] = inv.get(aml.account_id, 0.0) + aml.balance
        elif self.move_type in ('out_invoice', 'out_refund'):
            for aml in self.line_ids:
                if (
                    aml.display_type == 'cogs'
                    and aml.account_id in stock_val_accs
                    and abs(aml.balance) > 0.01
                ):
                    inv[aml.account_id] = inv.get(aml.account_id, 0.0) + aml.balance
        return inv

    def _build_comp_lines(self, all_accs, prov, inv, change_acc):
        """
        Build the journal line vals list for the compensation entry.
        Applies move_type-specific pairing logic for every stock account.
        Returns a list of line dicts ready for line_ids = [(0, 0, v) ...].
        """
        lines_vals = []
        for acc in all_accs:
            p = prov.get(acc, 0.0)
            i = inv.get(acc, 0.0)

            if self.move_type == 'in_refund':
                if p and i:
                    lines_vals += self._comp_pair(change_acc, acc, p, self.name)
                    abs_diff = abs(i) - abs(p)
                    if abs(abs_diff) > 0.01:
                        price_diff_acc = self._get_price_diff_account_for_stock_acc(acc)
                        if price_diff_acc:
                            lines_vals += self._comp_pair(acc, price_diff_acc, abs_diff, self.name)
                        else:
                            _logger.info(
                                'ASVC %s: no expense account found for stock acc %s — '
                                'price diff %.4f left unposted.',
                                self.name, acc.code, abs_diff,
                            )
                elif p:
                    lines_vals += self._comp_pair(change_acc, acc, p, self.name)
                elif i:
                    lines_vals += self._comp_pair(change_acc, acc, i, self.name)

            elif self.move_type == 'out_refund':
                if p:
                    lines_vals += self._comp_pair(change_acc, acc, p, self.name)
                elif i:
                    lines_vals += self._comp_pair(change_acc, acc, i, self.name)

            elif p and i:
                # For Standard Price: compensation = reversal at cost (p)
                # + a symmetric price-difference pair that closes the gap
                # between purchase price and product cost through the same
                # stock-valuation and variation accounts (in the same entry):
                #
                #   Dr. Variation  p    Cr. Stock Val  p   ← reversal
                #   Dr. Stock Val  diff  Cr. Variation  diff ← price diff
                if (
                    self.move_type == 'in_invoice'
                    and self._get_costing_method_for_stock_acc(acc) == 'standard'
                ):
                    # pair 1: reversal at cost price
                    lines_vals += self._comp_pair(change_acc, acc, p, self.name)
                    diff = i - p
                    if abs(diff) > 0.01:
                        # pair 2: price difference → property_price_difference_account_id
                        price_diff_acc = self._get_price_diff_account_for_stock_acc(acc)
                        if price_diff_acc:
                            # diff > 0 (purchase > cost): Dr. price_diff / Cr. stock
                            # diff < 0 (purchase < cost): Dr. stock  / Cr. price_diff
                            lines_vals += self._comp_pair(price_diff_acc, acc, diff, self.name)
                        else:
                            lines_vals += self._comp_pair(change_acc, acc, diff, self.name)
                else:
                    lines_vals += self._comp_pair(change_acc, acc, i, self.name)
                    diff = i - p
                    if abs(diff) > 0.01:
                        if self.move_type == 'in_invoice':
                            stock_portion, expense_portion = self._split_invoice_price_diff(acc, diff)
                            if abs(stock_portion) > 0.01:
                                lines_vals += self._comp_pair(acc, change_acc, stock_portion, self.name)
                            if abs(expense_portion) > 0.01:
                                price_diff_acc = self._get_price_diff_account_for_stock_acc(acc)
                                if price_diff_acc:
                                    lines_vals += self._comp_pair(price_diff_acc, change_acc, expense_portion, self.name)
                                else:
                                    lines_vals += self._comp_pair(acc, change_acc, expense_portion, self.name)
                                    _logger.info(
                                        'ASVC %s: no expense account for stock acc %s — '
                                        'expense portion %.4f posted to Stock.',
                                        self.name, acc.code, expense_portion,
                                    )
                        elif self.move_type != 'out_invoice':
                            lines_vals += self._comp_pair(acc, change_acc, diff, self.name)

            elif p:
                lines_vals += self._comp_pair(change_acc, acc, p, self.name)
                # Standard Price: if the bill has no stock-val line (Odoo
                # books through Stock Input instead), compute the purchase
                # amount from the invoice product lines to close the diff.
                if (
                    self.move_type == 'in_invoice'
                    and self._get_costing_method_for_stock_acc(acc) == 'standard'
                ):
                    # Bill has no stock-val AML (Odoo routes via Stock Input).
                    # Compute purchase amount from invoice product lines.
                    inv_line_total = sum(
                        abs(line.balance)
                        for line in self.invoice_line_ids
                        if line.product_id
                        and line.product_id.categ_id.property_stock_valuation_account_id == acc
                        and abs(line.balance) > 0.01
                    )
                    diff = inv_line_total - p
                    if abs(diff) > 0.01:
                        # Same direction as the reversal: Dr. Variation / Cr. Stock
                        lines_vals += self._comp_pair(change_acc, acc, diff, self.name)

            elif i:
                lines_vals += self._comp_pair(change_acc, acc, i, self.name)

        return lines_vals

    # ------------------------------------------------------------------
    # Compensation entry
    # ------------------------------------------------------------------

    def _create_interim_compensation(self):
        """
        Creates a compensation entry when a vendor bill or customer invoice
        is posted.  The entry exactly reverses the provisional and includes
        a price-difference adjustment so that Stock Variation is always zero.

        Example — purchase receipt 100, billed at 170:

          Provisional (receipt):   Dr. Stock 100  /  Cr. Change 100
          Compensation (here):     Dr. Change 170 /  Cr. Stock 170   ← main reversal
                                   Dr. Stock 70   /  Cr. Change 70   ← price diff (+70)
          Odoo bill entry:         Dr. Stock 170  /  Cr. AP 170

          Net: Stock = 100 + 170 - 170 + 70 = 170 ✓  |  Change = 0 ✓

        The same logic applies to sales (out_invoice) and to negative
        differences (invoice < provisional).
        """
        existing = self.stock_interim_comp_move_id
        if existing and existing.state != 'cancel':
            return

        change_acc = self._get_change_account()
        if not change_acc:
            return

        stock_val_accs = self._get_stock_val_accounts()
        if not stock_val_accs:
            return

        all_provisional = self._get_provisional_pickings()

        # Split provisional pickings into standard and expense-based
        expense_pickings = all_provisional.filtered(
            lambda p: p.auto_interim_move_id.auto_interim_is_expense_based
        )
        standard_pickings = all_provisional - expense_pickings

        lines_vals = []

        # ── Standard compensation (existing logic) ──────────────────────
        if standard_pickings:
            prov = self._collect_prov_amounts(change_acc, pickings=standard_pickings)
            inv = self._collect_inv_amounts(stock_val_accs)
            all_accs = set(prov) | set(inv)
            if all_accs:
                lines_vals += self._build_comp_lines(all_accs, prov, inv, change_acc)

        # ── Expense-based: direct reversal of provisional entries ────────
        # The provisional entry (Dr. Expense / Cr. Stock Val) is simply
        # reversed here. Odoo's own COGS entry on the invoice handles the
        # final cost recognition — no price-diff logic needed.
        for picking in expense_pickings:
            for aml in picking.auto_interim_move_id.line_ids:
                if aml.debit > 0.01:
                    lines_vals.append({
                        'account_id': aml.account_id.id,
                        'debit': 0.0,
                        'credit': aml.debit,
                        'name': self.name,
                    })
                elif aml.credit > 0.01:
                    lines_vals.append({
                        'account_id': aml.account_id.id,
                        'debit': aml.credit,
                        'credit': 0.0,
                        'name': self.name,
                    })

        if not lines_vals:
            return

        journal = self._get_interim_journal()
        if not journal:
            _logger.warning(
                'Auto Stock Variation Closure: no journal found for '
                'compensation entry on %s — skipped.', self.name,
            )
            return

        comp_move = (
            self.env['account.move']
            .with_context(skip_stock_interim_comp=True)
            .with_company(self.company_id)
            .create({
                'company_id': self.company_id.id,
                'journal_id': journal.id,
                'date': self.invoice_date or fields.Date.context_today(self),
                'ref': 'Interim Comp.: %s' % self.name,
                'move_type': 'entry',
                'line_ids': [(0, 0, v) for v in lines_vals],
            })
        )
        comp_move.with_context(skip_stock_interim_comp=True).action_post()
        self.stock_interim_comp_move_id = comp_move.id

    @staticmethod
    def _comp_pair(dr_acc, cr_acc, balance, name):
        """Return a [debit, credit] line pair. Swaps accounts when balance < 0."""
        amount = abs(balance)
        if balance >= 0:
            return [
                {'account_id': dr_acc.id, 'debit': amount, 'credit': 0.0, 'name': name},
                {'account_id': cr_acc.id, 'debit': 0.0, 'credit': amount, 'name': name},
            ]
        return [
            {'account_id': cr_acc.id, 'debit': amount, 'credit': 0.0, 'name': name},
            {'account_id': dr_acc.id, 'debit': 0.0, 'credit': amount, 'name': name},
        ]
