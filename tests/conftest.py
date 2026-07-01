import pytest

BASE_URL = "https://api.test.biaspay.com"


def customer_obj(id="cus_1", name="Acme", email="acme@example.com"):
    return {
        "object": "customer",
        "live": False,
        "id": id,
        "created_at": 1700000000000,
        "updated_at": 1700000000000,
        "deleted": False,
        "name": name,
        "email": email,
        "phone": None,
        "external_sync": None,
        "metadata": {},
    }


def file_obj(id="file_1", name="doc.pdf"):
    return {
        "object": "file",
        "live": False,
        "id": id,
        "created_at": 1700000000000,
        "name": name,
        "purpose": "invoice_attachment",
        "size": 1234,
    }


def event_obj(id="evt_1", type="customer.created"):
    return {
        "object": "event",
        "live": False,
        "id": id,
        "created_at": 1700000000000,
        "type": type,
        "state": customer_obj(),
        "previous_state": None,
        "request": None,
        "context": None,
    }


def error_body(type, *, code=None, message="boom", param=None, live=False):
    err = {"type": type, "message": message, "live": live}
    if code is not None:
        err["code"] = code
    if param is not None:
        err["param"] = param
    return {"object": "error", "error": err}


@pytest.fixture
def base_url():
    return BASE_URL
