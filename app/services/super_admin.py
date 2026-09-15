"""Helpers for the environment-configured fallback administrator account."""


DEFAULT_SUPER_ADMIN_USERNAME = "tceadmin"


def configured_super_admin_username(config):
    """Return the fallback username in the same form used by the login form."""
    value = config.get("SUPER_ADMIN_USERNAME") or DEFAULT_SUPER_ADMIN_USERNAME
    return str(value).strip().lower()
