import pytest
from django.test import RequestFactory, override_settings

from config.middleware.embed import EmbedFrameOptionsMiddleware


@pytest.mark.django_db
class TestEmbedFrameOptionsMiddleware:
    def setup_method(self):
        self.factory = RequestFactory()
        self.get_response = lambda request: self._response
        self.middleware = EmbedFrameOptionsMiddleware(self.get_response)

    def _make_response(self, status=200):
        from django.http import HttpResponse
        self._response = HttpResponse("OK")
        self._response["X-Frame-Options"] = "DENY"
        return self._response

    @override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
    def test_embed_route_removes_x_frame_options(self):
        self._make_response()
        request = self.factory.get("/embed/")
        response = self.middleware(request)
        assert "X-Frame-Options" not in response

    @override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
    def test_embed_route_sets_frame_ancestors(self):
        self._make_response()
        request = self.factory.get("/embed/")
        response = self.middleware(request)
        assert "frame-ancestors" in response.get("Content-Security-Policy", "")
        assert "https://connect-labs.example.com" in response["Content-Security-Policy"]

    @override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
    def test_non_embed_route_keeps_x_frame_options(self):
        self._make_response()
        request = self.factory.get("/api/chat/")
        response = self.middleware(request)
        assert response.get("X-Frame-Options") == "DENY"

    @override_settings(EMBED_ALLOWED_ORIGINS=[])
    def test_empty_origins_denies_framing(self):
        self._make_response()
        request = self.factory.get("/embed/")
        response = self.middleware(request)
        assert response.get("X-Frame-Options") == "DENY"

    @override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
    def test_embed_route_sets_samesite_none_on_cookies(self):
        self._make_response()
        self._response.set_cookie("sessionid_scout", "abc123")
        self._response.set_cookie("csrftoken_scout", "xyz789")
        request = self.factory.get("/embed/")
        response = self.middleware(request)
        for cookie_name in ["sessionid_scout", "csrftoken_scout"]:
            cookie = response.cookies[cookie_name]
            assert cookie["samesite"] == "None"
            assert cookie["secure"] is True

    @override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
    def test_non_embed_route_does_not_change_cookies(self):
        self._make_response()
        self._response.set_cookie("sessionid_scout", "abc123", samesite="Lax")
        request = self.factory.get("/api/chat/")
        response = self.middleware(request)
        assert response.cookies["sessionid_scout"]["samesite"] == "Lax"
