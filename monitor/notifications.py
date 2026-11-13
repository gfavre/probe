import requests
from django.conf import settings
from .platform import platform_values


def send_notification(subject, body, channel='email', destination=''):
    config = platform_values()
    if channel == 'email':
        recipient = destination or config["email_to"]
        if not all([config["postmark_server_token"], config["email_from"], recipient]):
            raise ValueError('Configurer Postmark et le destinataire email.')
        url = 'https://api.postmarkapp.com/email'
        headers = {'X-Postmark-Server-Token': config["postmark_server_token"], 'Accept': 'application/json'}
        payload = {'From': config["email_from"], 'To': recipient, 'Subject': subject, 'TextBody': body, 'MessageStream': config["postmark_message_stream"]}
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
