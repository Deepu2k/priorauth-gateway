"""Print the sha256 of a password or API key, for DEMO_USERS / API_KEYS.

python scripts/hash_secret.py 'my-new-api-key'
"""

import hashlib
import sys

if len(sys.argv) != 2:
    sys.exit(__doc__)
print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
