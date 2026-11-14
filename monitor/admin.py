from django.contrib import admin
from .forms import TargetForm
from .models import Target, Check, Incident, HistoryState


@admin.register(Target)
class TargetAdmin(admin.ModelAdmin):
    form = TargetForm
    list_display = ['name', 'url', 'enabled', 'interval_minutes', 'notification_channel']
    exclude = ['next_check_at']
    list_filter = ['enabled', 'notification_channel']
    search_fields = ['name', 'url']


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False
    def has_change_permission(self, request, obj=None):
        return False
    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Incident)
class IncidentAdmin(ReadOnlyAdmin):
    list_display = ['target', 'started_at', 'ended_at', 'notification_channel', 'down_sent_at', 'recovery_sent_at']
    list_filter = ['target']


@admin.register(Check)
class CheckAdmin(ReadOnlyAdmin):
    list_display = ['target', 'checked_at', 'healthy', 'detail', 'duration_ms']
    list_filter = ['target', 'healthy']


@admin.register(HistoryState)
class HistoryStateAdmin(ReadOnlyAdmin):
    list_display = ['target', 'last_started_at', 'last_success_at', 'last_error', 'deleted_checks']
