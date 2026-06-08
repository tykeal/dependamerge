# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Output utilities for consistent logging and console display.

This module provides shared utilities for outputting messages to both
logs and console in a consistent manner across different managers.
"""

from __future__ import annotations

import logging

from rich.console import Console


def log_and_print(
    logger: logging.Logger,
    console: Console,
    message: str,
    style: str | None = None,
    level: str = "info",
) -> None:
    """Log message and also print to stdout for CLI visibility.

    This function provides a unified interface for outputting messages
    to both the logging system and the console, ensuring consistency
    across the application.

    Args:
        logger: Logger instance to use for logging
        console: Rich Console instance for styled output
        message: The message to log and print
        style: Optional rich style for console output (e.g., "bold red")
        level: Log level - one of 'debug', 'info', 'warning', 'error'

    Examples:
        >>> import logging
        >>> from rich.console import Console
        >>> logger = logging.getLogger(__name__)
        >>> console = Console()
        >>> log_and_print(logger, console, "Operation complete", level="info")
        >>> log_and_print(logger, console, "Error occurred", style="bold red", level="error")
    """
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(message)
    # ``markup=False`` is important: status messages routinely embed a
    # bracketed reason (e.g. "❌ Failed: <url> [merge conflicts]").  With
    # Rich markup enabled (the default), ``[merge conflicts]`` is parsed
    # as a style tag and silently dropped, so the reason never reaches
    # the user.  These messages never use intentional Rich markup — the
    # ``style`` argument is the supported styling path — so disabling
    # markup is safe and keeps the reason visible.
    if style:
        console.print(message, style=style, markup=False)
    else:
        # Route through the Rich console rather than the builtin
        # ``print`` so output coordinates correctly with any active
        # Rich ``Live`` display in the same console.  Using
        # ``print`` here causes the Live re-draw to garble or eat
        # interleaved messages (e.g. per-PR ✅/❌ lines emitted
        # while a progress tracker is running).
        console.print(message, markup=False)
