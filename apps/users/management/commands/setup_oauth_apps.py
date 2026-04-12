"""Register OAuth SocialApp records from environment variables.

Reads COMMCARE_OAUTH_*, COMMCARE_CONNECT_OAUTH_*, GOOGLE_OAUTH_*, and
GITHUB_OAUTH_* env vars and upserts the corresponding allauth SocialApp rows.

Idempotent — safe to re-run after credential rotation or fresh DB setup.
"""

import os

from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand

# (provider_id, display_name, env_prefix)
PROVIDERS = [
    ("commcare", "CommCare HQ", "COMMCARE"),
    ("commcare_connect", "CommCare Connect", "CONNECT"),
    ("google", "Google", "GOOGLE_OAUTH"),
    ("github", "GitHub", "GITHUB_OAUTH"),
]


class Command(BaseCommand):
    help = "Bootstrap OAuth SocialApp records from environment variables."

    def add_arguments(self, parser):
        parser.add_argument(
            "--domain",
            default="localhost:8000",
            help="Site domain for OAuth callbacks (default: localhost:8000)",
        )

    def handle(self, *args, **options):
        domain = options["domain"]
        site_name = "Scout" if domain != "localhost:8000" else "Scout Dev"

        site, _ = Site.objects.get_or_create(id=1, defaults={"domain": domain, "name": site_name})
        site.domain = domain
        site.name = site_name
        site.save()
        self.stdout.write(f"  site   {site.domain} ({site.name})")

        for provider_id, name, env_prefix in PROVIDERS:
            client_id = os.environ.get(f"{env_prefix}_OAUTH_CLIENT_ID", "")
            client_secret = os.environ.get(f"{env_prefix}_OAUTH_CLIENT_SECRET", "")

            if not client_id or not client_secret:
                self.stdout.write(f"  skip   {name} ({env_prefix}_CLIENT_ID not set)")
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
            verb = "create" if created else "update"
            self.stdout.write(f"  {verb} {name} (provider={provider_id})")

        self.stdout.write(self.style.SUCCESS("Done."))
