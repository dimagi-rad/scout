"""Register OAuth SocialApp records from environment variables.

Reads ``{PREFIX}_OAUTH_CLIENT_ID`` / ``{PREFIX}_OAUTH_CLIENT_SECRET`` for each
provider and upserts the corresponding allauth SocialApp rows. Production uses
COMMCARE_OAUTH_*, CONNECT_OAUTH_*, OCS_OAUTH_*, GOOGLE_OAUTH_*, GITHUB_OAUTH_*.
Staging substitutes STAGING_CONNECT_OAUTH_* for Connect because Connect staging
has its own OAuth application database.

Idempotent — safe to re-run after credential rotation or fresh DB setup.
"""

import os

from allauth.socialaccount.models import SocialApp
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand

# (provider_id, display_name, env_prefix)
# The env vars read are ``{env_prefix}_OAUTH_CLIENT_ID`` /
# ``{env_prefix}_OAUTH_CLIENT_SECRET`` — so the prefix must NOT itself end in
# ``_OAUTH`` (that produced the double ``_OAUTH_`` bug for Google/GitHub).
PROVIDERS = [
    ("commcare", "CommCare HQ", "COMMCARE"),
    ("commcare_connect", "CommCare Connect", "CONNECT"),
    ("ocs", "Open Chat Studio", "OCS"),
    ("google", "Google", "GOOGLE"),
    ("github", "GitHub", "GITHUB"),
]


def oauth_env_prefix(provider_id: str, default_prefix: str) -> str:
    """Select environment-isolated credentials for providers that need them."""
    if provider_id == "commcare_connect" and settings.DEPLOY_ENVIRONMENT == "staging":
        return "STAGING_CONNECT"
    return default_prefix


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

        for provider_id, name, default_prefix in PROVIDERS:
            env_prefix = oauth_env_prefix(provider_id, default_prefix)
            client_id = os.environ.get(f"{env_prefix}_OAUTH_CLIENT_ID", "")
            client_secret = os.environ.get(f"{env_prefix}_OAUTH_CLIENT_SECRET", "")

            if not client_id or not client_secret:
                self.stdout.write(f"  skip   {name} ({env_prefix}_OAUTH_CLIENT_ID not set)")
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
