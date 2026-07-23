"""Create a detached same-UID descendant for lease-cleanup acceptance."""

import os
import time


first = os.fork()
if first == 0:
    os.setsid()
    second = os.fork()
    if second:
        os._exit(0)
    # Do not keep the lease's stdio pipes open: the detached descendant is
    # intentionally observable only through fixed-UID cleanup.
    for descriptor in (0, 1, 2):
        try:
            os.close(descriptor)
        except OSError:
            pass
    time.sleep(300)
    os._exit(0)

os.waitpid(first, 0)
print("escape-descendant-ok", flush=True)
