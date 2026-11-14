"""Run the same bounded maintenance used by the nightly scheduler."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from monitor.history import maintain_target
from monitor.models import HistoryState, Target


class Command(BaseCommand):
    help = 'Compact monitoring history and prune safely aggregated observations.'

    def add_arguments(self, parser):
        parser.add_argument('--target', type=int, help='Process one probe only.')
        parser.add_argument('--dry-run', action='store_true', help='Show policy and counts without writing.')
        parser.add_argument('--no-purge', action='store_true', help='Build aggregates and snapshots without deleting.')

    def handle(self, *args, **options):
        targets = Target.objects.all()
        if options['target'] is not None:
            targets = targets.filter(pk=options['target'])
            if not targets.exists():
                raise CommandError('Probe not found.')
        self.stdout.write(
            f'Policy: checks={settings.CHECK_RETENTION_DAYS}d, failures={settings.FAILURE_RETENTION_DAYS}d, '
            f'hourly={settings.HOURLY_RETENTION_DAYS}d, daily={settings.DAILY_RETENTION_DAYS}d; '
            'nightly at 02:00 Europe/Zurich.'
        )
        now = timezone.now()
        for target in targets.iterator():
            if options['dry_run']:
                self.stdout.write(f'Probe {target.pk}: {target.checks.count()} checks; no changes.')
                continue
            while maintain_target(target.pk, now=now, purge=not options['no_purge']):
                pass
            state = HistoryState.objects.filter(target=target).first()
            if state is None:
                self.stdout.write(f'Probe {target.pk}: another maintenance owns the lock, or probe was deleted.')
                continue
            self.stdout.write(f'Probe {target.pk}: hourly through {state.hourly_until}; '
                              f'daily through {state.daily_until}; deleted checks={state.deleted_checks}.')
