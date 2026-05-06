import pytest
import requests_mock as rm

from mcp_server.loaders.connect_assessments import ConnectAssessmentLoader
from mcp_server.loaders.connect_base import (
    EXPORT_ACCEPT_HEADER,
    ConnectAuthError,
    ConnectExportError,
)
from mcp_server.loaders.connect_completed_modules import ConnectCompletedModuleLoader
from mcp_server.loaders.connect_completed_works import ConnectCompletedWorkLoader
from mcp_server.loaders.connect_invoices import ConnectInvoiceLoader
from mcp_server.loaders.connect_payments import ConnectPaymentLoader
from mcp_server.loaders.connect_users import ConnectUserLoader
from mcp_server.loaders.connect_visits import ConnectVisitLoader, _normalize_visit

BASE = "https://connect.example.com"
CRED = {"type": "oauth", "value": "test-token"}
OPP_ID = 814


def _make_loader(cls):
    return cls(opportunity_id=OPP_ID, credential=CRED, base_url=BASE)


def _page(results, next_url=None):
    """Build a v2 paginated JSON response body."""
    return {"next": next_url, "previous": None, "results": results}


# ---------------------------------------------------------------------------
# Visit loader tests
# ---------------------------------------------------------------------------


class TestConnectVisitLoader:
    @pytest.fixture
    def loader(self):
        return _make_loader(ConnectVisitLoader)

    def _visit_record(self, **overrides):
        # Mirrors what DRF's UserVisitDataSerializer actually produces. FK
        # fields (deliver_unit, completed_work) are ints — the default
        # ModelSerializer renders them as the related PK. flag_reason is a
        # JSONField, so it arrives as a dict or None. form_json/images
        # already arrive as dict/list.
        record = {
            "id": 1,
            "opportunity_id": 814,
            "username": "alice",
            "deliver_unit": 42,
            "entity_id": "e1",
            "entity_name": "Entity One",
            "visit_date": "2025-01-01T12:34:56Z",
            "status": "approved",
            "reason": None,
            "location": "loc1",
            "flagged": False,
            "flag_reason": None,
            "form_json": {"q1": "yes"},
            "completed_work": 7,
            "status_modified_date": "2025-01-02T00:00:00Z",
            "review_status": "approved",
            "review_created_on": "2025-01-03T00:00:00Z",
            "justification": None,
            "date_created": "2025-01-01T00:00:00Z",
            "completed_work_id": 7,
            "deliver_unit_id": 42,
            "images": [],
        }
        record.update(overrides)
        return record

    def test_load_pages(self, loader):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([self._visit_record()]),
            )
            pages = list(loader.load_pages())
            assert len(pages) == 1
            page, _total = pages[0]
            assert len(page) == 1
            row = page[0]
            assert row["visit_id"] == 1
            assert row["username"] == "alice"
            assert "id" not in row

    def test_form_json_passes_through_as_dict(self, loader):
        """v2 JSON returns form_json as a real dict — no parsing required."""
        record = self._visit_record(form_json={"name": "test", "active": True})
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([record]),
            )
            rows = loader.load()
            assert rows[0]["form_json"] == {"name": "test", "active": True}

    def test_form_json_missing_defaults_to_empty_dict(self):
        result = _normalize_visit({"id": 5}, opportunity_id=814)
        assert result["form_json"] == {}
        assert result["images"] == []

    def test_form_json_non_dict_coerced_to_empty(self):
        """Defensive: if upstream ever sends a non-dict for form_json, fall back to {}."""
        result = _normalize_visit(
            {"id": 5, "form_json": "broken", "images": "broken"},
            opportunity_id=814,
        )
        assert result["form_json"] == {}
        assert result["images"] == []

    def test_id_renamed_to_visit_id(self, loader):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([self._visit_record(id=99, username="bob")]),
            )
            rows = loader.load()
            assert rows[0]["visit_id"] == 99
            assert "id" not in rows[0]

    def test_opportunity_id_falls_back_to_loader(self, loader):
        """Some serializers omit opportunity_id from per-row data. Falls back
        to the loader's own opportunity_id (which is the int from the URL path)."""
        record = self._visit_record()
        del record["opportunity_id"]
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([record]),
            )
            rows = loader.load()
            assert rows[0]["opportunity_id"] == 814
            assert isinstance(rows[0]["opportunity_id"], int)

    def test_flagged_bool_passes_through(self, loader):
        """v2 JSON returns booleans as real bools; we preserve them for the
        BOOLEAN column in raw_visits.flagged (no more 'True'/'False' strings)."""
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([self._visit_record(flagged=True)]),
            )
            rows = loader.load()
            assert rows[0]["flagged"] is True
            assert isinstance(rows[0]["flagged"], bool)

    def test_flagged_false_not_coerced_to_none(self, loader):
        """Guards against the `or` falsy-trap: ``False or default`` returns
        default, which would wrongly replace a real False with None."""
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([self._visit_record(flagged=False)]),
            )
            rows = loader.load()
            assert rows[0]["flagged"] is False

    def test_missing_datetime_field_is_none(self, loader):
        """Missing nullable datetime fields arrive as None, not empty string,
        so psycopg can bind them to NULL for the TIMESTAMPTZ column."""
        record = self._visit_record()
        record["visit_date"] = None
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([record]),
            )
            rows = loader.load()
            assert rows[0]["visit_date"] is None

    def test_flag_reason_dict_passes_through(self, loader):
        """UserVisit.flag_reason is a Django JSONField; the v2 serializer
        emits it as a dict. The writer stores it in JSONB via json.dumps.
        Regression pin for the tenant 765 failure (psycopg couldn't adapt
        a dict to the old TEXT column)."""
        record = self._visit_record(
            flagged=True,
            flag_reason={"rule": "location_mismatch", "distance_m": 420},
        )
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([record]),
            )
            rows = loader.load()
            assert rows[0]["flag_reason"] == {"rule": "location_mismatch", "distance_m": 420}
            assert isinstance(rows[0]["flag_reason"], dict)

    def test_fk_ids_are_ints(self, loader):
        """DRF's ModelSerializer renders ForeignKey fields as the related
        PK (int), not a string. The writer's BIGINT columns bind ints
        directly via psycopg."""
        record = self._visit_record(deliver_unit=42, completed_work=7)
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([record]),
            )
            rows = loader.load()
            assert rows[0]["deliver_unit"] == 42
            assert rows[0]["deliver_unit_id"] == 42
            assert rows[0]["completed_work"] == 7
            assert rows[0]["completed_work_id"] == 7
            assert isinstance(rows[0]["deliver_unit"], int)
            assert isinstance(rows[0]["completed_work_id"], int)

    def test_empty_results_yields_nothing(self, loader):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([]),
            )
            pages = list(loader.load_pages())
            assert pages == []

    def test_pagination_follows_next_url(self, loader):
        """Loader must follow ``next`` until null and aggregate all pages."""
        first_url = f"{BASE}/export/opportunity/{OPP_ID}/user_visits/"
        next_url = f"{first_url}?last_id=1"

        with rm.Mocker() as m:
            m.get(first_url, json=_page([self._visit_record(id=1)], next_url=next_url))
            m.get(next_url, json=_page([self._visit_record(id=2, username="bob")]))

            rows = loader.load()
            assert [r["visit_id"] for r in rows] == [1, 2]
            assert [r["username"] for r in rows] == ["alice", "bob"]

    def test_sends_versioned_accept_header(self, loader):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([self._visit_record()]),
            )
            loader.load()
            assert m.last_request.headers["Accept"] == EXPORT_ACCEPT_HEADER

    def test_sends_bearer_auth(self, loader):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json=_page([self._visit_record()]),
            )
            loader.load()
            assert m.last_request.headers["Authorization"] == "Bearer test-token"

    def test_auth_error_on_401(self, loader):
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/user_visits/", status_code=401)
            with pytest.raises(ConnectAuthError):
                loader.load()

    def test_export_error_on_missing_results_key(self, loader):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/user_visits/",
                json={"next": None},
            )
            with pytest.raises(ConnectExportError):
                loader.load()


