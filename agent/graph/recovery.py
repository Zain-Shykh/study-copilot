"""Startup crash-recovery scan: finds assignment threads stuck mid-node, not cleanly paused."""


def scan_for_interrupted_assignments(assignment_graph, conn) -> list[str]:
    """Returns the titles of assignment threads that were actively
    mid-node (not cleanly paused at an interrupt, not terminal) when the
    process last stopped. Reads distinct assignment: thread_ids from the
    checkpointer's own `checkpoints` table, then inspects each thread's
    current state."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE 'assignment:%'"
        )
        thread_ids = [r[0] for r in cur.fetchall()]

    interrupted_titles = []
    for thread_id in thread_ids:
        snapshot = assignment_graph.get_state({"configurable": {"thread_id": thread_id}})
        if not snapshot.next:
            continue  # terminal — finished, or already ended (reject/decline)
        paused_at_interrupt = any(getattr(t, "interrupts", None) for t in snapshot.tasks)
        if paused_at_interrupt:
            continue  # legitimately waiting on a reply — not a crash
        title = snapshot.values.get("title", thread_id)
        interrupted_titles.append(title)
    return interrupted_titles
