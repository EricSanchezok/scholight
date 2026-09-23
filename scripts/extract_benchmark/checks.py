"""Acceptance checks must remain enabled even under optimized Python execution."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)
