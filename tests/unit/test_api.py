import uuid


def submit(client, headers, body):
    return client.post("/requests", json=body, headers=headers)


def test_submit_returns_202_and_queues(client, alice, valid_body, publisher):
    r = submit(client, alice, valid_body)
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["status"] == "received"
    assert r.headers["Location"] == data["statusUrl"] == f"/requests/{data['id']}"
    assert publisher.published == [uuid.UUID(data["id"])]


def test_get_and_history(client, alice, valid_body):
    rid = submit(client, alice, valid_body).json()["id"]
    r = client.get(f"/requests/{rid}", headers=alice)
    assert r.status_code == 200
    assert r.json()["serviceCode"] == "72148"
    assert r.json()["submittedBy"] == "alice"
    hist = client.get(f"/requests/{rid}/history", headers=alice).json()
    assert [(h["fromStatus"], h["toStatus"], h["event"]) for h in hist] == [(None, "received", "received")]


def test_validation_error_is_operation_outcome(client, alice, valid_body):
    valid_body["coverage"]["status"] = "cancelled"
    del valid_body["serviceRequest"]["intent"]
    r = submit(client, alice, valid_body)
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/fhir+json")
    body = r.json()
    assert body["resourceType"] == "OperationOutcome"
    by_path = {i["expression"][0]: i for i in body["issue"]}
    assert by_path["coverage.status"]["diagnostics"] == "coverage must be active to request prior authorization"
    assert by_path["serviceRequest.intent"]["code"] == "required"


def test_unknown_id_is_404(client, alice):
    assert client.get(f"/requests/{uuid.uuid4()}", headers=alice).status_code == 404


def test_malformed_id_is_422(client, alice):
    assert client.get("/requests/not-a-uuid", headers=alice).status_code == 422


def test_enqueue_failure_marks_request_failed(client, alice, viewer, valid_body, publisher):
    publisher.fail = True
    r = submit(client, alice, valid_body)
    assert r.status_code == 503
    assert "saved as failed" in r.json()["detail"]
    listed = client.get("/requests", params={"status": "failed"}, headers=viewer).json()
    assert len(listed) == 1
    assert listed[0]["lastError"] == "could not enqueue: ConnectionError"


def test_list_filters_by_status(client, alice, viewer, valid_body):
    submit(client, alice, valid_body)
    submit(client, alice, valid_body)
    assert len(client.get("/requests", headers=viewer).json()) == 2
    assert client.get("/requests", params={"status": "approved"}, headers=viewer).json() == []


def test_metrics_endpoint(client, alice, valid_body):
    submit(client, alice, valid_body)
    text = client.get("/metrics").text
    assert "priorauth_requests_received_total" in text
    assert 'priorauth_http_requests_total{method="POST",route="/requests",status="202"}' in text


def test_correlation_id_echoed(client):
    r = client.get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"


def test_openapi_lists_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/requests", "/requests/{request_id}", "/requests/{request_id}/history", "/auth/token"} <= paths.keys()
