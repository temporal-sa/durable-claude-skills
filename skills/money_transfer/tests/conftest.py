"""Test configuration.

The workflow tests mock the banking activities, so they never import or touch
the real bank. We still redirect ``BANK_DB_PATH`` to a throwaway location as a
safety net, in case a test (or a future one) does pull the bank in.
"""

import os
import tempfile

os.environ.setdefault(
    "BANK_DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="bank-test-"), "test.db"),
)
