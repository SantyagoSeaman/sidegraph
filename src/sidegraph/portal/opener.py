"""Keep the token out of the browser's process arguments (D1, D3)."""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile

_lock = threading.Lock()
_redirect: Path | None = None


def open_via_redirect_file(url: str, opener: Callable[[str], object]) -> Path:
    """Write a private redirect and give only its path to the browser opener (D1)."""
    global _redirect
    target = json.dumps(url).replace("<", "\\u003c")
    try:
        with (
            _lock,
            NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="sidegraph-portal-", suffix=".html", delete=False
            ) as file,
        ):
            path = Path(file.name)
            _redirect = path
            file.write(
                '<!doctype html>\n<meta charset="utf-8">\n'
                '<meta name="referrer" content="no-referrer">\n'
                f"<script>location.replace({target});</script>\n"
            )
        opener(str(path))
    except Exception:
        forget_redirect_file()
        raise
    return path


def forget_redirect_file() -> None:
    """Remove the launch redirect after authentication or at shutdown (D1)."""
    global _redirect
    with _lock:
        if _redirect is not None:
            try:
                _redirect.unlink(missing_ok=True)
            except OSError:
                return  # It may still be open in the browser. Retry at shutdown.
            _redirect = None
