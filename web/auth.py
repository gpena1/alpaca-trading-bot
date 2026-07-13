"""HTTP Basic Auth gate for the dashboard, credentials from env vars only."""

import os
import secrets
from functools import wraps

from flask import Response, request


def _valid(username, password):
    expected_user = os.environ.get("DASHBOARD_USERNAME", "")
    expected_pass = os.environ.get("DASHBOARD_PASSWORD", "")
    if not expected_user or not expected_pass:
        raise RuntimeError("DASHBOARD_USERNAME/DASHBOARD_PASSWORD must be set")
    return secrets.compare_digest(username or "", expected_user) and secrets.compare_digest(
        password or "", expected_pass
    )


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not _valid(auth.username, auth.password):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Trading Dashboard"'},
            )
        return view(*args, **kwargs)

    return wrapped
