"""Remain alive so executor restart cleanup can be tested explicitly."""

import time


print("long-running-ready", flush=True)
time.sleep(300)