# ---------------------------------------------------------------------------
# Simple loader tests
# ---------------------------------------------------------------------------
#
# These all share the same shape — paginate JSON, yield pages unchanged so
# native Python types reach the writer's typed columns directly. The
# parameterized matrix captures one realistic record per endpoint plus the
# key/value pair we sample to confirm the row survived a round-trip.


# Each fixture mirrors the REAL shape produced by the DRF serializers in
# commcare_connect.data_export.serializer. Key gotchas:
#   - ForeignKey fields render as the related PK int, not a string
#   - payment_accrued (user) is IntegerField → int, not a decimal string
#   - claim_limits (user) is a SerializerMethodField returning list[dict]
#   - service_delivery (invoice) is a BooleanField on PaymentInvoice, not a label
#   - exchange_rate (invoice) is a FK to ExchangeRate → int PK, not the rate
#   - duration (completed_module) is DurationField → string like "0:30:00"
SIMPLE_LOADER_CASES = [
    (
        ConnectUserLoader,
        "user_data/",
        {
            "username": "alice",
            "name": "Alice Smith",
            "phone": "555-0001",
            "date_learn_started": "2025-01-01T00:00:00Z",
            "user_invite_status": "accepted",
            "payment_accrued": 100,
            "suspended": False,
            "suspension_date": None,
            "suspension_reason": None,
            "invited_date": "2025-01-01T00:00:00Z",
            "completed_learn_date": None,
            "last_active": "2025-06-01T00:00:00Z",
            "date_claimed": None,
            "claim_limits": [
                {"payment_unit": "pu1", "max_visits": 50},
                {"payment_unit": "pu2", "max_visits": 100},
            ],
        },
        ("username", "alice"),
    ),
    (
        ConnectCompletedWorkLoader,
        "completed_works/",
        {
            "username": "alice",
            "opportunity_id": 814,
            "payment_unit_id": 11,
            "status": "approved",
            "last_modified": "2025-01-01T00:00:00Z",
            "entity_id": "e1",
            "entity_name": "Entity",
            "reason": None,
            "status_modified_date": None,
            "payment_date": "2025-01-02T00:00:00Z",
            "date_created": "2025-01-01T00:00:00Z",
            "saved_completed_count": 5,
            "saved_approved_count": 5,
            "saved_payment_accrued": 50,
            "saved_payment_accrued_usd": "10.00",
            "saved_org_payment_accrued": 50,
            "saved_org_payment_accrued_usd": "10.00",
        },
        ("status", "approved"),
    ),
    (
        ConnectPaymentLoader,
        "payment/",
        {
            "username": "alice",
            "opportunity_id": 814,
            "created_at": "2025-01-01T00:00:00Z",
            "amount": "100.00",
            "amount_usd": "20.00",
            "date_paid": "2025-01-02T00:00:00Z",
            "payment_unit": 11,
            "confirmed": True,
            "confirmation_date": "2025-01-02T00:00:00Z",
            "organization": "dimagi",
            "invoice_id": 77,
            "payment_method": "mobile",
            "payment_operator": "op1",
        },
        ("amount", "100.00"),
    ),
    (
        ConnectInvoiceLoader,
        "invoice/",
        {
            "opportunity_id": 814,
            "amount": "500.00",
            "amount_usd": "100.00",
            "date": "2025-01-01",
            "invoice_number": "INV-001",
            "service_delivery": True,
            "exchange_rate": 3,
        },
        ("invoice_number", "INV-001"),
    ),
    (
        ConnectAssessmentLoader,
        "assessment/",
        {
            "username": "alice",
            "app": 9,
            "opportunity_id": 814,
            "date": "2025-01-01T00:00:00Z",
            "score": 85,
            "passing_score": 70,
            "passed": True,
        },
        ("score", 85),
    ),
    (
        ConnectCompletedModuleLoader,
        "completed_module/",
        {
            "username": "alice",
            "module": 5,
            "opportunity_id": 814,
            "date": "2025-01-01T00:00:00Z",
            "duration": "0:30:00",
        },
        ("module", 5),
    ),
]


