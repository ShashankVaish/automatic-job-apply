"""Terminal interaction for assist and review mode.

Assist mode has to watch two things at once: the browser (did you click Submit
and land on a confirmation page?) and the keyboard (did you press "s" to skip?).
Both are polled without blocking, on Windows via msvcrt and elsewhere via
select() on stdin.
"""
from __future__ import annotations

import logging
import sys
import time

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


class KeyWatcher:
    """Non-blocking single-key reads from the terminal.

    Used as a context manager so POSIX terminal settings are always restored,
    even if the run crashes.
    """

    def __init__(self) -> None:
        self._fd: int | None = None
        self._saved: object | None = None
        self._active = False

    def __enter__(self) -> "KeyWatcher":
        if IS_WINDOWS:
            self._active = True
            return self
        try:
            import termios  # noqa: PLC0415
            import tty  # noqa: PLC0415

            if not sys.stdin.isatty():
                return self
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._active = True
        except Exception as exc:  # pragma: no cover
            log.debug("raw terminal mode unavailable: %s", exc)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None and self._saved is not None:
            try:
                import termios  # noqa: PLC0415

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:  # pragma: no cover
                pass
        self._active = False

    def poll(self) -> str | None:
        """Return a pressed key, or None if nothing is waiting."""
        if not self._active:
            return None
        if IS_WINDOWS:
            try:
                import msvcrt  # noqa: PLC0415

                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    return ch
            except Exception as exc:  # pragma: no cover
                log.debug("msvcrt poll failed: %s", exc)
            return None
        try:
            import select  # noqa: PLC0415

            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready:
                return sys.stdin.read(1)
        except Exception as exc:  # pragma: no cover
            log.debug("stdin poll failed: %s", exc)
        return None

    def drain(self) -> None:
        """Throw away buffered keystrokes so an old Enter can't auto-answer."""
        for _ in range(64):
            if self.poll() is None:
                return


def ask_yes_no(question: str, *, default: bool | None = None) -> bool:
    """Blocking y/n prompt for review mode."""
    suffix = " [y/n]"
    if default is True:
        suffix = " [Y/n]"
    elif default is False:
        suffix = " [y/N]"
    while True:
        try:
            raw = input(question + suffix + " ").strip().lower()
        except EOFError:
            return bool(default)
        if not raw and default is not None:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please answer y or n.")


class AssistResult:
    SUBMITTED = "submitted"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    QUIT = "quit"


def wait_for_user_submit(
    *,
    is_confirmed,
    timeout_s: float = 600.0,
    poll_s: float = 1.5,
    keys: KeyWatcher | None = None,
    announce=print,
) -> str:
    """Block until the user submits, skips, or we give up.

    `is_confirmed` is a zero-arg callable that returns True once the page looks
    like a confirmation page. Returns one of the AssistResult constants.
    """
    deadline = time.monotonic() + timeout_s
    last_hint = 0.0
    watcher = keys

    while time.monotonic() < deadline:
        try:
            if is_confirmed():
                return AssistResult.SUBMITTED
        except Exception as exc:
            log.debug("confirmation check failed: %s", exc)

        if watcher is not None:
            key = watcher.poll()
            if key:
                k = key.lower()
                if k == "s":
                    return AssistResult.SKIPPED
                if k == "q":
                    return AssistResult.QUIT
                if k in ("\r", "\n"):
                    # Enter means "I'm done, check the page now".
                    try:
                        if is_confirmed():
                            return AssistResult.SUBMITTED
                    except Exception:
                        pass
                    announce(
                        "  No confirmation page detected yet. Still waiting - "
                        "press s to skip this job."
                    )

        now = time.monotonic()
        if now - last_hint > 60:
            remaining = int(deadline - now)
            announce(
                "  Still waiting ({0}s left). Click Submit in the browser, "
                "or press s to skip.".format(remaining)
            )
            last_hint = now

        time.sleep(poll_s)

    return AssistResult.TIMEOUT
