from django.http import HttpResponse
from django.views.decorators.cache import cache_control


@cache_control(public=True, max_age=3600)
def widget_js_view(request):
    """Serve the Scout embed widget SDK."""
    from pathlib import Path

    widget_path = Path(__file__).parent.parent / "frontend" / "public" / "widget.js"
    try:
        content = widget_path.read_text()
    except FileNotFoundError:
        content = "// widget.js not found"

    response = HttpResponse(content, content_type="application/javascript")
    response["Access-Control-Allow-Origin"] = "*"
    return response
