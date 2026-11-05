class WatchError(Exception):
    """Base exception for Watch 6R SDK errors."""


class WatchNotConnectedError(WatchError):
    """Raised when an operation requires an active BLE connection."""


class ProtocolError(WatchError):
    """Raised when a protocol packet or response is malformed."""


class QueryTimeoutError(WatchError):
    """Raised when the watch does not answer a command before its timeout."""


class UnsupportedCommandError(WatchError):
    """Raised when a command is outside the SDK's read-only allowlist."""
