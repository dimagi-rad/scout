#!/usr/bin/env python
"""
Register OAuth SocialApp records from environment variables.

Reads COMMCARE_OAUTH_*, COMMCARE_CONNECT_OAUTH_*, GOOGLE_OAUTH_*, and
GITHUB_OAUTH_* env vars and upserts the corresponding allauth SocialApp rows.

Idempotent — safe to re-run after credential rotation or fresh DB setup.

Usage:
    # Local dev
    uv run python scripts/setup_oauth_apps.py

    # Production (updates Site domain to match DJANGO_ALLOWED_HOSTS)
    uv run python scripts/setup_oauth_apps.py --domain scout.dimagi.com
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

import django

django.setup()

from allauth.socialaccount.models import SocialApp  # noqa: E402
from django.contrib.sites.models import Site  # noqa: E402

# (provider_id, display_name, env_prefix)
PROVIDERS = [
    ("commcare", "CommCare HQ", "COMMCARE_OAUTH"),
    ("commcare_connect", "CommCare Connect", "COMMCARE_CONNECT_OAUTH"),
    ("google", "Google", "GOOGLE_OAUTH"),
    ("github", "GitHub", "GITHUB_OAUTH"),
]


def main():
    parser = argparse.ArgumentParser(description="Bootstrap OAuth SocialApp records")
    parser.add_argument(
        "--domain",
        default="localhost:8000",
        help="Site domain for OAuth callbacks (default: localhost:8000)",
    )
    args = parser.parse_args()

    site_name = "Scout" if args.domain != "localhost:8000" else "Scout Dev"
    site, _ = Site.objects.get_or_create(
        id=1, defaults={"domain": args.domain, "name": site_name}
    )
    site.domain = args.domain
    site.name = site_name
    site.save()
    print(f"  site   {site.domain} ({site.name})")

    for provider_id, name, env_prefix in PROVIDERS:
        client_id = os.environ.get(f"{env_prefix}_CLIENT_ID", "")
        client_secret = os.environ.get(f"{env_prefix}_CLIENT_SECRET", "")

        if not client_id or not client_secret:
            print(f"  skip   {name} ({env_prefix}_CLIENT_ID not set)")
            continue

        app, created = SocialApp.objects.update_or_create(
            provider=provider_id,
            defaults={
                "name": name,
                "client_id": client_id,
                "secret": client_secret,
            },
        )
        app.sites.add(site)
        print(f"  {'create' if created else 'update'} {name} (provider={provider_id})")

    print("\nDone.")


if __name__ == "__main__":
    main()
