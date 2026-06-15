# Gap Round 1 — Upstream Provider Contract Verifier

*Mandate: verify the loaders vertical's inferred upstream-API claims against actual provider
source/docs (commcare-connect, commcare-hq, open-chat-studio) instead of this repo's comments.
Date: 2026-06-12. Reviewer: gap1-upstream-provider-contract-verifier.*

**Method.** Each Scout-side assumption was stated from the loader code first
(`mcp_server/loaders/*`, `pipelines/*.yml`, the relevant writer SQL in
`mcp_server/services/materializer.py`), then checked against upstream **source fetched verbatim**
(curl against `raw.githubusercontent.com` for `dimagi/commcare-connect`, `dimagi/commcare-hq`,
`dimagi/open-chat-studio` at `main`/`master` HEAD on 2026-06-12) plus published docs
(commcare-hq.readthedocs.io). Verdicts: CONFIRMED / REFUTED / UNVERIFIABLE.

**Standing caveat.** All upstream evidence is the *current default branch*. Deployed
production versions of Connect/HQ/OCS may lag or lead it. Where a claim's truth changed
recently upstream (e.g., OCS pagination `count`), I say so.

---

## Part 1 — CommCare Connect (`commcare_connect/data_export/`)

Scout side: `mcp_server/loaders/connect_base.py`, `connect_visits.py` (+ 6 sibling loaders),
`pipelines/connect_sync.yml`, writers in `materializer.py` (`_CONNECT_VISITS_INSERT` ~L1263,
`ON CONFLICT (visit_id)`).

### 1.1 Per-row `id` on visits; no `id` on the other six resources — **CONFIRMED (verbatim)**

