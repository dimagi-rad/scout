from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from apps.workspaces import access_cache


class WorkspaceAccessCacheMiddleware:
    """Open one workspace access cache scope per request (see ``access_cache``).

    First in ``MIDDLEWARE`` so under ASGI the scope is set in the request task
    itself. An async streamed response (a chat turn) keeps the scope open until
    its body finishes, since the agent runs after the view has returned.
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
        # Sync bodies (file downloads, CSV exports) resolve nothing while
        # streaming, so the scope ends with the view either way. A WSGI thread
        # keeps its context across requests, hence the detach.
        scope, token = access_cache.open_scope()
        try:
            return self.get_response(request)
        finally:
            access_cache.close_scope(scope)
            access_cache.detach_scope(token)

    async def __acall__(self, request):
        scope, _token = access_cache.open_scope()
        try:
            response = await self.get_response(request)
        except BaseException:
            access_cache.close_scope(scope)
            raise
        if not (getattr(response, "streaming", False) and response.is_async):
            access_cache.close_scope(scope)
            return response
        content = response.streaming_content

        async def closing():
            try:
                async for chunk in content:
                    yield chunk
            finally:
                access_cache.close_scope(scope)

        response.streaming_content = closing()
        return response
