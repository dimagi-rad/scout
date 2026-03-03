"""
Diagnose the CommCare Connect workspace selector pipeline for a user.

Usage:
    uv run python manage.py diagnose_connect <email>

Checks each layer and reports exactly where things break:
  1. Does the user exist?
  2. Does a CommCare Connect SocialAccount exist?
  3. Does a SocialToken exist (i.e. is OAuth Connected)?
  4. Can we call the Connect API with that token?
  5. Does the API return opportunities?
  6. Do TenantMembership records exist in the DB?
"""

import requests
from allauth.socialaccount.models import SocialAccount, SocialToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.users.models import TenantMembership


class Command(BaseCommand):
    help = "Diagnose the Connect OAuth workspace selector pipeline"

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email of the user to diagnose")
        parser.add_argument(
            "--resolve",
            action="store_true",
            help="Actually run resolve_connect_opportunities to fix missing memberships",
        )

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        email = options["email"]

        # ── Layer 1: User ──
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Layer 1: User ==="))
        try:
            user = User.objects.get(email=email)
            self.stdout.write(self.style.SUCCESS(f"  OK: User found: {user.email} (id={user.id})"))
        except User.DoesNotExist:
            raise CommandError(f"No user with email '{email}'") from None

        # ── Layer 2: SocialAccount ──
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Layer 2: SocialAccount ==="))
        accounts = SocialAccount.objects.filter(user=user)
        if not accounts.exists():
            self.stdout.write(self.style.ERROR("  FAIL: No SocialAccounts found for this user"))
        for acct in accounts:
            self.stdout.write(f"  - provider={acct.provider} uid={acct.uid}")

        connect_accounts = accounts.filter(provider="commcare_connect")
        if not connect_accounts.exists():
            self.stdout.write(self.style.ERROR(
                "  FAIL: No CommCare Connect SocialAccount. User needs to OAuth with Connect first."
            ))
            return

        self.stdout.write(self.style.SUCCESS(
            f"  OK: {connect_accounts.count()} CommCare Connect SocialAccount(s)"
        ))

        # ── Layer 3: SocialToken ──
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Layer 3: SocialToken (OAuth token) ==="))
        tokens = SocialToken.objects.filter(
            account__user=user, account__provider="commcare_connect"
        )
        if not tokens.exists():
            self.stdout.write(self.style.ERROR(
                "  FAIL: No SocialToken for CommCare Connect. "
                "Provider will show 'Not connected' on the Connections page."
            ))
            return

        token = tokens.first()
        token_preview = token.token[:20] + "..." if len(token.token) > 20 else token.token
        self.stdout.write(self.style.SUCCESS(f"  OK: Token found: {token_preview}"))

        # ── Layer 4: Connect API call ──
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Layer 4: Connect API ==="))
        base_url = getattr(settings, "CONNECT_API_URL", "https://connect.dimagi.com")
        api_url = f"{base_url.rstrip('/')}/export/opp_org_program_list/"
        self.stdout.write(f"  URL: {api_url}")

        try:
            resp = requests.get(
                api_url,
                headers={"Authorization": f"Bearer {token.token}"},
                timeout=30,
            )
            self.stdout.write(f"  HTTP {resp.status_code}")

            if resp.status_code in (401, 403):
                self.stdout.write(self.style.ERROR(
                    f"  FAIL: Auth error ({resp.status_code}). Token may be expired. "
                    "User should disconnect and reconnect on the Connections page."
                ))
                self.stdout.write(f"  Response body: {resp.text[:500]}")
                return

            if resp.status_code != 200:
                self.stdout.write(self.style.ERROR(
                    f"  FAIL: Unexpected status {resp.status_code}"
                ))
                self.stdout.write(f"  Response body: {resp.text[:500]}")
                return

            data = resp.json()
            opportunities = data.get("opportunities", [])
            self.stdout.write(self.style.SUCCESS(
                f"  OK: API returned {len(opportunities)} opportunities"
            ))
            if opportunities:
                self.stdout.write(f"  First 5: {[o.get('name', o.get('id')) for o in opportunities[:5]]}")

            if not opportunities:
                self.stdout.write(self.style.WARNING(
                    "  WARN: API returned 0 opportunities. Check if the user has access "
                    "to any opportunities in CommCare Connect."
                ))
                # Check the full response shape
                self.stdout.write(f"  Response keys: {list(data.keys())}")
                self.stdout.write(f"  Full response (first 500 chars): {resp.text[:500]}")

        except requests.Timeout:
            self.stdout.write(self.style.ERROR(
                f"  FAIL: Timeout connecting to {api_url}"
            ))
            return
        except requests.ConnectionError as e:
            self.stdout.write(self.style.ERROR(
                f"  FAIL: Connection error: {e}"
            ))
            return

        # ── Layer 5: TenantMembership records ──
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Layer 5: TenantMembership records ==="))
        memberships = TenantMembership.objects.filter(
            user=user, provider="commcare_connect"
        )
        self.stdout.write(f"  DB records: {memberships.count()}")
        for tm in memberships[:5]:
            has_cred = hasattr(tm, "credential")
            cred_type = tm.credential.credential_type if has_cred else "NONE"
            self.stdout.write(f"  - {tm.tenant_id} ({tm.tenant_name}) credential={cred_type}")

        if memberships.count() == 0 and len(opportunities) > 0:
            self.stdout.write(self.style.ERROR(
                f"  FAIL: API returned {len(opportunities)} opportunities but DB has 0 memberships.\n"
                "    Resolution did not persist. The _last_refresh cache may have prevented it,\n"
                "    or the signal/view resolution failed silently."
            ))

        # ── Layer 6: Optionally run resolution ──
        if options["resolve"] and len(opportunities) > 0:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "\n=== Running resolve_connect_opportunities... ==="
            ))
            from apps.users.services.tenant_resolution import resolve_connect_opportunities

            result = resolve_connect_opportunities(user, token.token)
            self.stdout.write(self.style.SUCCESS(
                f"  OK: Resolved {len(result)} memberships"
            ))

        # ── Summary ──
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Summary ==="))
        final_count = TenantMembership.objects.filter(
            user=user, provider="commcare_connect"
        ).count()
        if final_count > 0:
            self.stdout.write(self.style.SUCCESS(
                f"  OK: {final_count} Connect workspace(s) available for workspace selector"
            ))
        else:
            self.stdout.write(self.style.ERROR(
                "  FAIL: 0 Connect workspaces. The workspace selector will show nothing.\n"
                "    Run with --resolve to attempt resolution, or check the layers above."
            ))
