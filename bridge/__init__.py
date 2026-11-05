"""Bridge package for the Oraimo Watch 6R dashboard.

Exposes the existing read-only SDK over a local-only HTTP API so the
Chrome new-tab extension can poll it. The SDK itself stays independent
of the dashboard: this package only consumes it.
"""
