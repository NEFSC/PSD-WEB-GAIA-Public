# ------------------------------------------------------------------------------
# ----- totp_admin.py ----------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    authors:  Jeffrey Wyman (jeffrey.wyman@noaa.gov), John Wall (john.wall@noaa.gov)
#    purpose:  TOTP device management — list, cleanup, reset, disable, throttle reset.
#
#    tickets:  GAIFAGP-477 (dry-run/confirm guards)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - django_otp TOTPDevice is the sole 2FA device model
#      - Static backup tokens have been removed from the product
#      - User.username is the lookup key for per-user operations
#
#    usage:    python manage.py totp_admin --help
#
# ------------------------------------------------------------------------------

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django_otp.plugins.otp_totp.models import TOTPDevice


# Actions that mutate data and require --dry-run or --confirm
DESTRUCTIVE_ACTIONS = {'cleanup', 'reset', 'disable_all', 'reset_throttling'}

# Actions that are read-only — no guard needed
READ_ONLY_ACTIONS = {'list'}


class Command(BaseCommand):
    help = 'Manage TOTP devices for troubleshooting authentication issues'

    def add_arguments(self, parser):
        parser.add_argument(
            'action',
            choices=['list', 'cleanup', 'reset', 'disable_all', 'reset_throttling'],
            help='Action to perform',
        )
        parser.add_argument(
            '--username',
            type=str,
            help='Username for reset, disable_all, or reset_throttling action',
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
        Route to the requested TOTP admin action.

        Read-only actions (list) execute without guards. Destructive actions
        (cleanup, reset, disable_all, reset_throttling) require --dry-run or
        --confirm per Engineering Guide §1.3 (Safety by Default).

        Argument validation (e.g., --username requirement) runs before the
        dry-run/confirm guard so users get specific error messages.
        """
        action = options['action']
        dry_run = options['dry_run']
        confirm = options['confirm']

        # --- Argument validation first (before guard check) ---
        if action == 'reset':
            if not options['username']:
                raise CommandError('Username required for reset action')
        elif action == 'disable_all':
            if not options['username']:
                raise CommandError('Username required for disable_all action')

        # --- Dry-run/confirm guard for destructive actions ---
        if action in DESTRUCTIVE_ACTIONS:
            if not dry_run and not confirm:
                raise CommandError(
                    f"{action} requires --dry-run or --confirm.\n"
                    f"Example: python manage.py totp_admin {action} --dry-run"
                )

            if dry_run and confirm:
                raise CommandError("Cannot use --dry-run and --confirm together.")

        # --- Dispatch ---
        if action == 'list':
            self.list_devices()
        elif action == 'cleanup':
            self.cleanup_devices(dry_run=dry_run)
        elif action == 'reset':
            self.reset_user_devices(options['username'], dry_run=dry_run)
        elif action == 'disable_all':
            self.disable_all_user_2fa(options['username'], dry_run=dry_run)
        elif action == 'reset_throttling':
            if options['username']:
                self.reset_throttling_for_user(options['username'], dry_run=dry_run)
            else:
                self.reset_throttling_all(dry_run=dry_run)

    def list_devices(self):
        """List all TOTP devices by user."""
        self.stdout.write('Current 2FA devices:')
        self.stdout.write('-' * 50)

        for user in User.objects.all():
            totp_devices = TOTPDevice.objects.filter(user=user)

            if totp_devices.exists():
                self.stdout.write(f"User: {user.username}")

                for device in totp_devices:
                    status = "Confirmed" if device.confirmed else "Unconfirmed"
                    self.stdout.write(
                        f"  TOTP Device ID {device.id}: '{device.name}' [{status}]"
                    )

                self.stdout.write('')

    def cleanup_devices(self, dry_run=False):
        """Remove test and unconfirmed devices."""
        test_totp_devices = TOTPDevice.objects.filter(name__in=['test-device', 'default'])
        totp_test_count = test_totp_devices.count()

        unconfirmed_totp = TOTPDevice.objects.filter(confirmed=False)
        totp_unconfirmed_count = unconfirmed_totp.count()

        total = totp_test_count + totp_unconfirmed_count

        if dry_run:
            self.stdout.write(self.style.WARNING('[DRY RUN] Would clean up test and unconfirmed devices'))
            self.stdout.write(f"  Test TOTP devices to remove: {totp_test_count}")
            self.stdout.write(f"  Unconfirmed TOTP devices to remove: {totp_unconfirmed_count}")
            self.stdout.write(f"  Total devices that would be deleted: {total}")
            self.stdout.write('  No devices were deleted.')
            return

        test_totp_devices.delete()
        self.stdout.write(f"Removed {totp_test_count} test TOTP devices")

        unconfirmed_totp.delete()
        self.stdout.write(f"Removed {totp_unconfirmed_count} unconfirmed TOTP devices")

        self.stdout.write(self.style.SUCCESS(f'Cleanup complete. {total} devices deleted.'))

    def reset_user_devices(self, username, dry_run=False):
        """Reset all TOTP devices for a user (delegates to disable_all_user_2fa)."""
        self.disable_all_user_2fa(username, dry_run=dry_run)

    def disable_all_user_2fa(self, username, dry_run=False):
        """Disable all 2FA devices for a user."""
        try:
            user = User.objects.get(username=username)

            totp_devices = TOTPDevice.objects.filter(user=user)
            totp_count = totp_devices.count()
            totp_names = [d.name or 'TOTP Device' for d in totp_devices]

            if dry_run:
                self.stdout.write(self.style.WARNING(
                    f"[DRY RUN] Would remove {totp_count} 2FA device(s) for user '{username}'"
                ))
                if totp_names:
                    self.stdout.write(f"  Devices: {', '.join(totp_names)}")
                self.stdout.write('  No devices were deleted.')
                return

            totp_devices.delete()

            self.stdout.write(self.style.SUCCESS(
                f"Removed {totp_count} 2FA device(s) for user '{username}'."
            ))
            if totp_names:
                self.stdout.write(f"  Devices removed: {', '.join(totp_names)}")

        except User.DoesNotExist:
            raise CommandError(f"User '{username}' not found")

    def reset_throttling_for_user(self, username, dry_run=False):
        """Reset throttling for a specific user's TOTP devices."""
        try:
            user = User.objects.get(username=username)
            devices = TOTPDevice.objects.filter(
                user=user,
                throttling_failure_count__gt=0,
            )
            count = devices.count()

            if dry_run:
                self.stdout.write(self.style.WARNING(
                    f'[DRY RUN] Would reset throttling for {count} device(s) for user: {username}'
                ))
                self.stdout.write('  No throttling counters were reset.')
                return

            devices.update(
                throttling_failure_count=0,
                throttling_failure_timestamp=None,
            )
            self.stdout.write(self.style.SUCCESS(
                f'Reset throttling for {count} device(s) for user: {username}'
            ))
        except User.DoesNotExist:
            raise CommandError(f"User '{username}' not found")

    def reset_throttling_all(self, dry_run=False):
        """Reset throttling for all TOTP devices."""
        devices = TOTPDevice.objects.filter(throttling_failure_count__gt=0)
        count = devices.count()

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'[DRY RUN] Would reset throttling for {count} device(s)'
            ))
            self.stdout.write('  No throttling counters were reset.')
            return

        devices.update(
            throttling_failure_count=0,
            throttling_failure_timestamp=None,
        )
        self.stdout.write(self.style.SUCCESS(
            f'Reset throttling for {count} device(s).'
        ))
