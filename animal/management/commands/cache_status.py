# ------------------------------------------------------------------------------
# ----- cache_status.py --------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    authors:  Jeffrey Wyman (jeffrey.wyman@noaa.gov), John Wall (john.wall@noaa.gov)
#    purpose:  Cache inspection and clearing for project page cache.
#
#    tickets:  GAIFAGP-477 (dry-run/confirm guards)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - Django cache backend (settings.CACHES) is the sole cache layer
#      - ENABLE_PROJECT_PAGE_CACHE controls whether caching is active
#
#    usage:    python manage.py cache_status --help
#
# ------------------------------------------------------------------------------

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.core.cache import cache


class Command(BaseCommand):
    help = 'Check the status of project page caching'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear-all',
            action='store_true',
            help='Clear all project page cache entries',
        )
        parser.add_argument(
            '--test-cache',
            action='store_true',
            help='Test cache connectivity',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would happen without making changes',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Execute destructive operation',
        )

    def handle(self, *args, **options):
        """
        Inspect cache status, test connectivity, or clear cache entries.

        Read-only operations (status check, --test-cache) run without guards.
        Destructive operations (--clear-all) require --dry-run or --confirm
        per Engineering Guide §1.3 (Safety by Default).
        """
        dry_run = options['dry_run']
        confirm = options['confirm']

        cache_enabled = getattr(settings, 'ENABLE_PROJECT_PAGE_CACHE', True)

        self.stdout.write(f"Project page cache: {'ENABLED' if cache_enabled else 'DISABLED'}")

        if not cache_enabled:
            self.stdout.write(self.style.WARNING(
                'Cache is disabled. No cache operations performed.'
            ))
            return

        # Test cache connectivity (read-only, no guard)
        if options['test_cache']:
            try:
                cache.set('test_key', 'test_value', timeout=60)
                result = cache.get('test_key')
                cache.delete('test_key')

                if result == 'test_value':
                    self.stdout.write(self.style.SUCCESS('Cache connectivity: OK'))
                else:
                    self.stdout.write(self.style.ERROR(
                        'Cache connectivity: FAILED (retrieved wrong value)'
                    ))
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'Cache connectivity: FAILED ({e})'
                ))

        # Clear all cache entries (destructive — guarded)
        if options['clear_all']:
            if not dry_run and not confirm:
                raise CommandError(
                    "--clear-all requires --dry-run or --confirm.\n"
                    "Example: python manage.py cache_status --clear-all --dry-run"
                )

            if dry_run and confirm:
                raise CommandError("Cannot use --dry-run and --confirm together.")

            if dry_run:
                cache_config = getattr(settings, 'CACHES', {}).get('default', {})
                backend = cache_config.get('BACKEND', 'unknown')
                self.stdout.write(self.style.WARNING('[DRY RUN] Would clear all cache entries'))
                self.stdout.write(f'  Cache backend: {backend}')
                self.stdout.write('  No entries were cleared.')
            else:
                try:
                    cache.clear()
                    self.stdout.write(self.style.SUCCESS('All cache entries cleared.'))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'Cache clear failed: {e}'))

        # Show cache configuration
        self.stdout.write('')
        self.stdout.write('Cache configuration:')
        cache_config = getattr(settings, 'CACHES', {}).get('default', {})
        for key, value in cache_config.items():
            self.stdout.write(f'  {key}: {value}')