def test_knowledge_rest_routes_are_registered():
    from sag_api.main import app

    routes = set(app.openapi()["paths"])

    assert {
        "/api/v1/sources/{source_id}/outline",
        "/api/v1/sources/{source_id}/grep",
        "/api/v1/sources/{source_id}/documents/{document_id}/read",
        "/api/v1/sources/{source_id}/entities/{name}/context",
    } <= routes
