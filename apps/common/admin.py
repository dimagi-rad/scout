"""Shared Django admin helpers (arch #260).

``ReadOnlyModelAdmin`` is the in-process equivalent of procrastinate's bundled
admin: operator models that the team needs to *inspect* during incidents
(zombie jobs, stuck Preparing…, EXPIRED resurrection, cascade-FAILED view
schemas) are registered for visibility but can never be added, changed, or
deleted through the admin. This inverts the prior surface where dangerous
state-machine rows were fully editable while operator models were absent.
"""

from django.contrib import admin


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """A ModelAdmin that allows viewing but never add/change/delete.

    All concrete model fields are forced readonly so the change form renders as
    a read-only detail view. Subclasses set ``list_display`` / ``search_fields``
    / ``list_filter`` for operator ergonomics.
    """

    def get_readonly_fields(self, request, obj=None):
        # Every concrete (non-m2m, editable-or-not) field is readonly.
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
