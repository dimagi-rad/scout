#!/usr/bin/env python
"""
Register OAuth SocialApp records from environment variables.

Reads COMMCARE_OAUTH_*, COMMCARE_CONNECT_OAUTH_*, GOOGLE_OAUTH_*, and
GITHUB_OAUTH_* env vars and upserts the corresponding allauth SocialApp rows.

Idempotent — safe to re-run after credential rotation or fresh DB setup.

Usage:
    uv run python scripts/setup_oauth_apps.py
"""

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
    site, _ = Site.objects.get_or_create(
        id=1, defaults={"domain": "localhost:8000", "name": "Scout Dev"}
    )
    site.domain = "localhost:8000"
    site.name = "Scout Dev"
    site.save()

    for provider_id, name, env_prefix in PROVIDERS:
        client_id = os.environ.get(f"{env_prefix}_CLIENT_ID", "")
        client_secret = os.environ.get(f"{env_prefix}_CLIENT_SECRET", "")

        if not client_id or not client_secret:
            print(f"  skip  {name} ({env_prefix}_CLIENT_ID not set)")
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
        print(f"  {'created' if created else 'updated'}  {name} (provider={provider_id})")

    print("\nDone.")


if __name__ == "__main__":
    main()
