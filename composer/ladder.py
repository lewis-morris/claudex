"""The escalation ladder: which (model, effort) a worker steps up to when its work keeps failing review."""


def next_rung(ladder, agent, model, effort):
    """The rung directly above (model, effort) on this agent's ladder, or None.

    Only exact positions on the ladder can step up. A pair that isn't on the ladder (for example a
    model the lead chose by hand) has no defined "stronger" neighbour, so it never escalates rather
    than risk stepping down to a weaker model.
    """
    rungs = [tuple(r) for r in ladder.get(agent, [])]
    try:
        i = rungs.index((model, effort))
    except ValueError:
        return None
    return rungs[i + 1] if i + 1 < len(rungs) else None
