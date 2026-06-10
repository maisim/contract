# Copyright 2024 Odoo Community Association (OCA)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging

from dateutil.relativedelta import relativedelta

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Fix contract lines with inconsistent last_date_invoiced values.

    This script addresses two classes of data corruption introduced by the
    12.0 → 16.0 migration:

    Case A — recurring_next_date <= last_date_invoiced
    ---------------------------------------------------
    In 12.0, _update_recurring_next_date() wrote both last_date_invoiced and
    recurring_next_date atomically.  In early 16.0 builds the method was
    simplified to write only last_date_invoiced, relying on the computed-field
    dependency chain to recalculate recurring_next_date.  However,
    last_date_invoiced is not in the @api.depends of _compute_recurring_next_date,
    so recurring_next_date was left stale, violating the
    _check_last_date_invoiced constraint («Ligne 2» error).

    Fix: recalculate recurring_next_date via the model method
    get_next_invoice_date(last_date_invoiced + 1 day, …) and write it together
    with any pending cleanup.

    Case B — date_end < last_date_invoiced
    ----------------------------------------
    This can result from the old _init_last_date_invoiced helper setting
    last_date_invoiced to a value beyond date_end during the migration.  The
    business rule is ambiguous (the line may have been intentionally invoiced
    past its end date), so this script only logs a detailed warning and does
    NOT auto-correct the data.  An integrator must review and fix these lines
    manually if needed.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    ContractLine = env["contract.line"]

    # --- Case A: recurring_next_date <= last_date_invoiced -------------------
    cr.execute(
        """
        SELECT id
        FROM contract_line
        WHERE last_date_invoiced IS NOT NULL
          AND recurring_next_date IS NOT NULL
          AND recurring_next_date <= last_date_invoiced
          AND is_canceled IS NOT TRUE
        """
    )
    case_a_ids = [row[0] for row in cr.fetchall()]
    if case_a_ids:
        lines_a = ContractLine.browse(case_a_ids)
        fixed_ids = []
        for line in lines_a:
            next_start = line.last_date_invoiced + relativedelta(days=1)
            new_next_date = line.get_next_invoice_date(
                next_start,
                line.recurring_invoicing_type,
                line.recurring_invoicing_offset,
                line.recurring_rule_type,
                line.recurring_interval,
                max_date_end=line.date_end,
            )
            # Write with tracking disabled to keep migration logs clean.
            line.with_context(tracking_disable=True).write(
                {"recurring_next_date": new_next_date}
            )
            fixed_ids.append(line.id)
        _logger.info(
            "contract post-migrate 16.0.2.16.0 — Case A: fixed recurring_next_date "
            "on %d contract line(s): %s",
            len(fixed_ids),
            fixed_ids,
        )
    else:
        _logger.info(
            "contract post-migrate 16.0.2.16.0 — Case A: no lines to fix "
            "(recurring_next_date already consistent)."
        )

    # --- Case B: date_end < last_date_invoiced --------------------------------
    cr.execute(
        """
        SELECT id, name, date_end, last_date_invoiced
        FROM contract_line
        WHERE last_date_invoiced IS NOT NULL
          AND date_end IS NOT NULL
          AND date_end < last_date_invoiced
        """
    )
    case_b_rows = cr.fetchall()
    if case_b_rows:
        _logger.warning(
            "contract post-migrate 16.0.2.16.0 — Case B: %d contract line(s) have "
            "date_end < last_date_invoiced.  These lines may have been invoiced "
            "beyond their end date during the 12.0→16.0 migration.  The data is "
            "ambiguous and has NOT been auto-corrected.  Please review the "
            "following lines manually:\n%s",
            len(case_b_rows),
            "\n".join(
                f"  id={row[0]} name={row[1]!r} date_end={row[2]}"
                f" last_date_invoiced={row[3]}"
                for row in case_b_rows
            ),
        )
    else:
        _logger.info(
            "contract post-migrate 16.0.2.16.0 — Case B: no lines with "
            "date_end < last_date_invoiced."
        )
