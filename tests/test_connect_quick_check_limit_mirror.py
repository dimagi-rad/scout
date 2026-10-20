import re
from pathlib import Path

from apps.users.services.tenant_listing.connect import MAX_SUBSET

FRONTEND_LIMIT = Path(__file__).parent.parent / "frontend/src/lib/connectAccessCheck.ts"


def test_frontend_warning_threshold_matches_the_quick_check_cap():
    match = re.search(r"CONNECT_QUICK_CHECK_LIMIT = (\d+)", FRONTEND_LIMIT.read_text())
    assert match, "CONNECT_QUICK_CHECK_LIMIT not found"
    assert int(match.group(1)) == MAX_SUBSET
