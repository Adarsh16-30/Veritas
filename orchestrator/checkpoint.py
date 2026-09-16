"""Checkpoint before side effect (Rule 6).

PROVIDED VERBATIM — PRD §9.2. Do not modify, simplify, or stub. Byte-identical
to the PRD and fenced off from the formatter; `scripts/check_rules.sh` diffs it.
"""


# PROVIDED VERBATIM (PRD §9.2). `fmt: off` must stay a bare directive —
# a trailing comment on the same line silently disables it.
# fmt: off
def advance(ctx, db, side_effect):
    db.checkpoint(ctx.workflow_id, ctx.step, status="in_progress")  # BEFORE the effect
    result = side_effect()                                          # ERPNext write
    db.checkpoint(ctx.workflow_id, ctx.step, status="committed")
    return result
# On restart, any step left "in_progress" is re-entered idempotently (Section 9.1),
# so a crash between the two checkpoints never double-posts.
# fmt: on
