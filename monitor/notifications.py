import requests
from types import SimpleNamespace
from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format, number_format
from django.utils.translation import gettext as _
from .platform import platform_values


@sensitive_variables()
def send_notification(subject, body, channel='email', destination='', *, html_body=None):
    config = platform_values()
    if channel == 'email':
        recipient = destination or config["email_to"]
        if not all([config["postmark_server_token"], config["email_from"], recipient]):
            raise ValueError('Configurer Postmark et le destinataire email.')
        url = 'https://api.postmarkapp.com/email'
        headers = {'X-Postmark-Server-Token': config["postmark_server_token"], 'Accept': 'application/json'}
        payload = {'From': config["email_from"], 'To': recipient, 'Subject': subject, 'TextBody': body, 'MessageStream': config["postmark_message_stream"]}
        if html_body:
            payload['HtmlBody'] = html_body
    elif channel == 'slack':
        if not config["slack_webhook_url"].startswith('https://hooks.slack.com/services/'):
            raise ValueError('Configurer le webhook Slack.')
        url, headers = config["slack_webhook_url"], {}
        payload = {'text': f'{subject}\n{body}', 'mrkdwn': False, 'unfurl_links': False, 'unfurl_media': False}
    elif channel == 'telegram':
        if not config['telegram_bot_token'] or not (destination or config["telegram_chat_id"]):
            raise ValueError('Configurer le bot et la conversation Telegram.')
        url = f'https://api.telegram.org/bot{config['telegram_bot_token']}/sendMessage'
        headers = {}
        payload = {'chat_id': destination or config["telegram_chat_id"], 'text': f'{subject}\n{body}', 'link_preview_options': {'is_disabled': True}}
    else:
        raise ValueError('Canal inconnu.')
    try:
        with requests.post(url, headers=headers, json=payload, timeout=settings.HTTP_TIMEOUT, allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError(f'Notification refusée (HTTP {response.status_code}).')
            if channel == 'slack':
                accepted = response.text.strip() == 'ok'
            else:
                data = response.json()
                accepted = isinstance(data, dict) and (data.get('ErrorCode') == 0 if channel == 'email' else data.get('ok') is True)
            if not accepted:
                raise ValueError('Notification refusée par le service.')
    except requests.RequestException:
        # Never include a webhook URL, bot token or provider response in logs.
        raise ValueError('Service de notification inaccessible.') from None


def readable_duration(seconds):
    remaining = max(0, int(seconds))
    parts = []
    for size, unit in ((86400, _('j')), (3600, 'h'), (60, 'min'), (1, 's')):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f'{count} {unit}')
    return ' '.join(parts) or '0 s'


def incident_notification(target, incident, *, recovered=False):
    """Build escaped HTML and readable plain text from recorded observations."""
    end = incident.ended_at or timezone.now()
    failures = target.checks.filter(healthy=False, checked_at__gte=incident.started_at, checked_at__lte=end)
    failure = (SimpleNamespace(**incident.latest_failure) if incident.latest_failure
               else failures.order_by('-checked_at').first())
    saved_latest = incident.recovery_result if recovered else incident.latest_failure
    latest = (SimpleNamespace(**saved_latest) if saved_latest
              else target.checks.filter(checked_at__lte=end).order_by('-checked_at').first())
    service_name = incident.service_name or target.name
    service_url = incident.service_url or target.url
    elapsed = readable_duration((end - incident.started_at).total_seconds())
    if failure is None:
        kind, diagnostic = _('Erreur non précisée'), _('Diagnostic indisponible')
    else:
        diagnostic = failure.detail
        if not diagnostic.startswith('HTTP '):
            kind = diagnostic
        elif failure.status_code and failure.status_code >= 500:
            kind = _('Erreur serveur')
        elif failure.status_code and failure.status_code >= 400:
            kind = _('Erreur HTTP')
        else:
            kind = _('Réponse non conforme')

    def timestamp(value):
        return date_format(timezone.localtime(value), 'd.m.Y H:i:s T')

    title = _('Service rétabli') if recovered else _('Service indisponible')
    subject = (
        _('[RÉTABLI] %(service)s — %(duration)s d’indisponibilité')
        % {'service': service_name, 'duration': elapsed}
        if recovered else _('[INDISPONIBLE] %(service)s — %(diagnostic)s')
        % {'service': service_name, 'diagnostic': diagnostic}
    )
    rows = [
        (_('Service'), service_name), (_('URL'), service_url),
        (_('Type de panne'), kind), (_('Diagnostic'), diagnostic),
        (_('Premier échec'), timestamp(incident.started_at)),
        (_('Panne confirmée'), timestamp(incident.confirmed_at)),
    ]
    if recovered:
        rows.append((_('Rétablissement'), timestamp(incident.ended_at)))
    rows.append(((_('Durée totale observée') if recovered else _('Durée observée — en cours')), elapsed))
    if latest:
        response = f'HTTP {latest.status_code}' if latest.status_code else latest.detail
        response_time = (f'{latest.duration_ms} ms' if latest.duration_ms < 1000
                         else f'{number_format(latest.duration_ms / 1000, decimal_pos=2)} s')
        rows.append((_('Dernier contrôle'), f'{response} · {response_time}'))
    rows.append((_('Incident'), f'#{incident.pk}'))
    url = settings.PUBLIC_URL.rstrip('/') + reverse('target_detail', args=[target.pk])
    note = _('Les durées sont mesurées entre les contrôles; elles ne donnent pas l’instant exact de la panne.')
    body = title + '\n\n' + '\n'.join(f'{label}: {value}' for label, value in rows)
    body += f'\n\n{note}\n\n' + _('Voir la sonde') + f': {url}'
    from .branding import logo_options
    branding = logo_options(email=True)
    if branding:
        branding['platform_logo_url'] = settings.PUBLIC_URL.rstrip('/') + branding['platform_logo_url']
        branding['platform_logo_email_width'] = min(260, branding['platform_logo_width'],
                                                   max(1, round(96 * branding['platform_logo_width']
                                                                / branding['platform_logo_height'])))
    html = render_to_string('monitor/email/incident.html', {
        **branding,
        'title': title, 'service': service_name, 'rows': rows, 'url': url,
        'note': note, 'recovered': recovered, 'elapsed': elapsed,
    })
    return subject, body, html
