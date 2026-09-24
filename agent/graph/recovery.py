"""Startup crash-recovery scan: finds assignment/email threads stuck
mid-node, not cleanly paused."""


async def scan_for_interrupted_threads(assignment_graph, email_graph, conn) -> list[str]:
    """Returns display names of assignment/email threads that were
    actively mid-node (not cleanly paused at an interrupt, not terminal)
    when the process last stopped. Reads distinct thread_ids per prefix
    from the checkpointer's own `checkpoints` table, then inspects each
    thread's current state with the graph that owns that prefix."""
    interrupted = []
    for prefix, graph, label_key in (
        ("assignment:", assignment_graph, "title"),
        ("email:", email_graph, "recipient_display"),
    ):
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE %s", (f"{prefix}%",))
            thread_ids = [r[0] for r in cur.fetchall()]

        for thread_id in thread_ids:
            snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            if not snapshot.next:
                continue  # terminal — finished, or already ended (reject/decline)
            paused_at_interrupt = any(getattr(t, "interrupts", None) for t in snapshot.tasks)
            if paused_at_interrupt:
                continue  # legitimately waiting on a reply — not a crash
            interrupted.append(snapshot.values.get(label_key, thread_id))
    return interrupted
