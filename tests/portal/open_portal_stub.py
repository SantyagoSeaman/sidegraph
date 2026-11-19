"""Stand-in child for startup failures and the Windows lifetime pipe check."""

import os
import sys
import time
from contextlib import suppress


def main() -> None:
    """Emit the selected handshake or check the inherited handles and wait for EOF."""
    sys.stdout.reconfigure(encoding="utf-8")
    mode = os.environ["PORTAL_STUB_MODE"]
    if mode == "windows":
        import _winapi
        import msvcrt

        handle = int(sys.argv[sys.argv.index("--lifetime-fd") + 1])
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        # If the unrelated handle reached the child, this signals the parent's event.
        with suppress(OSError):
            _winapi.SetEvent(int(os.environ["PORTAL_STUB_SENTINEL"]))
        print("url http://127.0.0.1:12345/#t=" + "a" * 43, flush=True)
        with os.fdopen(fd, "rb") as pipe:
            assert pipe.read() == b""
        return
    if mode == "malformed":
        print("url https://example.test/", flush=True)
    elif mode == "error":
        print("error Cannot bind the stub port.", flush=True)
    time.sleep(30)


if __name__ == "__main__":
    main()
