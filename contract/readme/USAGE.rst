#. Contracts are in Invoicing -> Customers -> Customer and Invoicing -> Vendors -> Supplier Contracts
#. When creating a contract, fill fields for selecting the invoicing parameters:

   * a journal
   * a price list (optional)

#. And add the lines to be invoiced with:

   * the product with a description, a quantity and a price
   * the recurrence parameters: interval (days, weeks, months, months last day or years),
     start date, date of next invoice (automatically computed, can be modified) and end date (optional)
   * auto-price, for having a price automatically obtained from the price list
   * #START# - #END# or #INVOICEMONTHNAME# in the description field to display
     the start/end date or the start month of the invoiced period in the invoice line description
   * pre-paid (invoice at period start) or post-paid (invoice at start of next period)

#. The "Generate Recurring Invoices from Contracts" cron runs daily to generate the invoices.
   If you are in debug mode, you can click on the invoice creation button.
#. The *Show recurring invoices* shortcut on contracts shows all invoices created from the
   contract.
#. The contract report can be printed from the Print menu
#. The contract can be sent by email with the *Send by Email* button
#. Contract templates can be created from the Configuration -> Contracts -> Contract Templates menu.
   They allow to define default journal, price list and lines when creating a contract.
   To use it, just select the template on the contract and fields will be filled automatically.

* Contracts appear in portal to following users in every contract:

.. image:: ../static/src/screenshots/portal-my.png
.. image:: ../static/src/screenshots/portal-list.png
.. image:: ../static/src/screenshots/portal-detail.png


Data fix after migration from 12.0 to 16.0
===========================================

After migrating from 12.0 to 16.0, some contract lines may have inconsistent
values that violate the ``_check_last_date_invoiced`` constraint:

* ``date_end < last_date_invoiced`` — the line was invoiced past its end date.
* ``recurring_next_date <= last_date_invoiced`` — the next invoice date is stale
  (equal to the last invoice date) because ``_update_recurring_next_date`` no
  longer writes both fields atomically.
* ``last_date_invoiced <= date_end < today`` — the end date is in the past;
  the line must be closed and auto-renew disabled to prevent the
  ``cron_renew_contract_line`` from cascading year-by-year.

These inconsistencies block any ORM write to the affected lines, and may
trigger unwanted catch-up invoicing or auto-renewal cascades when fixed.

A maintenance script is available to diagnose and fix these lines.
Set ``DRY_RUN = True`` to preview the changes, then ``DRY_RUN = False`` to
apply them via direct SQL (bypassing ORM constraints).
Only active, non-terminated contracts are processed.

.. code-block:: python

    # Run in an Odoo shell:
    #   exec(open("/path/to/fix_contract_lines.py").read())

    from dateutil.relativedelta import relativedelta
    from odoo import fields

    DRY_RUN = True

    ContractLine = env["contract.line"]
    today = fields.Date.context_today(env.user)

    # Only active, non-terminated contracts
    lines = ContractLine.search([
        ("last_date_invoiced", "!=", False),
        ("contract_id.active", "=", True),
        ("contract_id.is_terminated", "=", False),
    ])

    # --- 1a: date_end < last_date_invoiced (invoiced past end date) ---
    fix_1a = lines.filtered(lambda l: l.date_end and l.date_end < l.last_date_invoiced)

    # --- 1b: recurring_next_date <= last_date_invoiced (line_recurrence=True) ---
    # Only for genuinely active lines: date_end is NULL or in the future.
    # Lines with past date_end are handled by 1c below (close + disable auto-renew).
    fix_1b = ContractLine
    for line in lines:
        if not line.contract_id.line_recurrence:
            continue
        if line.date_end and line.date_end < today:
            continue
        if line.recurring_next_date and line.recurring_next_date <= line.last_date_invoiced:
            fix_1b |= line

    # --- 1c: past date_end (close + disable auto-renew) ---
    # Close lines where date_end is in the past.  Disable auto-renew to prevent
    # cron_renew_contract_line from cascading year-by-year.
    fix_1c = ContractLine
    for line in lines:
        if (
            line.date_end
            and line.last_date_invoiced <= line.date_end
            and line.date_end < today
        ):
            fix_1c |= line

    all_fix = fix_1a | fix_1b | fix_1c
    impacted = all_fix.mapped("contract_id")
    print(f"Impacted contracts: {len(impacted)}")
    for c in impacted:
        print(f"  ID={c.id}  '{c.name}'  line_recurrence={c.line_recurrence}")

    updates = []  # (line_id, field, new_value)

    # --- 1a: date_end < last_date_invoiced (extend end date) ---
    for line in fix_1a:
        updates.append((line.id, "date_end", line.last_date_invoiced))

    # --- 1b: recurring_next_date <= last_date_invoiced (recompute + advance) ---
    # These are stale values (equal to last_date_invoiced from months/years ago).
    # Advancing to today avoids the cron catching up all missed periods at once.
    for line in fix_1b:
        npds = line.next_period_date_start
        new_rnd = line.get_next_invoice_date(
            npds, line.recurring_invoicing_type, line.recurring_invoicing_offset,
            line.recurring_rule_type, line.recurring_interval, max_date_end=line.date_end,
        )
        if new_rnd and new_rnd < today:
            delta = line.get_relative_delta(line.recurring_rule_type, line.recurring_interval)
            while new_rnd and new_rnd < today:
                npds = npds + delta
                new_rnd = line.get_next_invoice_date(
                    npds, line.recurring_invoicing_type, line.recurring_invoicing_offset,
                    line.recurring_rule_type, line.recurring_interval, max_date_end=line.date_end,
                )
        updates.append((line.id, "recurring_next_date", new_rnd))

    # --- 1c: close past-date lines ---
    for line in fix_1c:
        updates.append((line.id, "date_end", line.last_date_invoiced))
        updates.append((line.id, "is_auto_renew", False))

    # --- Apply via SQL (DRY_RUN = False) ---
    if not DRY_RUN:
        cr = env.cr
        table = ContractLine._table
        # Group by line for a single UPDATE per line, converting False to None
        by_line = {}
        for line_id, field, new_val in updates:
            by_line.setdefault(line_id, {})[field] = new_val
        for line_id, fields_vals in by_line.items():
            set_clause = ", ".join(f"{f} = %s" for f in fields_vals)
            values = [(v if not isinstance(v, bool) else None) for v in fields_vals.values()]
            values.append(line_id)
            cr.execute(f"UPDATE {table} SET {set_clause} WHERE id = %s", values)
        cr.commit()
        ContractLine.invalidate_model()
        env["contract.contract"].invalidate_model()
        print(f"Fixed {len(by_line)} lines.")

    if DRY_RUN:
        print(f"DRY-RUN: {len(updates)} writes would be performed.")
        print("Set DRY_RUN = False to apply.")

