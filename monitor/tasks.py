import logging
from datetime import timedelta, timezone as dt_timezone
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import Check, Incident, Target
from .notifications import send_notification, incident_notification
from .probes import probe, certificate_expiry
from .platform import platform_values
from .history import maintain_target, result_snapshot

logger = logging.getLogger(__name__)


@shared_task(max_retries=0)
def dispatch_checks():
    for pk in Target.objects.filter(enabled=True, next_check_at__lte=timezone.now()).values_list('pk', flat=True):
        check_target.apply_async(args=[pk], expires=30, retry=False)


def record_result(target, now, result):
    previous = target.checks.order_by('-checked_at').first()
    if previous and now > previous.checked_at + timedelta(seconds=previous.coverage_seconds * 2):
        target.consecutive_failures = 0
        target.first_failure_at = None
    incident = target.incidents.filter(ended_at__isnull=True).first()
    if result.healthy:
        target.consecutive_failures = 0
        target.first_failure_at = None
        if incident:
            incident.ended_at = now
            incident.recovery_result = result_snapshot(result)
            incident.save(update_fields=['ended_at', 'recovery_result'])
    else:
        target.consecutive_failures += 1
        if target.first_failure_at is None:
            target.first_failure_at = now
        confirmation_ready = now >= target.first_failure_at + timedelta(seconds=Target.CONFIRMATION_DELAY_SECONDS)
        if not incident and target.consecutive_failures >= target.failure_threshold and confirmation_ready:
            destination = ''
            platform = platform_values()
            if target.notification_channel == 'email':
                destination = target.email_to or platform["email_to"]
            elif target.notification_channel == 'telegram':
                destination = target.telegram_chat_id or platform["telegram_chat_id"]
            incident = Incident.objects.create(
                target=target, started_at=target.first_failure_at, confirmed_at=now,
                notification_channel=target.notification_channel, notification_destination=destination,
                service_name=target.name, service_url=target.url,
                initial_result=result_snapshot(target.checks.filter(
                    healthy=False, checked_at__gte=target.first_failure_at,
                ).order_by('checked_at').first() or result),
                confirmation_result=result_snapshot(result), latest_failure=result_snapshot(result),
            )
        elif incident:
            incident.latest_failure = result_snapshot(result)
            incident.save(update_fields=['latest_failure'])
    interval_seconds = target.interval_minutes * 60
    if not result.healthy and incident is None:
        interval_seconds = Target.CONFIRMATION_DELAY_SECONDS
    target.next_check_at = now + timedelta(seconds=interval_seconds)
    Check.objects.create(target=target, checked_at=now, healthy=result.healthy, detail=result.detail,
                         duration_ms=result.duration_ms, status_code=result.status_code,
                         coverage_seconds=interval_seconds)
    target.save(update_fields=['consecutive_failures', 'first_failure_at', 'next_check_at', 'tls_checked_at', 'tls_expires_at'])


def notify_pending(target):
    cutoff = timezone.now() - timedelta(minutes=target.notification_delay_minutes)
    for incident in target.incidents.filter(ended_at__isnull=True, down_sent_at__isnull=True, started_at__lte=cutoff):
        try:
            subject, body, html = incident_notification(target, incident)
            send_notification(subject, body, incident.notification_channel,
                              incident.notification_destination, html_body=html)
        except ValueError:
            logger.warning('Notification de panne en attente pour cible %s', target.pk)
        else:
            incident.down_sent_at = timezone.now()
            incident.save(update_fields=['down_sent_at'])
    for incident in target.incidents.filter(ended_at__isnull=False, down_sent_at__isnull=False, recovery_sent_at__isnull=True):
        try:
            subject, body, html = incident_notification(target, incident, recovered=True)
            send_notification(subject, body, incident.notification_channel,
                              incident.notification_destination, html_body=html)
        except ValueError:
            logger.warning('Notification de rétablissement en attente pour cible %s', target.pk)
        else:
            incident.recovery_sent_at = timezone.now()
            incident.save(update_fields=['recovery_sent_at'])


@shared_task(max_retries=0)
def check_target(pk):
    with transaction.atomic():
        target = Target.objects.select_for_update(skip_locked=True).filter(pk=pk, enabled=True, next_check_at__lte=timezone.now()).first()
        if target is None:
            return
        result = probe(target)
        if target.tls_checked_at is None or target.tls_checked_at < timezone.now() - timedelta(days=1):
            target.tls_expires_at = certificate_expiry(target)
            target.tls_checked_at = timezone.now()
        record_result(target, timezone.now(), result)
    # Persist observations before attempting notifications; serialize delivery.
    with transaction.atomic():
        target = Target.objects.select_for_update().filter(pk=pk).first()
        if target:
            notify_pending(target)


@shared_task(max_retries=0)
def maintain_history():
    for pk in Target.objects.values_list('pk', flat=True):
        maintain_target_history.apply_async(args=[pk], retry=False)


@shared_task(max_retries=0)
def maintain_history_dst():
    # Swiss 02:00 does not exist on the spring clock-change night; catch up at 03:00.
    local = timezone.localtime()
    scheduled = local.replace(hour=2, minute=0, second=0, microsecond=0, fold=0)
    actual = scheduled.astimezone(dt_timezone.utc).astimezone(local.tzinfo)
    if actual.hour != 2:
        maintain_history()


@shared_task(max_retries=0)
def maintain_target_history(pk):
    # A continuation is new maintenance work, never a retry of an HTTP observation.
    if maintain_target(pk):
        maintain_target_history.apply_async(args=[pk], countdown=1, retry=False)
