"""
Django settings for Scout deployed to the connect-labs AWS environment.

Inherits production security settings but configures for:
- ALB TLS termination (no SSL redirect)
- /scout/ path prefix (FORCE_SCRIPT_NAME)
- iframe embedding from labs.connect.dimagi.com
"""

import environ

env = environ.Env()

from .production import *  # noqa: F401, F403

# ALB terminates TLS, so don't redirect HTTP → HTTPS at Django level
SECURE_SSL_REDIRECT = False

# Scout is served under /scout/ path prefix on the ALB
FORCE_SCRIPT_NAME = env("FORCE_SCRIPT_NAME", default="/scout")

# After OAuth login, return to the connect-labs embed page (not the Scout
# standalone app). The embed iframe detects the session cookie automatically
# since everything is same-origin.
LOGIN_REDIRECT_URL = "/labs/scout/"
LOGOUT_REDIRECT_URL = "/labs/scout/"

# Allow iframe embedding from labs.connect.dimagi.com (same origin)
X_FRAME_OPTIONS = "SAMEORIGIN"
