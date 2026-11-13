from django.core.management.base import BaseCommand
from monitor.notifications import send_notification


class Command(BaseCommand):
    help = 'Envoyer un mail de test Postmark.'

    def handle(self, *args, **options):
        send_notification('TEST: sonde de disponibilité', 'La sonde peut envoyer des notifications.')
        self.stdout.write('Message accepté par Postmark. Vérifiez sa réception.')
