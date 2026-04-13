# ------------------------------------------------------------------------------
# ----- toggle_2fa_enforcement.py ----------------------------------------------
# ------------------------------------------------------------------------------
#
#    authors:  Jeffrey Wyman (jeffrey.wyman@noaa.gov), John Wall (john.wall@noaa.gov)
#    purpose:  Toggle 2FA enforcement in settings.py (global and NOAA-specific).
#
#    tickets:  GAIFAGP-477 (dry-run/confirm guards)
#              GAIFAGP-516 (getattr(os.environ) fix)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - gaia/settings.py is the settings file to modify
#      - ENFORCE_TWO_FACTOR_AUTH and ENFORCE_TWO_FACTOR_AUTH_NOAA are the
#        relevant settings keys
#      - Server restart is required after modification
#
#    usage:    python manage.py toggle_2fa_enforcement --help
#
# ------------------------------------------------------------------------------

import os
import re

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings


class Command(BaseCommand):
    help = 'Toggle two-factor authentication enforcement for all users'

    def add_arguments(self, parser):
        parser.add_argument(
            '--enable',
            action='store_true',
            help='Enable 2FA enforcement for all users',
        )
        parser.add_argument(
            '--disable',
            action='store_true',
            help='Disable 2FA enforcement (makes 2FA optional)',
        )
        parser.add_argument(
            '--enable-noaa',
            action='store_true',
            help='Enable 2FA enforcement only for users with noaa.gov email addresses',
        )
        parser.add_argument(
            '--disable-noaa',
            action='store_true',
            help='Disable NOAA-specific 2FA enforcement',
        )
        parser.add_argument(
            '--status',
            action='store_true',
            help='Show current 2FA enforcement status',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would change without modifying settings.py',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Execute the settings.py modification',
        )

    def handle(self, *args, **options):
        """
        Toggle 2FA enforcement by modifying gaia/settings.py.

        --status is read-only and runs without guards. All toggle operations
        (--enable, --disable, --enable-noaa, --disable-noaa) modify settings.py
        and require --dry-run or --confirm per Engineering Guide §1.3
        (Safety by Default). Server restart required after changes.
        """
        dry_run = options['dry_run']
        confirm = options['confirm']

        current_global = getattr(settings, 'ENFORCE_TWO_FACTOR_AUTH', True)
        current_noaa = getattr(settings, 'ENFORCE_TWO_FACTOR_AUTH_NOAA', False)

        # --status is read-only, no guard needed
        if options['status']:
            self.stdout.write('2FA enforcement status:')
            self.stdout.write(f"  ENFORCE_TWO_FACTOR_AUTH: {current_global}")
            self.stdout.write(f"  ENFORCE_TWO_FACTOR_AUTH_NOAA: {current_noaa}")
            return

        # Validate arguments
        enable_count = sum([
            options['enable'], options['disable'],
            options['enable_noaa'], options['disable_noaa'],
        ])
        if enable_count > 1:
            raise CommandError(
                "Cannot use multiple conflicting options at the same time.\n"
                "Use exactly one of: --enable, --disable, --enable-noaa, --disable-noaa"
            )

        if enable_count == 0:
            raise CommandError(
                "Specify one of: --enable, --disable, --enable-noaa, --disable-noaa, or --status.\n"
                f"Current: ENFORCE_TWO_FACTOR_AUTH={current_global}, "
                f"ENFORCE_TWO_FACTOR_AUTH_NOAA={current_noaa}"
            )

        # --- Dry-run/confirm guard for all toggle operations ---
        if not dry_run and not confirm:
            raise CommandError(
                "Toggle operations require --dry-run or --confirm.\n"
                "Example: python manage.py toggle_2fa_enforcement --enable --dry-run"
            )

        if dry_run and confirm:
            raise CommandError("Cannot use --dry-run and --confirm together.")

        # --- Execute toggle ---
        if options['enable']:
            changes = [
                ('ENFORCE_TWO_FACTOR_AUTH', current_global, True),
                ('ENFORCE_TWO_FACTOR_AUTH_NOAA', current_noaa, False),
            ]
        elif options['disable']:
            changes = [
                ('ENFORCE_TWO_FACTOR_AUTH', current_global, False),
                ('ENFORCE_TWO_FACTOR_AUTH_NOAA', current_noaa, False),
            ]
        elif options['enable_noaa']:
            changes = [
                ('ENFORCE_TWO_FACTOR_AUTH', current_global, False),
                ('ENFORCE_TWO_FACTOR_AUTH_NOAA', current_noaa, True),
            ]
        elif options['disable_noaa']:
            changes = [
                ('ENFORCE_TWO_FACTOR_AUTH_NOAA', current_noaa, False),
            ]

        if dry_run:
            self.stdout.write(self.style.WARNING('[DRY RUN] Would modify settings.py:'))
            for setting_name, old_val, new_val in changes:
                self.stdout.write(f'  {setting_name}: {old_val} -> {new_val}')
            self.stdout.write('  No changes were written.')
            return

        for setting_name, _old_val, new_val in changes:
            self._update_settings_file(setting_name, new_val)

        self.stdout.write(self.style.SUCCESS('Settings updated.'))
        for setting_name, old_val, new_val in changes:
            self.stdout.write(f'  {setting_name}: {old_val} -> {new_val}')
        self.stdout.write('')
        self.stdout.write(self.style.WARNING('Server restart required for changes to take effect.'))

    def _update_settings_file(self, setting_name, enable):
        """
        Update a specific setting in gaia/settings.py.

        Args:
            setting_name: The settings key to modify.
            enable: True to set the value to True, False to set to False.

        Raises:
            CommandError: If settings.py cannot be found.
        """
        settings_path = os.path.join(settings.BASE_DIR, 'gaia', 'settings.py')

        if not os.path.exists(settings_path):
            raise CommandError(f"Settings file not found: {settings_path}")

        with open(settings_path, 'r') as f:
            content = f.read()

        pattern = f'{setting_name}\\s*=.*'
        new_value = 'True' if enable else 'False'
        replacement = (
            f"{setting_name} = os.environ.get('{setting_name}', "
            f"'{new_value}').lower() == 'true'"
        )

        if re.search(pattern, content):
            content = re.sub(pattern, replacement, content)
        else:
            content += f"\n\n# 2FA Enforcement Setting\n{replacement}\n"

        with open(settings_path, 'w') as f:
            f.write(content)
