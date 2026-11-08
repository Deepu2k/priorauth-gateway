class RetryableError(Exception):
    """The call might work later: timeout, connection refused, 5xx, 429."""


class PermanentError(Exception):
    """Retrying will not help: 4xx, bad data."""
