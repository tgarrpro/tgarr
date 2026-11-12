"""Moved to registry/tme_validate.py, where the registry's daily web reader
(registry/webcrawl.py) uses the same analysis. This runs the bulk pass as before."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "registry"))
import tme_validate  # noqa: E402

if __name__ == "__main__":
    asyncio.run(tme_validate.main())
