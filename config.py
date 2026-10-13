"""App configuration. Secrets come from the environment."""

import os
import secrets

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# $29 migration-pack unlock key. Paid via Stripe Payment Link later;
# until then keys are issued manually.
UNLOCK_KEY = os.environ.get("PDM_UNLOCK_KEY", "")
