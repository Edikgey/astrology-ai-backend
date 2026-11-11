"""Temporary product quotas only; never grants or modifies a paid entitlement."""
import os


def free_beta_enabled():
    # Explicit opt-in owned by the server. Client inputs cannot enable it.
    return os.getenv("LUNARIA_FREE_BETA_ENABLED", "").strip().lower() == "true"
