def check_duplicate_thought(current_description: str, prev_description: str) -> str | None:
    """Check if the agent is submitting the exact same description for an action."""
    if current_description == prev_description and current_description:
        return (
            "SYSTEM: You have submitted the exact same description as the previous turn — "
            "word for word. You are looping. Either:\n"
            "1. Switch to a different mode for a fresh perspective, or\n"
            "2. Tell the user what you've found so far and ask for guidance.\n"
            "Do NOT repeat the same action."
        )
    return None


def check_repeated_execution(script_to_run: str, exec_history: list[tuple[str, str, int]]) -> tuple[bool, str | None]:
    """Check if the exact same script has been executed multiple times."""
    repeat_count = 0
    matched_cell = None

    script_to_run = script_to_run.strip()
    for prev_script, _prev_output, prev_cell in exec_history:
        if script_to_run == prev_script:
            repeat_count += 1
            matched_cell = prev_cell

    if repeat_count >= 2 and matched_cell is not None:
        warning = (
            f"SYSTEM: This exact script has already been executed {repeat_count} times "
            f"with the same output. It was NOT executed again. "
            f"Use %view_output Cell_{matched_cell} to review the previous output. "
            f"Try a fundamentally different approach."
        )
        return True, warning

    return False, None


def check_final_script_bounce(current_mode: str, final_script_bounces: int, max_bounces: int) -> tuple[bool, str | None]:
    """Handle final script submission constraints."""
    if current_mode != "building":
        return True, (
            "SYSTEM: Final script submitted outside building mode. "
            "Switch to reading or testing mode before attempting to finalise. "
            "Rule: only submit the final script when in building mode."
        )

    # Note: final_script_bounces has already been incremented before this check in main loop
    if final_script_bounces < max_bounces:
        return True, (
            "SYSTEM: Final script received but not yet verified. "
            "Confirm you have tested it in testing mode. "
            "If reading/testing mode cannot produce a working solution, "
            "inform the user in plain text and halt. "
            "Otherwise, resubmit once testing is complete."
        )

    return False, None