@pytest.mark.parametrize(
    ("loader_cls", "endpoint", "record", "expected"),
    SIMPLE_LOADER_CASES,
    ids=[c[0].__name__ for c in SIMPLE_LOADER_CASES],
)
class TestSimpleLoaders:
    def test_load_single_page(self, loader_cls, endpoint, record, expected):
        loader = _make_loader(loader_cls)
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/{endpoint}",
                json=_page([record]),
            )
            rows = loader.load()
            assert len(rows) == 1
            key, value = expected
            assert rows[0][key] == value

    def test_native_types_preserved(self, loader_cls, endpoint, record, expected):
        """v2 JSON types flow through untouched — no stringification. The
        writer's typed columns bind native int/bool/None directly via psycopg."""
        loader = _make_loader(loader_cls)
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/{endpoint}",
                json=_page([record]),
            )
            rows = loader.load()
            # Every value in the returned row must match the type of the
            # corresponding value in the original record — i.e. the loader
            # is a pass-through for the simple loaders.
            for key, original in record.items():
                assert rows[0][key] == original
                assert type(rows[0][key]) is type(original)

    def test_empty_results(self, loader_cls, endpoint, record, expected):
        loader = _make_loader(loader_cls)
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/{endpoint}",
                json=_page([]),
            )
            assert list(loader.load_pages()) == []

    def test_pagination_aggregates_pages(self, loader_cls, endpoint, record, expected):
        loader = _make_loader(loader_cls)
        first_url = f"{BASE}/export/opportunity/{OPP_ID}/{endpoint}"
        next_url = f"{first_url}?last_id=1"

        record2 = {**record}

        with rm.Mocker() as m:
            m.get(first_url, json=_page([record], next_url=next_url))
            m.get(next_url, json=_page([record2]))

            rows = loader.load()
            assert len(rows) == 2

    def test_sends_versioned_accept_header(self, loader_cls, endpoint, record, expected):
        loader = _make_loader(loader_cls)
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/export/opportunity/{OPP_ID}/{endpoint}",
                json=_page([record]),
            )
            loader.load()
            assert m.last_request.headers["Accept"] == EXPORT_ACCEPT_HEADER
