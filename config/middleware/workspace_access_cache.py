from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from apps.workspaces import access_cache


class WorkspaceAccessCacheMiddleware:
    """Open one workspace access cache scope per request (see ``access_cache``).

    First in ``MIDDLEWARE`` so under ASGI the scope is set in the request task
    itself. A streamed response (a chat turn) keeps the scope open until its body
    finishes, since the agent runs after the view has returned.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        scope, token = access_cache.open_scope()
        try:
            response = self.get_response(request)
        finally:
            # A WSGI thread keeps its context across requests.
            access_cache.detach_scope(token)
        return _close_after(response, scope)

    async def __acall__(self, request):
        scope, _token = access_cache.open_scope()
        try:
            response = await self.get_response(request)
        except BaseException:
            access_cache.close_scope(scope)
            raise
        return _close_after(response, scope)


def _close_after(response, scope):
    if not getattr(response, "streaming", False):
        access_cache.close_scope(scope)
        return response
    content = response.streaming_content
    if response.is_async:

        async def closing():
            try:
                async for chunk in content:
                    yield chunk
            finally:
                access_cache.close_scope(scope)

    else:

        def closing():
            try:
                yield from content
            finally:
                access_cache.close_scope(scope)

    response.streaming_content = closing()
    return response
