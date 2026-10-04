"""Provider tenant-list protocols: one adapter per provider and one paginator.

Adapters build the first request and decode a page; ``paginator`` follows the
list. Callers keep their own policy for what a failure or omission means.
"""
