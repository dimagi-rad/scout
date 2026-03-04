# Canonical Tenant Model — Design

**Issue:** #52 — Security: Arbitrary tenant membership creation enables cross-tenant access
**Date:** 2026-03-04

## Problem

The current authorization boundary is the raw `tenant_id` string. Any authenticated user can POST an arbitrary `tenant_id` to `/api/auth/tenant-credentials/`, receive a `TenantMembership`, and gain access to all workspace-scoped resources for that tenant. Authorization checks like:

```python
TenantMembership.objects.filter(user=request.user, tenant_id=artifact.workspace.tenant_id).exists()
```

are trivially bypassed by guessing or observing a tenant identifier string.

## Approach

Introduce a canonical `Tenant` model that decouples the external provider identifier from the internal authorization boundary. A `TenantMembership` must reference a verified `Tenant` record rather than a free-form string. A `Tenant` record is only created after the credential or OAuth token is verified against the provider.

## Data Model Changes

### New model: `Tenant` (in `apps/users/models.py`)

```python
class Tenant(models.Model):
    id = UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = CharField(max_length=50, choices=PROVIDER_CHOICES)
    external_id = CharField(max_length=255)       # domain name or org ID from provider
    canonical_name = CharField(max_length=255)    # updated on each OAuth/API-key refresh
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["provider", "external_id"]
```

### `TenantMembership` — updated

Remove `provider`, `tenant_id`, `tenant_name` fields. Add:

```python
tenant = ForeignKey(Tenant, on_delete=CASCADE, related_name="memberships")
```

Unique constraint changes from `["user", "provider", "tenant_id"]` to `["user", "tenant"]`.

### `TenantWorkspace` — updated

Replace `tenant_id = CharField(unique=True)` and `tenant_name = CharField(...)` with:

```python
tenant = OneToOneField("users.Tenant", on_delete=CASCADE, related_name="workspace")
```

`__str__` and any name/id references become `self.tenant.canonical_name` / `self.tenant.external_id`.

### `TenantSchema`, `TenantMetadata` — no direct changes

These already FK to `TenantMembership` and are unaffected.

## Authorization Check Changes

All cross-tenant access checks change from string comparison:

```python
# Before
TenantMembership.objects.filter(user=request.user, tenant_id=artifact.workspace.tenant_id)
```

to FK comparison:

```python
# After
TenantMembership.objects.filter(user=request.user, tenant=artifact.workspace.tenant)
```

Affected files:
- `apps/artifacts/views.py` (5 occurrences)
- `apps/recipes/api/views.py`
- `apps/knowledge/api/views.py`
- `apps/chat/views.py`
- `apps/agents/graph/base.py`

## Credential Endpoint (Security Fix)

`tenant_credential_list_view` POST flow:

1. Receive `provider`, `tenant_id`, `tenant_name`, `credential` from request
2. **Verify credential against provider** (CommCare: call `/a/{domain}/api/v0.5/web-user/{username}/` with the API key)
3. On verification success:
   - `Tenant.objects.update_or_create(provider=provider, external_id=tenant_id, defaults={"canonical_name": verified_name})`
   - `TenantMembership.objects.get_or_create(user=user, tenant=tenant)`
   - Store encrypted credential on membership
4. On verification failure → HTTP 400, no records created

## OAuth Resolution (`tenant_resolution.py`)

`resolve_commcare_domains` and `resolve_connect_opportunities` upsert `Tenant` first, then upsert `TenantMembership` referencing the `Tenant`:

```python
tenant, _ = Tenant.objects.update_or_create(
    provider="commcare",
    external_id=domain["domain_name"],
    defaults={"canonical_name": domain["project_name"]},
)
tm, _ = TenantMembership.objects.update_or_create(
    user=user,
    tenant=tenant,
)
```

## API Response Compatibility

`tenant_list_view` and `tenant_credential_list_view` still return `tenant_id` (mapped from `tenant.external_id`) and `tenant_name` (mapped from `tenant.canonical_name`) — no frontend changes required.

## Migrations

1. Add `Tenant` table
2. Update `TenantMembership`: add `tenant` FK, remove `provider`/`tenant_id`/`tenant_name`
3. Update `TenantWorkspace`: add `tenant` OneToOneField, remove `tenant_id`/`tenant_name` CharFields

No data migration needed (no production data).

## Testing

- Update `test_share_api.py` and `test_artifact_query_data.py` factories to use `Tenant` + updated `TenantMembership`
- Add test: credential endpoint rejects unverified/bad credentials
- Add test: credential endpoint with valid credential creates `Tenant` and `TenantMembership`
- Add test: cross-tenant access denied even when attacker knows the `external_id`
