import pytest
import requests_mock as rm

from mcp_server.loaders.connect_assessments import ConnectAssessmentLoader
from mcp_server.loaders.connect_completed_modules import ConnectCompletedModuleLoader
from mcp_server.loaders.connect_completed_works import ConnectCompletedWorkLoader
from mcp_server.loaders.connect_invoices import ConnectInvoiceLoader
from mcp_server.loaders.connect_payments import ConnectPaymentLoader
from mcp_server.loaders.connect_users import ConnectUserLoader
from mcp_server.loaders.connect_visits import (
    ConnectVisitLoader,
    _normalize_visit,
    _parse_json_field,
)

BASE = "https://connect.example.com"
CRED = {"type": "oauth", "value": "test-token"}
OPP_ID = 814


def _make_loader(cls):
    return cls(opportunity_id=OPP_ID, credential=CRED, base_url=BASE)


# ---------------------------------------------------------------------------
# Visit loader tests
# ---------------------------------------------------------------------------

class TestConnectVisitLoader:
    @pytest.fixture
    def loader(self):
        return _make_loader(ConnectVisitLoader)

    def test_load_pages(self, loader):
        csv_text = (
            "id,opportunity_id,username,deliver_unit,entity_id,entity_name,"
            "visit_date,status,reason,location,flagged,flag_reason,form_json,"
            "completed_work,status_modified_date,review_status,review_created_on,"
            "justification,date_created,completed_work_id,deliver_unit_id,images\n"
            '1,814,alice,du1,e1,Entity One,2025-01-01,approved,,loc1,False,,'
            '"{""q1"": ""yes""}",cw1,2025-01-02,approved,2025-01-03,,2025-01-01,cw1,du1,[]\n'
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/user_visits/", text=csv_text)
            pages = list(loader.load_pages())
            assert len(pages) == 1
            assert len(pages[0]) == 1
            row = pages[0][0]
            assert row["visit_id"] == "1"
            assert row["username"] == "alice"
            assert "id" not in row

    def test_load_convenience(self, loader):
        csv_text = (
            "id,opportunity_id,username,deliver_unit,entity_id,entity_name,"
            "visit_date,status,reason,location,flagged,flag_reason,form_json,"
            "completed_work,status_modified_date,review_status,review_created_on,"
            "justification,date_created,completed_work_id,deliver_unit_id,images\n"
            '1,814,alice,du1,e1,Entity One,2025-01-01,approved,,loc1,False,,'
            '"{""q1"": ""yes""}",cw1,2025-01-02,approved,2025-01-03,,2025-01-01,cw1,du1,[]\n'
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/user_visits/", text=csv_text)
            rows = loader.load()
            assert len(rows) == 1

    def test_form_json_python_repr_parsing(self):
        raw = {
            "id": "42",
            "form_json": "{'name': 'test', 'active': True}",
            "images": "[]",
        }
        result = _normalize_visit(raw)
        assert result["form_json"] == {"name": "test", "active": True}
        assert result["visit_id"] == "42"

    def test_form_json_standard_json(self):
        raw = {
            "id": "5",
            "form_json": '{"name": "test"}',
            "images": "[]",
        }
        result = _normalize_visit(raw)
        assert result["form_json"] == {"name": "test"}

    def test_form_json_empty(self):
        raw = {"id": "5", "form_json": "", "images": ""}
        result = _normalize_visit(raw)
        assert result["form_json"] == {}
        assert result["images"] == {}

    def test_parse_json_field_invalid(self):
        assert _parse_json_field("not valid {{{") == {}

    def test_id_renamed_to_visit_id(self, loader):
        csv_text = (
            "id,opportunity_id,username,deliver_unit,entity_id,entity_name,"
            "visit_date,status,reason,location,flagged,flag_reason,form_json,"
            "completed_work,status_modified_date,review_status,review_created_on,"
            "justification,date_created,completed_work_id,deliver_unit_id,images\n"
            "99,814,bob,du2,e2,Ent2,2025-02-01,pending,,,,,,,,,,,,,,\n"
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/user_visits/", text=csv_text)
            rows = loader.load()
            assert rows[0]["visit_id"] == "99"
            assert "id" not in rows[0]

    def test_empty_csv_yields_nothing(self, loader):
        csv_text = (
            "id,opportunity_id,username,deliver_unit,entity_id,entity_name,"
            "visit_date,status,reason,location,flagged,flag_reason,form_json,"
            "completed_work,status_modified_date,review_status,review_created_on,"
            "justification,date_created,completed_work_id,deliver_unit_id,images\n"
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/user_visits/", text=csv_text)
            pages = list(loader.load_pages())
            assert pages == []

    def test_chunking_large_dataset(self, loader):
        """Visits with >1000 rows should be yielded in chunks of 1000."""
        header = (
            "id,opportunity_id,username,deliver_unit,entity_id,entity_name,"
            "visit_date,status,reason,location,flagged,flag_reason,form_json,"
            "completed_work,status_modified_date,review_status,review_created_on,"
            "justification,date_created,completed_work_id,deliver_unit_id,images"
        )
        rows = [
            f"{i},814,user{i},du,e,Ent,2025-01-01,approved,,,,,,,,,,,,,,[]"
            for i in range(1, 1502)
        ]
        csv_text = header + "\n" + "\n".join(rows) + "\n"
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/user_visits/", text=csv_text)
            pages = list(loader.load_pages())
            assert len(pages) == 2
            assert len(pages[0]) == 1000
            assert len(pages[1]) == 501


# ---------------------------------------------------------------------------
# Simple loader tests (users, completed_works, payments, invoices,
#                       assessments, completed_modules)
# ---------------------------------------------------------------------------

class TestConnectUserLoader:
    @pytest.fixture
    def loader(self):
        return _make_loader(ConnectUserLoader)

    def test_load_pages(self, loader):
        csv_text = (
            "username,name,phone,date_learn_started,user_invite_status,"
            "payment_accrued,suspended,suspension_date,suspension_reason,"
            "invited_date,completed_learn_date,last_active,date_claimed,claim_limits\n"
            "alice,Alice Smith,555-0001,2025-01-01,accepted,100,False,,,2025-01-01,,2025-06-01,,\n"
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/user_data/", text=csv_text)
            pages = list(loader.load_pages())
            assert len(pages) == 1
            assert pages[0][0]["username"] == "alice"

    def test_load(self, loader):
        csv_text = "username,name,phone\nalice,Alice,555\n"
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/user_data/", text=csv_text)
            rows = loader.load()
            assert len(rows) == 1

    def test_empty(self, loader):
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/user_data/", text="username,name\n")
            assert list(loader.load_pages()) == []


class TestConnectCompletedWorkLoader:
    @pytest.fixture
    def loader(self):
        return _make_loader(ConnectCompletedWorkLoader)

    def test_load_pages(self, loader):
        csv_text = (
            "username,opportunity_id,payment_unit_id,status,last_modified,entity_id,"
            "entity_name,reason,status_modified_date,payment_date,date_created,"
            "saved_completed_count,saved_approved_count,saved_payment_accrued,"
            "saved_payment_accrued_usd,saved_org_payment_accrued,saved_org_payment_accrued_usd\n"
            "alice,814,pu1,approved,2025-01-01,e1,Entity,,,2025-01-02,2025-01-01,5,5,50,10,50,10\n"
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/completed_works/", text=csv_text)
            pages = list(loader.load_pages())
            assert len(pages) == 1
            assert pages[0][0]["username"] == "alice"


class TestConnectPaymentLoader:
    @pytest.fixture
    def loader(self):
        return _make_loader(ConnectPaymentLoader)

    def test_load_pages(self, loader):
        csv_text = (
            "username,opportunity_id,created_at,amount,amount_usd,date_paid,"
            "payment_unit,confirmed,confirmation_date,organization,invoice_id,"
            "payment_method,payment_operator\n"
            "alice,814,2025-01-01,100,20,2025-01-02,pu1,True,2025-01-02,dimagi,inv1,mobile,op1\n"
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/payment/", text=csv_text)
            pages = list(loader.load_pages())
            assert len(pages) == 1
            assert pages[0][0]["amount"] == "100"


class TestConnectInvoiceLoader:
    @pytest.fixture
    def loader(self):
        return _make_loader(ConnectInvoiceLoader)

    def test_load_pages(self, loader):
        csv_text = (
            "opportunity_id,amount,amount_usd,date,invoice_number,"
            "service_delivery,exchange_rate\n"
            "814,500,100,2025-01-01,INV-001,sd1,5.0\n"
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/invoice/", text=csv_text)
            pages = list(loader.load_pages())
            assert len(pages) == 1
            assert pages[0][0]["invoice_number"] == "INV-001"


class TestConnectAssessmentLoader:
    @pytest.fixture
    def loader(self):
        return _make_loader(ConnectAssessmentLoader)

    def test_load_pages(self, loader):
        csv_text = (
            "username,app,opportunity_id,date,score,passing_score,passed\n"
            "alice,app1,814,2025-01-01,85,70,True\n"
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/assessment/", text=csv_text)
            pages = list(loader.load_pages())
            assert len(pages) == 1
            assert pages[0][0]["score"] == "85"


class TestConnectCompletedModuleLoader:
    @pytest.fixture
    def loader(self):
        return _make_loader(ConnectCompletedModuleLoader)

    def test_load_pages(self, loader):
        csv_text = (
            "username,module,opportunity_id,date,duration\n"
            "alice,mod1,814,2025-01-01,30\n"
        )
        with rm.Mocker() as m:
            m.get(f"{BASE}/export/opportunity/{OPP_ID}/completed_module/", text=csv_text)
            pages = list(loader.load_pages())
            assert len(pages) == 1
            assert pages[0][0]["module"] == "mod1"
