"""ID helpers for the Run runtime."""
import secrets


def new_run_id() -> str:
    """``run_`` prefix + 16 hex chars (8 bytes of randomness, 64-bit space).

    Collision probability at 1M runs: ~3e-8. Good enough for our scale.
    """
    return "run_" + secrets.token_hex(8)