Upstream `commcare_connect/data_export/serializer.py` (curl'd verbatim):

- `UserVisitDataSerializer` (L95–): `model = UserVisit`, `fields = ["id", "opportunity_id",
  "username", ...]` — **per-row `id` (the model auto-increment pk) is emitted**. This is the
  basis of Scout's `visit_id` rename (`connect_visits.py:44`), the `ON CONFLICT (visit_id)`
  dedup, and the keyset resume watermark (`_max_id(page, "visit_id")`, materializer ~L1489).
- `OpportunityUserDataSerializer` (L72, plain `serializers.Serializer`),
  `CompletedWorkDataSerializer` (L141), `PaymentDataSerializer` (L174),
  `InvoiceDataSerializer` (L204), `AssessmentDataSerializer` (L218) — **none emit `id`**
  (verbatim field lists; `CompletedModuleDataSerializer` confirmed no-`id` via a second
  independent fetch of the same file).

**Effect on existing findings:** the `pipelines/connect_sync.yml` comment ("The v2 export
serializers for the resources below do not emit a per-row `id`") is **accurate**, and the
`resumable: false` flags are correctly motivated. The basis of the F4-class finding
("Connect resumable writers can duplicate on stale-cursor replay") is confirmed: replay from a
stale cursor re-serves rows with `id > stale_cursor`, and only writers with a pk conflict
target (visits) dedup them; the id-less sources *cannot* be made resumable safely — which makes
the already-reported "resumability registry default-True" finding's guard-rail role concrete.

### 1.2 Pagination contract the cursor watermark assumes — **CONFIRMED (verbatim)**

Upstream `data_export/pagination.py`, class `IdKeysetPagination` (curl'd verbatim):

```python
queryset = queryset.order_by("id" if is_forward else "-id")
if self.last_id is not None:
    queryset = queryset.filter(id__gt=self.last_id) if is_forward else ...
```

- `last_id` keyset on integer pk, **forward = ascending, strictly `id > last_id`** — exactly
  what `connect_base.py:160-166` documents and what the resume path (#187) assumes.
- `default_page_size = 1000`, `max_page_size = 5000` — Scout's "~1000 records" comment correct.
- `get_next_link()` preserves all query params and rewrites `last_id` to the page's last item id
  — Scout's "next URL already preserves all original params" (`connect_base.py:235-236`) correct.
- `last_id` is validated `min_value=1`; a malformed value yields a DRF 400 (not in Scout's retry
  forcelist → fails fast with `attempts=1`). Scout never sends 0/negatives. Fine.

### 1.3 `count` field — **REFUTED: upstream never sends it**

Upstream `get_paginated_response` (verbatim):

```python
return Response({"next": self.get_next_link(), "results": data})
```

**There is no `count` key, ever.** Scout's `connect_base.py:225-229` reads
`payload.get("count")` and its docstring asserts "`total_count` is the `count` field from the
first response" (`connect_base.py:165-167`); every Connect loader threads `page_total` through
to the materializer's `rows_total`. All of it is dead: **Connect totals are always `None` and
Connect materialization progress is always indeterminate.** Behaviorally this is honest
(indeterminate, not fake), but the docstrings describe a field the provider has never emitted
on this code path — comments-as-claims failure. Notably the OCS team *did* add a first-page
`count` upstream explicitly for Scout (see 3.4); nobody asked Connect for the same.

### 1.4 Endpoint suffixes — **CONFIRMED (verbatim)**

Upstream `data_export/urls.py` vs Scout loaders: `user_visits/` ✓ (`connect_visits.py:77`),
`user_data/` ✓ (`connect_users.py:23`), `completed_works/` ✓, `payment/` ✓ (singular),
`invoice/` ✓, `assessment/` ✓, `completed_module/` ✓, `opp_org_program_list/` ✓,
`opportunity/<int:opp_id>/` ✓. No drift.

### 1.5 `next` URL scheme/host trust and dimagi/commcare-connect#1109 — **CONFIRMED**

- `get_next_link()` uses `self.request.build_absolute_uri(...)` — the `next` host/scheme come
  from the **incoming request as seen by gunicorn**, i.e. from proxy headers.
- **#1109 is real and was merged**: gunicorn's default `--forwarded-allow-ips` (loopback only)
  stripped `X-Forwarded-Proto: https` from Traefik (different container IP), so
  `request.scheme` was `http` and production emitted `http://` next URLs. Fixed upstream with
  `--forwarded-allow-ips="*"`.
- Severity texture for the known "loaders follow server-supplied next URLs anywhere with
  session-pinned credentials" finding: during the #1109 window, Scout's
  `connect_base.py:195-197` (`self._session.get(url, ...)` with `allow_redirects=True` default)
  issued the **plaintext `http://` request first, Bearer header attached**, and only then
  followed the 301 to https (requests keeps the Authorization header across a same-host
  http→https upgrade). So the failure mode is not hypothetical: bearer tokens transited
  plaintext for one hop to the edge over the public internet whenever upstream regressed. The
  NOTE comment at `connect_base.py:187-194` frames redirect-following as the fix; it is
  actually the hazard. A scheme/host pin (or refusing `http://` next URLs) on the Scout side
  would close the recurrence class regardless of upstream proxy configuration.

### 1.6 `images` column — **new minor finding: always empty**

Upstream `UserVisitDataView` (`data_export/views.py:257-266`): the `images` field is emitted
**only when `?images=true`** (`UserVisitDataWithImagesSerializer`); the default serializer has
no `images` key. Scout never passes the param (`connect_visits.py:76-78` sends no params), yet:

- `_normalize_visit` maps `raw.get("images") or []` (`connect_visits.py:39-41, 65`);
- the writer creates an `images JSONB` column (materializer ~L1445) and even upserts it
  (`images=EXCLUDED.images`, ~L1273).

So `raw_visits.images` is **always `[]`** — an advertised, perpetually-empty column the agent
and analysts can see and reason about. Either pass `images=true` (with payload-size eyes open)
or drop the column.

### 1.7 Throttling — none upstream

No throttle/rate-limit machinery in `data_export/views.py` or DRF config visible for these
views. Connect's existing 4-attempt retry with `Retry-After` respect
(`connect_base.py:36-37, 61-69`) is adequate. Contrast with HQ below.

---

## Part 2 — CommCare HQ (Case API v2, Forms API v0.5)

Scout side: `mcp_server/loaders/commcare_base.py`, `commcare_cases.py`, `commcare_forms.py`,
`pipelines/commcare_sync.yml`, writers `ON CONFLICT (case_id)` / `(form_id)`
(materializer L1088/L1103).

### 2.1 Case API v2 envelope — **CONFIRMED**

Docs (commcare-hq.readthedocs.io/api/cases-v2.html) + source
(`corehq/apps/hqcase/api/get_list.py`): envelope is `{"cases": [...], "matching_records": N,
"next": ...}`; no tastypie `meta`. Matches `commcare_cases.py:38-57` exactly, including
`matching_records` as the only total. Case object fields (case_id, case_type, case_name,
external_id, owner_id, date_opened, last_modified, server_last_modified, indexed_on, closed,
date_closed, properties, indices) all documented — `_normalize_case` reads real fields.
`limit` max is 5000; Scout's 1000 is fine.

### 2.2 "Can Case v2 return relative next URLs?" (F7 trigger) — **REFUTED for v2**

Chain, all verbatim from `dimagi/commcare-hq@master`:

1. `corehq/apps/hqcase/views.py` `_handle_list_view`:
   `res['next'] = reverse('case_api', args=[request.domain], params=res['next'], absolute=True)`
2. `corehq/util/view_utils.py::reverse`: `if absolute: url = "{}{}".format(get_url_base(), url)`
3. `dimagi/utils/web.py`: `get_url_base() = '{}://{}'.format(settings.DEFAULT_PROTOCOL,
   settings.BASE_ADDRESS)`

So Case v2 `next` is **always absolute and built from server settings, not request headers**
(`https://www.commcarehq.org` in production — not even Host-header-influenceable, unlike
Connect). `commcare_cases.py:68` follows `data.get("next")` directly without
`_resolve_next_url` — harmless today precisely because of the above; it would break only if
HQ changed the contract. The internal `get_list.py` cursor is a base64-encoded query string
(`indexed_on.gte` + `last_case_id`), but the view layer absolutizes it before clients see it.

**However, the relative-next claim is CONFIRMED for Forms v0.5**: `XFormInstanceResource`
(`corehq/apps/api/resources/v0_4.py:64-171`) is a tastypie resource with no paginator
override (`Meta(CustomResourceMeta)`; the `DoesNothingPaginatorCompat` at v0_4.py:329 belongs
to the *application* resource). Tastypie's default `Paginator._generate_uri` emits
**path-relative** next links (`/a/<domain>/api/v0.5/form/?limit=...&offset=...`)
(strong-inference: stock tastypie behavior, no HQ override found). Scout's
`commcare_forms.py:70` correctly routes through `_resolve_next_url`. So
`commcare_base.py:49-59`'s docstring ("several formats") is accurate **across** APIs; the
asymmetry (cases doesn't call the resolver, forms does) happens to match upstream reality.

### 2.3 Pagination under live writes — **REFUTED for cases, REFINED for forms**

This corrects the previously-reported "CommCare offset pagination under live writes can
silently skip records":

- **Cases v2 is NOT offset pagination.** It is keyset on `('@indexed_on', 'doc_id')`
  (`get_list.py`: `query.sort('@indexed_on').sort('doc_id', reset_sort=False)`; cursor carries
  `indexed_on.gte` + `last_case_id`). Docs state the guarantee explicitly: "you should obtain a
  complete set of results, ordered from oldest to newest. If any cases are updated during the
  data pull, they may appear again towards the end." Under live writes Case v2 **re-serves
  updated cases rather than skipping** — and Scout's `ON CONFLICT (case_id) DO UPDATE`
  (materializer L1088) absorbs the duplicates correctly. The skip claim does not apply to the
  cases loader; severity **lowered** for that half.
- **Forms v0.5 IS offset pagination**, ES-backed, default ordering
  `.order_by('-received_on')` (v0_4.py:157) — **newest first, descending**. Mechanism under
  live writes, precisely: new submissions insert at position 0 and shift the remainder *down*,
  so an offset walk re-serves rows (duplicates — absorbed by `ON CONFLICT (form_id)`); rows
  are *skipped* only when documents leave the result set mid-walk (form archiving/deletion,
  ES retention events) shifting rows *up*. So for forms the realistic failure is bounded:
  duplicates common (handled), silent skips possible but tied to concurrent archiving rather
  than ordinary submission traffic. The original finding stands for forms with a corrected,
  narrower mechanism; recommend re-titling it "Forms v0.5 offset walk: dup-heavy, skip-on-
  archive; Cases v2 unaffected."

### 2.4 HQ actively rate-limits both APIs — **CONFIRMED; raises the no-retry finding**

- Case v2: `@api_throttle` on `case_api` (`hqcase/views.py:94`);
  `corehq/apps/api/decorators.py:51-61` returns **HTTP 429 with a `Retry-After` header**.
- Forms v0.5: `CustomResourceMeta.throttle = get_hq_throttle()`
  (`corehq/apps/api/resources/meta.py:76-82`).
- The limits are real and not generous: `PerUserRateDefinition` = per-user ratio scaling of
  1000 events/day **plus a constant floor of per_second=1, per_minute=10, per_hour=30,
  per_day=50** (`meta.py:20-34`).

Scout's CommCare loaders (`commcare_base.py:61-69`) have **no retry adapter, no 429 handling,
no `Retry-After` respect** — `resp.raise_for_status()` turns a designed, documented throttle
response into an unhandled `requests.HTTPError` that fails the source/run. A multi-thousand-page
sync of a large domain (1000 rows/page, page after page back-to-back) is exactly the traffic
shape this throttle exists to police. This upgrades the already-reported "Retry/error-shape
hardening applied only to Connect loaders" from *latent sibling-risk* to
*expected-in-production for large CommCare domains*, with the precise upstream contract
(429 + Retry-After) that the Connect retry policy already knows how to honor
(`respect_retry_after_header=True`, `connect_base.py:66`).

### 2.5 `matching_records` accuracy above 10k — **UNVERIFIABLE (hypothesis)**

`get_list.py` sets `matching_records = es_result.total`;
`corehq/apps/es/es_query.py:588-590` returns `self.raw['hits']['total']` raw. Whether HQ's ES
client requests exact totals (`track_total_hits`) or returns the ES-default 10,000-capped
value could not be traced in the time box (no `track_total_hits` hits in
`corehq/apps/es/{client,es_query,utils}.py`). Scout impact is limited: `page_total` feeds only
the progress denominator (`rows_total`, materializer L868-872), so worst case is an inaccurate
progress bar on >10k-case domains, not data loss. Flagging for the loaders vertical to ignore
unless progress-accuracy matters.

---

## Part 3 — Open Chat Studio (`apps/api/`)

Scout side: `mcp_server/loaders/ocs_base.py`, `ocs_participants.py`, `ocs_sessions.py`,
`ocs_messages.py`, `pipelines/ocs_sync.yml`.

### 3.1 **NEW, BROKEN-NOW: `/api/participants` ignores Scout's `chatbot` filter — every OCS participants sync pulls the whole team, unscoped**

Scout sends the filter the OCS OpenAPI schema documents; the OCS *implementation* reads a
different parameter name and silently ignores unknown ones.

Chain (Scout side):

1. `mcp_server/loaders/ocs_participants.py:44-47` — `url = f"{self.base_url}/api/participants"`,
   `params = {"chatbot": self.experiment_id}`, with the docstring claiming "the participant
   list (and each participant's `data` array) is scoped to this tenant's chatbot".
2. Reached from `pipelines/ocs_sync.yml` source `participants` on every OCS materialization.

Chain (upstream, `dimagi/open-chat-studio@main`, `apps/api/views/participants.py`, curl'd
verbatim 2026-06-12):

3. The `@extend_schema` decorator **documents** `OpenApiParameter(name="chatbot",
   description="Filter by chatbot public id; returns participants that have data for the
   chatbot.")`.
4. The `get()` body reads **only** `identifier`, `platform`, and `experiment`:

   ```python
   if experiment_uuid := _parse_experiment_uuid(request.query_params.get("experiment")):
       data_qs = data_qs.filter(experiment__public_id=experiment_uuid)
       qs = qs.filter(id__in=data_qs.values_list("participant_id", flat=True))
   ```

   `request.query_params.get("chatbot")` appears nowhere. DRF ignores unknown params.
5. Consequence: with the filter never applied, `qs = Participant.objects.filter(team=request.team)`
   and `data_qs = ParticipantData.objects...filter(team=request.team)` — **all participants of
   the whole OCS team, each serialized with their `data` entries for every chatbot in the
   team** (the unfiltered `data_qs` is prefetched into `ParticipantDetailSerializer.get_data`).
6. Scout writes all of it into this tenant's `raw_participants`
   (`_map_participant` keeps the full `data` array; writer `ON CONFLICT (participant_id)`,
   materializer ~L1379), inside a tenant schema whose boundary is *one chatbot*.

Impact: cross-chatbot data bleed — participant rosters and per-chatbot custom `data` payloads
(names, timezones, arbitrary key-values; classic PII territory) from *other* chatbots of the
same OCS team are materialized into a workspace that was only granted this one chatbot, and are
then queryable by the agent and workspace members. Row counts also inflate. For single-chatbot
teams the bug is invisible, which is presumably why it shipped.

Fix is one token on the Scout side (`params = {"experiment": self.experiment_id}` — the
working, undocumented param; `_parse_experiment_uuid` accepts the public UUID Scout already
holds). It is *also* an upstream doc/code bug (documented `chatbot` param unimplemented) worth
filing against open-chat-studio so the documented name doesn't get "fixed" later in a way that
breaks whichever param Scout standardizes on. Note the loaders' own docstring cites OCS PR
#3334 — the schema example in `ocs_participants.py:8-27` matches upstream exactly; only the
filter param is wrong. Status: BROKEN-NOW; impact: security (cross-boundary data exposure);
confidence: verified-by-trace (verbatim upstream source + Scout loader).

### 3.2 `/api/sessions` contract — **CONFIRMED (Scout is correct here)**

Upstream `apps/api/views/sessions.py` (verbatim):

- `experiment` query param → `queryset.filter(experiment__public_id=experiment_id)` with UUID
  validation — Scout's `params = {"experiment": self.experiment_id}` (`ocs_sessions.py:25`)
  is right. (The asymmetry with 3.1 — sessions uses `experiment`, participants *documents*
  `chatbot` — is the upstream inconsistency that bit Scout.)
- `lookup_field = "external_id"`, `lookup_url_kwarg = "id"` — Scout's detail fetch by the
  list's `id` (which `ExperimentSessionSerializer` sources from `external_id`) is correct.
- `retrieve` sets `include_messages=True` (`get_serializer`), so `GET /api/sessions/{id}/`
  includes `messages` with no extra param — `ocs_messages.py:47-49` is correct, and docs say
  messages are "ordered by the creation date" (serializer: `reversed(list(
  instance.chat.message_iterator()))` → ascending).
- **No standalone `/api/messages` endpoint exists** (`apps/api/urls.py` verbatim: only
  interactive `chat/...` endpoints). The N+1 session-detail walk in `ocs_messages.py` is
  essential complexity imposed by the provider, not accidental.
- Nested `participant` on the session list is `ParticipantSerializer` with
  `fields = ["identifier", "remote_id"]` — see 3.5.
- Ordering `-created_at` (newest first) with DRF cursor pagination: stable under live inserts
  (new sessions appear before the walk's start and are simply caught next run; no skips
  mid-walk). The OCS loaders' lack of any retry remains a gap for transient 5xx, but unlike
  HQ, **OCS has no application-level throttle** (`config/settings.py` REST_FRAMEWORK block has
  no throttle classes; none on the views) — so the OCS half of the "no retry" finding is
  *not* additionally aggravated by designed 429s. CommCare's half is (see 2.4).

### 3.3 Pagination class and page size — **CONFIRMED, with a cost note**

`ExperimentSessionViewSet` sets no `pagination_class`; settings provide
`DEFAULT_PAGINATION_CLASS = "apps.api.pagination.CursorPagination"` and **`PAGE_SIZE: 100`**
(`config/settings.py:427-450`). `ParticipantView` sets the same class explicitly. So OCS pages
are 100 rows by default; `page_size` is honored up to `max_page_size = 1500`
(`apps/api/pagination.py`). Scout never sends `page_size`
(`ocs_sessions.py:24-27`, `ocs_participants.py:44-50`) → the session-list walk costs ~10× the
HTTP requests of the loaders' Connect/CommCare mental model (1000/page). One param would cut
OCS list traffic by an order of magnitude. DEBT / cost-perf, minor.

### 3.4 `count` field — **CONFIRMED now exists (stale Scout docstring, self-healing code)**

`apps/api/pagination.py` (verbatim) now computes the total **on the first page only** and its
comment literally names Scout as the consumer:

```python
# Compute the total count on the first page only (no cursor param).
# Consumers syncing data (e.g. Scout) need the total once up front to
# show real progress; ...
```

`ocs_base._paginate` (`ocs_base.py:75-83`) already picks `count` off the first page, so
sessions and participants progress is now determinate with zero Scout changes — exactly the
"will pick a count up automatically if OCS ever adds one" escape hatch in the
`ocs_sessions.py:13-21` docstring. That docstring's "no count … always None today" and
`ocs_participants.py:49`'s "no count field, so totals are always None" are now stale
(cosmetic). Messages remain session-denominated (`progress_unit: sessions`,
`ocs_sync.yml`) which is still right — there is still no message total.

### 3.5 Nested participant has no `platform` → `raw_sessions.participant_platform` is always `''` — **CONFIRMED divergence (new minor finding)**

Upstream `apps/api/serializers.py` (verbatim): `ParticipantSerializer.fields =
["identifier", "remote_id"]`. Scout's `_map_session` reads
`participant.get("platform")` (`ocs_sessions.py:53`) — a key the provider never sends — so the
`participant_platform` column is silently empty for every row, while the session-level
`platform` field that *does* exist (`ExperimentSessionSerializer` fields include `"platform"`)
is discarded. Anyone filtering/joining on `participant_platform` gets garbage; the
sessions→participants join in `ocs_sync.yml` is by bare `identifier`, which is also the only
join key actually available — note OCS participants are unique per (team, platform,
identifier) (`ExperimentSessionCreateSerializer.create` uses `get_or_create(identifier=...,
team=..., platform=...)`), so identifier-only joins can conflate same-identifier participants
across platforms. Fix: map `participant_platform` from the session's own `platform` field, or
drop the column.

### 3.6 Session detail injects synthetic "summary" messages — **CONFIRMED upstream; destabilizes Scout's positional message ids (new latent finding)**

Upstream chain (verbatim): `ExperimentSessionSerializer.get_messages` →
`instance.chat.message_iterator()` → `apps/chat/models.py:73-81`:

```python
def message_iterator(self, with_summaries=True, ...):
    for message in queryset.iterator(100):
        yield message
        if with_summaries and message.summary:
            yield message.get_summary_message()
```

Default `with_summaries=True`, and `MessageSerializer.to_representation` explicitly handles
unsaved instances ("don't try and load tags if it isn't saved to the DB e.g. summary
messages"). So the messages array Scout consumes contains **synthetic, unsaved summary
messages interleaved with real ones**, and the interleaving *changes over time*: when OCS
later generates a summary for an old message (history compression), a new element appears in
the middle of the array.

Scout's `_map_message` builds `message_id = f"{session_id}:{index}"` from enumeration order
(`ocs_messages.py:50, 64-66`). Consequences:

- `raw_messages` counts and contents include machine-generated summaries indistinguishable
  from real traffic except via `metadata.compression_marker` — message-volume analytics
  overcount.
- `message_id`/`message_index` are **not stable across syncs** even for append-only chats.
  Today this is masked because OCS messages do a full DROP/CREATE/INSERT each run; but the
  already-reported "resumability truth lives in two registries with an unsafe default-True"
  finding gains a concrete blast radius here: if `ocs:messages` is ever treated as resumable
  (the default-True path), positional-id drift silently corrupts the table (same row content
  under shifting ids + `ON CONFLICT (message_id) DO UPDATE`, materializer L1368). The
  positional-id scheme is the latent hazard; pinning ids to a content hash or
  (session, created_at, role) would remove it.

### 3.7 Versioning — **CONFIRMED low risk**

`config/settings.py` (verbatim comment): "v1 is frozen against today's surface and is also
served under the unversioned /api/ alias (the permanent default); v2 (renamed surface + new
endpoints) lands in a later phase." Scout's unversioned calls are pinned to frozen v1 by
upstream policy. No action.

---

## Verdict summary vs existing findings

| Existing finding | Effect of this verification |
|---|---|
| "Retry/error-shape hardening applied only to Connect loaders; OCS/CommCare … no retry" | **RAISED for CommCare**: HQ throttles both APIs by design (429 + Retry-After, constant floor 10/min); large-domain syncs will trip it and fail the run. **Slightly lowered for OCS**: no app-level throttle upstream; only generic transients remain. |
| "CommCare offset pagination under live writes can silently skip records" | **REFUTED for Case v2** (keyset on indexed_on; dups-at-end by design, absorbed by upsert). **Refined for Forms v0.5**: `-received_on` DESC offset walk → inserts cause duplicates (handled), skips only via concurrent archiving/deletion. |
| "Loaders follow server-supplied next URLs anywhere with session-pinned credentials" | **Mechanism confirmed differs per provider**: Connect next = `build_absolute_uri` (proxy-header-derived; #1109 proved http:// emission in prod → Scout sent Bearer over plaintext first hop). Case v2 next = settings-derived absolute (not request-influenceable); Forms v0.5 next = relative (resolver needed and present). Recommend scheme/host pin on Scout side. |
| "Connect resumable writers can duplicate on stale-cursor replay" | **Basis confirmed verbatim** (strict `id > last_id` keyset on pk; visits-only `id`). |
| "Resumability truth … unsafe default-True" | **Blast radius enlarged**: OCS messages' positional ids (3.6) and the six id-less Connect sources (1.1) are both unsafe under any accidental resumable=True. |
| "Mid-rematerialization reads / progress honesty" norms | Connect progress denominators **never exist** (no `count` upstream, 1.3); OCS now provides first-page `count` added upstream for Scout (3.4). |

## New findings from this round

1. **OCS participants sync is team-wide, not chatbot-scoped** (3.1) — BROKEN-NOW / security.
2. **CommCare loaders will fail runs on designed HQ 429 throttling** (2.4) — LATENT / correctness (raises existing finding).
3. **`raw_visits.images` always empty** (1.6) — DEBT / correctness.
4. **`raw_sessions.participant_platform` always empty + identifier-only join ambiguity** (3.5) — DEBT / correctness.
5. **OCS synthetic summary messages + unstable positional message ids** (3.6) — LATENT / correctness.
6. **Connect `count` never sent; loader docs claim otherwise** (1.3) — COSMETIC.
7. **OCS page_size left at 100 default → ~10× list requests** (3.3) — DEBT / cost-perf.
8. **`matching_records` >10k accuracy unverified** (2.5) — hypothesis only.

## What's fine (verified healthy)

- Connect keyset pagination contract: Scout's resume watermark semantics match upstream
  verbatim (strict `id > last_id`, ascending, params preserved in `next`).
- `connect_sync.yml` resumable flags and their justifying comments are accurate against
  upstream serializers, field for field.
- All Connect endpoint suffixes match upstream `urls.py` exactly.
- Case API v2 loader: envelope, `matching_records`, field set, and follow-`next` behavior all
  match upstream; the missing `_resolve_next_url` call is harmless because v2 `next` is
  settings-derived absolute by construction.
- Forms loader correctly resolves relative tastypie `next` links via `_resolve_next_url`.
- OCS sessions loader: filter param, lookup field, detail-includes-messages, and the
  no-`/api/messages`-endpoint premise of the N+1 design are all correct against upstream.
- OCS `count` pickup is forward-compatible exactly as designed — upstream added the field and
  Scout consumed it with zero changes.
- Writers' `ON CONFLICT` keys (case_id, form_id, visit_id) align with the upstream identity
  semantics that actually exist (pk for visits, natural ids for HQ).

## Coverage log

**Deep-read (Scout):** `mcp_server/loaders/connect_base.py`, `connect_visits.py`,
`commcare_base.py`, `commcare_cases.py`, `commcare_forms.py`, `ocs_base.py`,
`ocs_participants.py`, `ocs_sessions.py`, `ocs_messages.py`, all three `pipelines/*.yml`;
targeted reads of `mcp_server/services/materializer.py` (watermark plumbing L33-90/L560-660/
L800-810, Connect/CommCare/OCS insert SQL L1080-1110/L1255-1290/L1340-1380/L1400-1490,
page_total consumption).

**Deep-read (upstream, verbatim via curl):** commcare-connect `data_export/{pagination,
serializer,urls,views(.partial)}.py`; commcare-hq `hqcase/views.py` (case_api decorators),
`hqcase/api/get_list.py` (via fetch), `api/resources/v0_4.py` (XFormInstanceResource),
`api/resources/meta.py`, `api/decorators.py` (api_throttle), `util/view_utils.py` (reverse),
`dimagi/utils/web.py`; open-chat-studio `apps/api/{pagination,urls,serializers}.py`,
`apps/api/views/{participants,sessions}.py`, `apps/chat/models.py` (message_iterator),
`config/settings.py` (REST_FRAMEWORK).

**Skimmed:** commcare-connect `data_export/views.py` (full, via summarizing fetch + verbatim
UserVisitDataView section); HQ cases-v2 readthedocs page; HQ `corehq/apps/es/{client,es_query,
utils}.py` (grep only); Scout `connect_users/payments/invoices/assessments/completed_modules/
completed_works.py` (suffix lines only); `ocs_experiments.py` not at all (see below).

**Not examined (honest gaps):**
- `mcp_server/loaders/{connect_metadata,commcare_metadata,ocs_metadata,ocs_experiments}.py`
  and their upstream counterparts (Connect `SingleOpportunityDataView` /
  `opp_org_program_list` response shapes, OCS experiments endpoint, HQ application-structure
  API) — metadata-path shape drift unverified.
- Upstream **deployed** versions: all verification is against `main`/`master` on 2026-06-12;
  production Connect/HQ/OCS may differ (notably whether OCS's first-page `count` and the
  participants endpoint are deployed yet).
- HQ ES total-hits exactness (`track_total_hits`) — left as hypothesis (2.5).
- Tastypie's `Paginator._generate_uri` source itself (relative-next claim rests on stock
  library behavior + absence of HQ override, not a quoted tastypie line).
- requests' `rebuild_auth` redirect header semantics — asserted from library knowledge, not
  re-traced this round.
- Connect `CompletedModuleDataSerializer` field list — two consistent fetch summaries, not in
  my verbatim curl window.
- OCS `Participant` model uniqueness constraint (platform+identifier+team inferred from
  `get_or_create` usage, model not opened).
- Scout-side consumers of `participant_platform` / `images` columns beyond the writers
  (whether any dbt model or prompt references them).
