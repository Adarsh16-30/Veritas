"""Checkpoint before side effect (Rule 6).

PROVIDED VERBATIM — PRD §9.2. Do not modify, simplify, or stub.
"""


def advance(ctx, db, side_effect):
    db.checkpoint(ctx.workflow_id, ctx.step, status="in_progress")  # BEFORE the effect
    result = side_effect()  # ERPNext write
    db.checkpoint(ctx.workflow_id, ctx.step, status="committed")
    return result


# On restart, any step left "in_progress" is re-entered idempotently (§9.1 /
# erp.idempotent), so a crash between the two checkpoints never double-posts.
