"""DRF request parsers shared by every API view."""

from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser


class JSONObjectParser(JSONParser):
    """A ``JSONParser`` that only accepts a top-level JSON object.

    The DRF counterpart of ``apps.common.http.parse_json_object``: views call
    ``request.data.get(...)``, so an array or scalar body used to be a 500.
    """

    def parse(self, stream, media_type=None, parser_context=None):
        data = super().parse(stream, media_type, parser_context)
        if not isinstance(data, dict):
            raise ParseError("Request body must be a JSON object.")
        return data
