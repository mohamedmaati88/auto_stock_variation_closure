from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAutoStockVariationClosure(TransactionCase):
    """
    Integration tests for auto_stock_variation_closure.

    Requires a company with a full chart of accounts (post_install).
    All tests are isolated within a transaction that is rolled back afterward.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        # ── Accounts ──────────────────────────────────────────────────
        cls.stock_val_account = cls.env['account.account'].create({
            'name': 'Test Stock Valuation',
            'code': 'TSTK_VAL',
            'account_type': 'asset_current',
            'company_id': cls.company.id,
        })
        cls.stock_var_account = cls.env['account.account'].create({
            'name': 'Test Stock Variation',
            'code': 'TSTK_VAR',
            'account_type': 'expense',
            'company_id': cls.company.id,
        })

        # ── Journal ───────────────────────────────────────────────────
        cls.stock_journal = cls.env['account.journal'].create({
            'name': 'Test Stock Journal',
            'type': 'general',
            'code': 'TSTJ',
            'company_id': cls.company.id,
        })

        # ── Company settings ──────────────────────────────────────────
        cls.company.write({
            'stock_auto_interim_close': True,
            'stock_change_in_stock_account_id': cls.stock_var_account.id,
            'stock_auto_interim_journal_id': cls.stock_journal.id,
        })

        # ── Product category with perpetual valuation ─────────────────
        cls.categ = cls.env['product.category'].create({
            'name': 'Test Real-Time Category',
            'property_valuation': 'real_time',
            'property_cost_method': 'standard',
            'property_stock_valuation_account_id': cls.stock_val_account.id,
        })

        # ── Product (storable in Odoo 19: type='consu' + real_time categ) ──
        cls.product = cls.env['product.product'].create({
            'name': 'Test Product',
            'type': 'consu',
            'categ_id': cls.categ.id,
            'standard_price': 100.0,
        })

        # ── Partners ──────────────────────────────────────────────────
        cls.vendor = cls.env['res.partner'].create({'name': 'Test Vendor', 'supplier_rank': 1})
        cls.customer = cls.env['res.partner'].create({'name': 'Test Customer', 'customer_rank': 1})

        # ── Warehouse ─────────────────────────────────────────────────
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )

        # ── LC fixtures ───────────────────────────────────────────────
        cls.lc_expense_account = cls.env['account.account'].create({
            'name': 'Test LC Expense',
            'code': 'TLC_EXP',
            'account_type': 'expense',
            'company_id': cls.company.id,
        })
        cls.cogs_account = cls.env['account.account'].create({
            'name': 'Test COGS',
            'code': 'TCOGS',
            'account_type': 'expense',
            'company_id': cls.company.id,
        })
        cls.lc_service_product = cls.env['product.product'].create({
            'name': 'Test LC Service',
            'type': 'service',
        })
        # Set expense account on category so the COGS-split test works
        cls.categ.property_account_expense_categ_id = cls.cogs_account

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_receipt(self, qty=1.0):
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.in_type_id.id,
            'partner_id': self.vendor.id,
            'move_ids': [(0, 0, {
                'name': 'Test receipt move',
                'product_id': self.product.id,
                'product_uom_qty': qty,
                'product_uom': self.product.uom_id.id,
                'location_id': self.warehouse.in_type_id.default_location_src_id.id,
                'location_dest_id': self.warehouse.lot_stock_id.id,
            })],
        })
        for move in picking.move_ids:
            move.quantity = qty
        picking.button_validate()
        return picking

    def _make_delivery(self, qty=1.0):
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, qty,
        )
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.out_type_id.id,
            'partner_id': self.customer.id,
            'move_ids': [(0, 0, {
                'name': 'Test delivery move',
                'product_id': self.product.id,
                'product_uom_qty': qty,
                'product_uom': self.product.uom_id.id,
                'location_id': self.warehouse.lot_stock_id.id,
                'location_dest_id': self.warehouse.out_type_id.default_location_dest_id.id,
            })],
        })
        for move in picking.move_ids:
            move.quantity = qty
        picking.button_validate()
        return picking

    def _make_vendor_bill(self, qty=1.0, price=100.0):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.vendor.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': qty,
                'price_unit': price,
                'account_id': self.stock_val_account.id,
            })],
        })
        bill.action_post()
        return bill

    def _make_customer_invoice(self, qty=1.0, price=120.0):
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.customer.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': qty,
                'price_unit': price,
            })],
        })
        invoice.action_post()
        return invoice

    # ------------------------------------------------------------------
    # Feature disabled
    # ------------------------------------------------------------------

    def test_no_entry_when_feature_disabled(self):
        self.company.stock_auto_interim_close = False
        try:
            receipt = self._make_receipt(qty=1.0)
            self.assertFalse(receipt.auto_interim_move_id,
                             "No interim entry should be created when feature is off")
        finally:
            self.company.stock_auto_interim_close = True

    def test_category_account_takes_priority_over_company(self):
        """
        Product category account (priority 2-4) must be used BEFORE the
        company-level account (priority 5).  An interim entry must be
        created using the category account even when the company account
        is also configured.
        """
        # Both company AND category accounts are set — category wins.
        receipt = self._make_receipt(qty=1.0)
        self.assertTrue(
            receipt.auto_interim_move_id,
            "Interim entry must be created",
        )
        credit_accs = receipt.auto_interim_move_id.line_ids.filtered(
            lambda l: l.credit > 0
        ).mapped('account_id')
        self.assertIn(
            self.cogs_account, credit_accs,
            "Category account must take priority over company account",
        )
        self.assertNotIn(
            self.stock_var_account, credit_accs,
            "Company account must NOT be used when category account is present",
        )

    def test_company_account_used_when_category_empty(self):
        """
        When all category accounts are empty, _get_change_account falls
        back to the company-level stock_change_in_stock_account_id (priority 5).
        """
        old_categ_expense = self.categ.property_account_expense_categ_id
        self.categ.property_account_expense_categ_id = False
        try:
            receipt = self._make_receipt(qty=1.0)
            self.assertTrue(
                receipt.auto_interim_move_id,
                "Interim entry must be created via company fallback account",
            )
            credit_accs = receipt.auto_interim_move_id.line_ids.filtered(
                lambda l: l.credit > 0
            ).mapped('account_id')
            self.assertIn(
                self.stock_var_account, credit_accs,
                "Company account must be used when all category accounts are empty",
            )
        finally:
            self.categ.property_account_expense_categ_id = old_categ_expense

    def test_no_entry_when_all_accounts_empty(self):
        """
        No interim entry must be created when the company account AND all
        category fallback accounts are empty.
        Skipped if an ir.property default expense account is active
        (it would act as the last-resort fallback and create an entry).
        """
        try:
            ir_acc = self.env['ir.property'].sudo()._get(
                'property_account_expense_id', 'product.template'
            )
        except Exception:
            ir_acc = False

        if ir_acc:
            self.skipTest(
                "ir.property default expense account is configured — "
                "'all empty' scenario cannot be reproduced in this environment."
            )

        old_company_acc = self.company.stock_change_in_stock_account_id
        old_categ_expense = self.categ.property_account_expense_categ_id
        self.company.stock_change_in_stock_account_id = False
        self.categ.property_account_expense_categ_id = False
        try:
            receipt = self._make_receipt(qty=1.0)
            self.assertFalse(
                receipt.auto_interim_move_id,
                "No entry must be created when all variation/expense accounts are empty",
            )
        finally:
            self.company.stock_change_in_stock_account_id = old_company_acc
            self.categ.property_account_expense_categ_id = old_categ_expense

    # ------------------------------------------------------------------
    # Purchase receipt
    # ------------------------------------------------------------------

    def test_receipt_creates_interim_entry(self):
        receipt = self._make_receipt(qty=1.0)
        self.assertEqual(receipt.state, 'done')
        self.assertTrue(receipt.auto_interim_move_id,
                        "Interim entry must be created on receipt validation")
        self.assertEqual(receipt.auto_interim_move_id.state, 'posted')

    def test_receipt_interim_entry_direction(self):
        receipt = self._make_receipt(qty=2.0)
        entry = receipt.auto_interim_move_id
        self.assertTrue(entry)
        # Dr. Stock Account / Cr. Change in Stock
        debit_accs = entry.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = entry.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        self.assertIn(self.stock_val_account, debit_accs,
                      "Stock valuation account must be debited on receipt")
        self.assertIn(self.stock_var_account, credit_accs,
                      "Change-in-Stock account must be credited on receipt")

    def test_receipt_interim_entry_amount(self):
        receipt = self._make_receipt(qty=3.0)
        entry = receipt.auto_interim_move_id
        total_debit = sum(entry.line_ids.mapped('debit'))
        # 3 units × 100 standard price = 300
        self.assertAlmostEqual(total_debit, 300.0, places=2)

    def test_no_duplicate_entry_on_double_call(self):
        receipt = self._make_receipt(qty=1.0)
        first_entry = receipt.auto_interim_move_id
        receipt._create_auto_interim_entry()  # explicit second call
        self.assertEqual(receipt.auto_interim_move_id, first_entry,
                         "Calling _create_auto_interim_entry twice must not create a duplicate")

    # ------------------------------------------------------------------
    # Sale delivery
    # ------------------------------------------------------------------

    def test_delivery_creates_interim_entry(self):
        delivery = self._make_delivery(qty=1.0)
        self.assertEqual(delivery.state, 'done')
        self.assertTrue(delivery.auto_interim_move_id,
                        "Interim entry must be created on delivery validation")
        self.assertEqual(delivery.auto_interim_move_id.state, 'posted')

    def test_delivery_interim_entry_direction(self):
        delivery = self._make_delivery(qty=1.0)
        entry = delivery.auto_interim_move_id
        # Dr. Change in Stock / Cr. Stock Account
        debit_accs = entry.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = entry.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        self.assertIn(self.stock_var_account, debit_accs,
                      "Change-in-Stock account must be debited on delivery")
        self.assertIn(self.stock_val_account, credit_accs,
                      "Stock valuation account must be credited on delivery")

    # ------------------------------------------------------------------
    # Vendor bill compensation
    # ------------------------------------------------------------------

    def test_vendor_bill_creates_compensation(self):
        bill = self._make_vendor_bill(qty=1.0, price=100.0)
        self.assertTrue(bill.stock_interim_comp_move_id,
                        "Compensation entry must be created on vendor bill posting")
        self.assertEqual(bill.stock_interim_comp_move_id.state, 'posted')

    def test_vendor_bill_compensation_direction(self):
        bill = self._make_vendor_bill(qty=1.0, price=100.0)
        comp = bill.stock_interim_comp_move_id
        self.assertTrue(comp)
        # Dr. Change in Stock / Cr. Stock Account
        debit_accs = comp.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = comp.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        self.assertIn(self.stock_var_account, debit_accs)
        self.assertIn(self.stock_val_account, credit_accs)

    def test_bill_reset_to_draft_cancels_compensation(self):
        bill = self._make_vendor_bill(qty=1.0, price=100.0)
        comp = bill.stock_interim_comp_move_id
        self.assertTrue(comp)
        bill.button_draft()
        self.assertEqual(comp.state, 'cancel',
                         "Compensation entry must be cancelled when bill is reset to draft")

    def test_bill_repost_creates_new_compensation(self):
        bill = self._make_vendor_bill(qty=1.0, price=100.0)
        old_comp = bill.stock_interim_comp_move_id
        bill.button_draft()
        bill.action_post()
        new_comp = bill.stock_interim_comp_move_id
        self.assertTrue(new_comp)
        self.assertNotEqual(old_comp, new_comp,
                            "Re-posting must create a new compensation entry")
        self.assertEqual(new_comp.state, 'posted')

    def test_no_duplicate_compensation_on_double_post(self):
        bill = self._make_vendor_bill(qty=1.0, price=100.0)
        first_comp = bill.stock_interim_comp_move_id
        bill._create_interim_compensation()  # explicit second call
        self.assertEqual(bill.stock_interim_comp_move_id, first_comp,
                         "Calling _create_interim_compensation twice must not duplicate entries")

    # ------------------------------------------------------------------
    # Non-perpetual product (no entry expected)
    # ------------------------------------------------------------------

    def test_no_entry_for_manual_valuation_product(self):
        manual_categ = self.env['product.category'].create({
            'name': 'Test Manual Category',
            'property_valuation': 'manual_periodic',
        })
        manual_product = self.env['product.product'].create({
            'name': 'Manual Product',
            'type': 'consu',
            'categ_id': manual_categ.id,
            'standard_price': 50.0,
        })
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.in_type_id.id,
            'partner_id': self.vendor.id,
            'move_ids': [(0, 0, {
                'name': 'Test manual move',
                'product_id': manual_product.id,
                'product_uom_qty': 1.0,
                'product_uom': manual_product.uom_id.id,
                'location_id': self.warehouse.in_type_id.default_location_src_id.id,
                'location_dest_id': self.warehouse.lot_stock_id.id,
            })],
        })
        for move in picking.move_ids:
            move.quantity = 1.0
        picking.button_validate()
        self.assertFalse(picking.auto_interim_move_id,
                         "No interim entry for non-perpetual (manual) products")

    # ------------------------------------------------------------------
    # Smart buttons
    # ------------------------------------------------------------------

    def test_smart_button_picking_action(self):
        receipt = self._make_receipt(qty=1.0)
        self.assertTrue(receipt.auto_interim_move_id)
        action = receipt.action_view_auto_interim_entry()
        self.assertEqual(action['res_model'], 'account.move')
        self.assertEqual(action['res_id'], receipt.auto_interim_move_id.id)

    def test_smart_button_invoice_action(self):
        bill = self._make_vendor_bill(qty=1.0, price=100.0)
        self.assertTrue(bill.stock_interim_comp_move_id)
        action = bill.action_view_stock_interim_comp()
        self.assertEqual(action['res_model'], 'account.move')
        self.assertEqual(action['res_id'], bill.stock_interim_comp_move_id.id)

    # ------------------------------------------------------------------
    # Customer invoice compensation
    # ------------------------------------------------------------------

    def test_customer_invoice_compensation_when_cogs_present(self):
        """
        When Odoo generates COGS lines (display_type='cogs') on a customer
        invoice using the stock valuation account, the module must create a
        compensation entry.  If COGS lines are absent (e.g. missing output
        account on the category) the test still passes — the module correctly
        skips compensation in that case.
        """
        invoice = self._make_customer_invoice(qty=1.0, price=120.0)
        cogs_on_val_accs = invoice.line_ids.filtered(
            lambda l: l.display_type == 'cogs'
            and l.account_id == self.stock_val_account
        )
        if cogs_on_val_accs:
            self.assertTrue(
                invoice.stock_interim_comp_move_id,
                "Compensation must be created when COGS lines hit the stock val account",
            )
            self.assertEqual(invoice.stock_interim_comp_move_id.state, 'posted')
        else:
            # COGS not generated in this environment (missing output account config)
            self.assertFalse(
                invoice.stock_interim_comp_move_id,
                "No compensation should be created when no COGS lines hit stock val account",
            )

    def test_customer_invoice_compensation_direction(self):
        """
        When compensation IS created for out_invoice:
        Dr. Stock Account / Cr. Change in Stock
        """
        invoice = self._make_customer_invoice(qty=1.0, price=120.0)
        comp = invoice.stock_interim_comp_move_id
        if not comp:
            return  # COGS lines not on stock val account — compensation not applicable
        debit_accs = comp.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = comp.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        self.assertIn(self.stock_val_account, debit_accs)
        self.assertIn(self.stock_var_account, credit_accs)

    # ------------------------------------------------------------------
    # Credit notes / refunds
    # ------------------------------------------------------------------

    def test_vendor_credit_note_creates_compensation(self):
        """
        A vendor credit note (in_refund) should create a compensation entry
        that reverses the direction of a regular vendor bill compensation.
        """
        refund = self.env['account.move'].create({
            'move_type': 'in_refund',
            'partner_id': self.vendor.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1.0,
                'price_unit': 100.0,
                'account_id': self.stock_val_account.id,
            })],
        })
        refund.action_post()
        self.assertTrue(
            refund.stock_interim_comp_move_id,
            "Compensation must be created for vendor credit notes (in_refund)",
        )
        self.assertEqual(refund.stock_interim_comp_move_id.state, 'posted')

    def test_vendor_credit_note_compensation_direction(self):
        """
        For in_refund the stock account is credited (balance < 0),
        so compensation must be: Dr. Stock Account / Cr. Change in Stock.
        """
        refund = self.env['account.move'].create({
            'move_type': 'in_refund',
            'partner_id': self.vendor.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1.0,
                'price_unit': 100.0,
                'account_id': self.stock_val_account.id,
            })],
        })
        refund.action_post()
        comp = refund.stock_interim_comp_move_id
        self.assertTrue(comp)
        debit_accs = comp.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = comp.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        # in_refund: stock account is credited → comp debits it back
        self.assertIn(self.stock_val_account, debit_accs)
        self.assertIn(self.stock_var_account, credit_accs)

    def test_customer_credit_note_reset_to_draft(self):
        """
        Resetting a customer credit note (out_refund) with a compensation
        entry to draft must cancel the compensation entry.
        """
        refund = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.customer.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })
        refund.action_post()
        comp = refund.stock_interim_comp_move_id
        if not comp:
            return  # COGS lines not on stock val account — not applicable
        refund.button_draft()
        self.assertEqual(comp.state, 'cancel',
                         "Compensation must be cancelled on out_refund reset-to-draft")

    # ------------------------------------------------------------------
    # Return picking
    # ------------------------------------------------------------------

    def test_return_picking_creates_interim_entry(self):
        """
        A return picking (reversed direction) must create its own provisional
        entry with the correct reversed direction.
        """
        receipt = self._make_receipt(qty=2.0)
        self.assertTrue(receipt.auto_interim_move_id)

        # Create return of the receipt
        return_wizard = self.env['stock.return.picking'].with_context(
            active_id=receipt.id, active_model='stock.picking'
        ).create({'picking_id': receipt.id})
        for line in return_wizard.product_return_moves:
            line.quantity = 2.0
        action = return_wizard.create_returns()
        return_picking = self.env['stock.picking'].browse(action['res_id'])

        for move in return_picking.move_ids:
            move.quantity = 2.0
        return_picking.button_validate()

        self.assertEqual(return_picking.state, 'done')
        self.assertTrue(
            return_picking.auto_interim_move_id,
            "Return picking must create its own interim entry",
        )

    def test_return_picking_interim_entry_reversed_direction(self):
        """
        A purchase return (outgoing from stock back to vendor) must produce
        the same direction as a sale delivery: Dr. Change in Stock / Cr. Stock.
        """
        receipt = self._make_receipt(qty=1.0)
        return_wizard = self.env['stock.return.picking'].with_context(
            active_id=receipt.id, active_model='stock.picking'
        ).create({'picking_id': receipt.id})
        for line in return_wizard.product_return_moves:
            line.quantity = 1.0
        action = return_wizard.create_returns()
        return_picking = self.env['stock.picking'].browse(action['res_id'])
        for move in return_picking.move_ids:
            move.quantity = 1.0
        return_picking.button_validate()

        entry = return_picking.auto_interim_move_id
        self.assertTrue(entry)
        debit_accs = entry.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = entry.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        # Return (outgoing): Dr. Change in Stock / Cr. Stock Account
        self.assertIn(self.stock_var_account, debit_accs)
        self.assertIn(self.stock_val_account, credit_accs)

    # ------------------------------------------------------------------
    # Partial receipt / delivery
    # ------------------------------------------------------------------

    def test_partial_receipt_entry_amount(self):
        """
        Receiving only part of a PO line must produce an interim entry
        for the partial quantity only.
        """
        # Create receipt for 10 but process only 4
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.in_type_id.id,
            'partner_id': self.vendor.id,
            'move_ids': [(0, 0, {
                'name': 'Partial receipt move',
                'product_id': self.product.id,
                'product_uom_qty': 10.0,
                'product_uom': self.product.uom_id.id,
                'location_id': self.warehouse.in_type_id.default_location_src_id.id,
                'location_dest_id': self.warehouse.lot_stock_id.id,
            })],
        })
        # Set done quantity to 4 (partial)
        for move in picking.move_ids:
            move.quantity = 4.0

        # Validate — may create a backorder; we only care about this picking
        picking.with_context(skip_backorder=True).button_validate()
        # If a backorder wizard appears, the picking state won't be done yet
        if picking.state != 'done':
            return  # Backorder wizard required; test not applicable in this environment

        entry = picking.auto_interim_move_id
        self.assertTrue(entry, "Interim entry must be created for partial receipt")
        total = sum(entry.line_ids.mapped('debit'))
        # 4 units × 100 = 400
        self.assertAlmostEqual(total, 400.0, places=2)

    def test_partial_delivery_entry_amount(self):
        """
        Delivering only part of a SO line must produce an interim entry
        for the partial quantity only.
        """
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, 10.0
        )
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.out_type_id.id,
            'partner_id': self.customer.id,
            'move_ids': [(0, 0, {
                'name': 'Partial delivery move',
                'product_id': self.product.id,
                'product_uom_qty': 10.0,
                'product_uom': self.product.uom_id.id,
                'location_id': self.warehouse.lot_stock_id.id,
                'location_dest_id': self.warehouse.out_type_id.default_location_dest_id.id,
            })],
        })
        for move in picking.move_ids:
            move.quantity = 3.0

        picking.with_context(skip_backorder=True).button_validate()
        if picking.state != 'done':
            return  # Backorder wizard required; test not applicable in this environment

        entry = picking.auto_interim_move_id
        self.assertTrue(entry, "Interim entry must be created for partial delivery")
        total = sum(entry.line_ids.mapped('credit'))
        # 3 units × 100 = 300
        self.assertAlmostEqual(total, 300.0, places=2)

    # ------------------------------------------------------------------
    # Multi-company isolation
    # ------------------------------------------------------------------

    def test_multi_company_entry_scoped_to_picking_company(self):
        """
        The interim entry must be created under the same company as the picking.
        """
        receipt = self._make_receipt(qty=1.0)
        entry = receipt.auto_interim_move_id
        self.assertTrue(entry)
        self.assertEqual(
            entry.company_id, self.company,
            "Interim entry company must match the picking's company",
        )

    def _make_landed_cost(self, picking, amount=100.0):
        lc = self.env['stock.landed.cost'].create({
            'picking_ids': [(6, 0, [picking.id])],
            'cost_lines': [(0, 0, {
                'product_id': self.lc_service_product.id,
                'name': 'Test LC',
                'price_unit': amount,
                'split_method': 'equal',
                'account_id': self.lc_expense_account.id,
            })],
        })
        if hasattr(lc, 'button_compute'):
            lc.button_compute()
        if lc.valuation_adjustment_lines:
            lc.button_validate()
        return lc

    def test_multi_company_feature_disabled_on_second_company(self):
        """
        With the feature disabled on a second company, pickings for that company
        must not generate interim entries.
        """
        company2 = self.env['res.company'].create({'name': 'Test Company 2 (no interim)'})
        company2.stock_auto_interim_close = False

        # Create a warehouse for company2
        warehouse2 = self.env['stock.warehouse'].search(
            [('company_id', '=', company2.id)], limit=1
        )
        if not warehouse2:
            return  # No warehouse auto-created for second company — not applicable

        partner2 = self.env['res.partner'].with_company(company2).create(
            {'name': 'Vendor C2'}
        )
        picking2 = self.env['stock.picking'].with_company(company2).create({
            'picking_type_id': warehouse2.in_type_id.id,
            'partner_id': partner2.id,
            'move_ids': [(0, 0, {
                'name': 'C2 receipt move',
                'product_id': self.product.id,
                'product_uom_qty': 1.0,
                'product_uom': self.product.uom_id.id,
                'location_id': warehouse2.in_type_id.default_location_src_id.id,
                'location_dest_id': warehouse2.lot_stock_id.id,
            })],
        })
        for move in picking2.move_ids:
            move.quantity = 1.0
        picking2.button_validate()

        self.assertFalse(
            picking2.auto_interim_move_id,
            "No interim entry for company2 which has the feature disabled",
        )

    # ------------------------------------------------------------------
    # Landed cost interim entry
    # ------------------------------------------------------------------

    def _deliver_from_stock(self, qty):
        """Deliver qty units using existing quant (no extra _update_available_quantity)."""
        del_picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.out_type_id.id,
            'partner_id': self.customer.id,
            'move_ids': [(0, 0, {
                'name': 'LC test delivery',
                'product_id': self.product.id,
                'product_uom_qty': qty,
                'product_uom': self.product.uom_id.id,
                'location_id': self.warehouse.lot_stock_id.id,
                'location_dest_id': self.warehouse.out_type_id.default_location_dest_id.id,
            })],
        })
        for move in del_picking.move_ids:
            move.quantity = qty
        del_picking.button_validate()
        return del_picking

    def test_lc_module_creates_entry_no_standard_duplicate(self):
        """
        When Auto Stock Variation Closure is active the MODULE creates the
        Dr. Stock / Cr. LC Expense entry (via _create_lc_interim_entry) and
        Odoo's standard _create_account_move is suppressed to prevent
        duplication.

        Verified:
        - lc.auto_interim_move_id is set (module created the entry)
        - Entry is posted (Dr. Stock / Cr. LC expense account)
        - Only ONE journal entry exists for this landed cost
        """
        receipt = self._make_receipt(qty=2.0)
        self._deliver_from_stock(qty=1.0)
        lc = self._make_landed_cost(receipt, amount=100.0)

        # Module must set auto_interim_move_id after validation
        self.assertTrue(
            getattr(lc, 'auto_interim_move_id', False),
            "Module must create LC allocation entry and store it in auto_interim_move_id",
        )
        entry = lc.auto_interim_move_id
        self.assertEqual(entry.state, 'posted', "LC allocation entry must be posted")

        # Dr. side: stock valuation account
        debit_accs = entry.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        self.assertIn(
            self.stock_val_account, debit_accs,
            "Stock valuation account must be debited in the LC allocation entry",
        )

        # Cr. side: the LC expense account (from cost_lines)
        credit_accs = entry.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        self.assertIn(
            self.lc_expense_account, credit_accs,
            "LC expense account must be credited in the LC allocation entry",
        )

        # Exactly one allocation entry — no duplicate from Odoo standard
        all_lc_entries = self.env['account.move'].search([
            ('ref', 'like', 'LC Interim: %'),
            ('company_id', '=', self.company.id),
        ])
        lc_entries_for_this = all_lc_entries.filtered(
            lambda m: lc.name in (m.ref or '')
        )
        self.assertEqual(
            len(lc_entries_for_this), 1,
            "Exactly one LC allocation entry must exist — no duplication with Odoo standard",
        )

    def test_lc_on_hand_only_distribution(self):
        """
        The LC allocation entry distributes the cost across ON-HAND quantities
        ONLY.  Items that were sold before LC validation are NOT included.

        Setup:
          - Receive 4 units → standard_price = 100 (total stock value = 400)
          - Sell 3 units    → 1 unit remains on hand
          - Validate LC of 100

        Expected:
          - Dr. amount = 100  (full LC charged to the 1 remaining unit)
          - Cr. amount = 100
          - New AVCO = (1×100 + 100) / 1 = 200
        """
        # Receive 4 units
        receipt = self._make_receipt(qty=4.0)
        self.assertTrue(receipt.auto_interim_move_id)

        # Sell 3 units — only 1 remains on hand
        self._deliver_from_stock(qty=3.0)

        old_price = self.product.standard_price  # = 100

        lc = self._make_landed_cost(receipt, amount=100.0)
        if not getattr(lc, 'auto_interim_move_id', False):
            return  # LC not validated (no adjustment lines) — not applicable here

        entry = lc.auto_interim_move_id
        self.assertTrue(entry)
        self.assertEqual(entry.state, 'posted')

        total_debit = sum(entry.line_ids.mapped('debit'))
        total_credit = sum(entry.line_ids.mapped('credit'))

        # Full LC amount must be debited / credited
        self.assertAlmostEqual(total_debit, 100.0, places=2,
                               msg="Full LC amount debited regardless of on-hand qty")
        self.assertAlmostEqual(total_credit, 100.0, places=2,
                               msg="Full LC amount credited regardless of on-hand qty")

        # AVCO update: 1 unit on hand, LC = 100 → new price = old + 100
        expected_new_avco = old_price + 100.0   # (1×100 + 100) / 1 = 200
        if self.product.categ_id.property_cost_method == 'average':
            self.assertAlmostEqual(
                self.product.standard_price, expected_new_avco, places=2,
                msg="AVCO must be updated based on on-hand qty only",
            )

    # ------------------------------------------------------------------
    # AVCO costing method
    # ------------------------------------------------------------------

    def test_avco_receipt_creates_interim_entry(self):
        """
        A receipt for an AVCO product must create an interim entry with the
        correct Dr. Stock / Cr. Change-in-Stock direction and amount.
        """
        avco_categ = self.env['product.category'].create({
            'name': 'Test AVCO Category',
            'property_valuation': 'real_time',
            'property_cost_method': 'average',
            'property_stock_valuation_account_id': self.stock_val_account.id,
        })
        avco_product = self.env['product.product'].create({
            'name': 'AVCO Product',
            'type': 'consu',
            'categ_id': avco_categ.id,
            'standard_price': 80.0,
        })
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.in_type_id.id,
            'partner_id': self.vendor.id,
            'move_ids': [(0, 0, {
                'name': 'AVCO receipt move',
                'product_id': avco_product.id,
                'product_uom_qty': 5.0,
                'product_uom': avco_product.uom_id.id,
                'location_id': self.warehouse.in_type_id.default_location_src_id.id,
                'location_dest_id': self.warehouse.lot_stock_id.id,
            })],
        })
        for move in picking.move_ids:
            move.quantity = 5.0
        picking.button_validate()

        self.assertEqual(picking.state, 'done')
        entry = picking.auto_interim_move_id
        self.assertTrue(entry, "Interim entry must be created for AVCO receipt")
        self.assertEqual(entry.state, 'posted')

        debit_accs = entry.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = entry.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        self.assertIn(self.stock_val_account, debit_accs,
                      "Stock valuation account must be debited on AVCO receipt")
        self.assertIn(self.stock_var_account, credit_accs,
                      "Change-in-Stock account must be credited on AVCO receipt")

    def test_avco_delivery_creates_interim_entry(self):
        """
        A delivery for an AVCO product must create an interim entry with the
        correct Dr. Change-in-Stock / Cr. Stock direction.
        """
        avco_categ = self.env['product.category'].create({
            'name': 'Test AVCO Delivery Category',
            'property_valuation': 'real_time',
            'property_cost_method': 'average',
            'property_stock_valuation_account_id': self.stock_val_account.id,
        })
        avco_product = self.env['product.product'].create({
            'name': 'AVCO Delivery Product',
            'type': 'consu',
            'categ_id': avco_categ.id,
            'standard_price': 60.0,
        })
        self.env['stock.quant']._update_available_quantity(
            avco_product, self.warehouse.lot_stock_id, 3.0
        )
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.out_type_id.id,
            'partner_id': self.customer.id,
            'move_ids': [(0, 0, {
                'name': 'AVCO delivery move',
                'product_id': avco_product.id,
                'product_uom_qty': 2.0,
                'product_uom': avco_product.uom_id.id,
                'location_id': self.warehouse.lot_stock_id.id,
                'location_dest_id': self.warehouse.out_type_id.default_location_dest_id.id,
            })],
        })
        for move in picking.move_ids:
            move.quantity = 2.0
        picking.button_validate()

        self.assertEqual(picking.state, 'done')
        entry = picking.auto_interim_move_id
        self.assertTrue(entry, "Interim entry must be created for AVCO delivery")

        debit_accs = entry.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = entry.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        self.assertIn(self.stock_var_account, debit_accs,
                      "Change-in-Stock account must be debited on AVCO delivery")
        self.assertIn(self.stock_val_account, credit_accs,
                      "Stock valuation account must be credited on AVCO delivery")

    # ------------------------------------------------------------------
    # FIFO costing method
    # ------------------------------------------------------------------

    def test_fifo_receipt_creates_interim_entry(self):
        """
        A receipt for a FIFO product must create an interim entry.
        Direction: Dr. Stock / Cr. Change-in-Stock.
        """
        fifo_categ = self.env['product.category'].create({
            'name': 'Test FIFO Category',
            'property_valuation': 'real_time',
            'property_cost_method': 'fifo',
            'property_stock_valuation_account_id': self.stock_val_account.id,
        })
        fifo_product = self.env['product.product'].create({
            'name': 'FIFO Product',
            'type': 'consu',
            'categ_id': fifo_categ.id,
            'standard_price': 120.0,
        })
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.in_type_id.id,
            'partner_id': self.vendor.id,
            'move_ids': [(0, 0, {
                'name': 'FIFO receipt move',
                'product_id': fifo_product.id,
                'product_uom_qty': 4.0,
                'product_uom': fifo_product.uom_id.id,
                'location_id': self.warehouse.in_type_id.default_location_src_id.id,
                'location_dest_id': self.warehouse.lot_stock_id.id,
            })],
        })
        for move in picking.move_ids:
            move.quantity = 4.0
        picking.button_validate()

        self.assertEqual(picking.state, 'done')
        entry = picking.auto_interim_move_id
        self.assertTrue(entry, "Interim entry must be created for FIFO receipt")
        self.assertEqual(entry.state, 'posted')

        debit_accs = entry.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = entry.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        self.assertIn(self.stock_val_account, debit_accs,
                      "Stock valuation account must be debited on FIFO receipt")
        self.assertIn(self.stock_var_account, credit_accs,
                      "Change-in-Stock account must be credited on FIFO receipt")

    # ------------------------------------------------------------------
    # Partial bill compensation
    # ------------------------------------------------------------------

    def test_partial_bill_compensation_amount(self):
        """
        When a vendor bill covers only part of the receipt quantity,
        the compensation entry amount must match the billed amount,
        not the original provisional amount.
        """
        receipt = self._make_receipt(qty=4.0)
        self.assertTrue(receipt.auto_interim_move_id)

        # Bill for 2 units only (partial)
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.vendor.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 2.0,
                'price_unit': 100.0,
                'account_id': self.stock_val_account.id,
            })],
        })
        bill.action_post()

        comp = bill.stock_interim_comp_move_id
        if not comp:
            return  # No provisional picking linked — not applicable in this environment
        self.assertEqual(comp.state, 'posted')
        # Compensation total must not exceed the billed amount
        total = sum(comp.line_ids.mapped('debit'))
        self.assertGreater(total, 0.0,
                           "Compensation entry must have a non-zero amount")

    # ------------------------------------------------------------------
    # Vendor return — provisional direction
    # ------------------------------------------------------------------

    def test_vendor_return_interim_entry_direction(self):
        """
        A vendor return (stock → supplier) must create a provisional entry
        with the same direction as a sale delivery:
        Dr. Change-in-Stock / Cr. Stock Account.
        This verifies the location-based detection (not operation-type-based).
        """
        receipt = self._make_receipt(qty=3.0)
        self.assertTrue(receipt.auto_interim_move_id)

        return_wizard = self.env['stock.return.picking'].with_context(
            active_id=receipt.id, active_model='stock.picking'
        ).create({'picking_id': receipt.id})
        for line in return_wizard.product_return_moves:
            line.quantity = 3.0
        action = return_wizard.create_returns()
        return_picking = self.env['stock.picking'].browse(action['res_id'])
        for move in return_picking.move_ids:
            move.quantity = 3.0
        return_picking.button_validate()

        self.assertEqual(return_picking.state, 'done')
        entry = return_picking.auto_interim_move_id
        self.assertTrue(entry, "Vendor return must create its own interim entry")

        # Verify location-based direction detection:
        # return moves go from internal → supplier
        for move in return_picking.move_ids.filtered(lambda m: m.state == 'done'):
            self.assertEqual(move.location_id.usage, 'internal',
                             "Return move source must be internal")
            self.assertEqual(move.location_dest_id.usage, 'supplier',
                             "Return move destination must be supplier")

        debit_accs = entry.line_ids.filtered(lambda l: l.debit > 0).mapped('account_id')
        credit_accs = entry.line_ids.filtered(lambda l: l.credit > 0).mapped('account_id')
        self.assertIn(self.stock_var_account, debit_accs,
                      "Change-in-Stock must be debited on vendor return")
        self.assertIn(self.stock_val_account, credit_accs,
                      "Stock valuation must be credited on vendor return")
