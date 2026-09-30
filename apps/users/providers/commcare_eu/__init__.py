"""OAuth2 provider for CommCare HQ's EU server (eu.commcarehq.org, #719).

EU HQ is a separate deployment with its own accounts and OAuth applications, so
it is a separate allauth provider rather than a setting on the www one. It is
hidden until ``setup_oauth_apps`` finds ``COMMCARE_EU_OAUTH_CLIENT_ID``/``_SECRET``.
"""
