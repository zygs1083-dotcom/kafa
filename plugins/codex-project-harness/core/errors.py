"""Shared Kernel error types."""


class HarnessError(Exception):
    """User-facing runtime error."""


def exception_text(exc: BaseException) -> str:
    """Render a stable primary error plus safety-relevant exception notes."""

    primary = str(exc) or type(exc).__name__
    notes = [
        str(note)
        for note in getattr(exc, "__notes__", ())
        if str(note)
    ]
    return "\n".join((primary, *(f"NOTE: {note}" for note in notes)))
