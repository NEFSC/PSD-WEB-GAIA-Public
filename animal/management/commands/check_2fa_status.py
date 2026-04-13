from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django_otp.plugins.otp_totp.models import TOTPDevice


class Command(BaseCommand):
    help = 'Check 2FA status for all users'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=== 2FA STATUS REPORT ===\n'))
        
        users = User.objects.all()
        
        for user in users:
            devices = TOTPDevice.objects.filter(user=user)
            device_count = devices.count()
            
            if device_count == 0:
                status = self.style.ERROR('❌ NO 2FA')
            else:
                confirmed_devices = devices.filter(confirmed=True).count()
                if confirmed_devices > 0:
                    status = self.style.SUCCESS(f'✅ 2FA ACTIVE ({confirmed_devices} device(s))')
                else:
                    status = self.style.WARNING(f'⚠️  2FA SETUP ({device_count} unconfirmed device(s))')
            
            self.stdout.write(f'{user.username:<20} | {status}')
        
        self.stdout.write(f'\n=== SUMMARY ===')
        total_users = users.count()
        users_with_2fa = User.objects.filter(
            totpdevice__confirmed=True
        ).distinct().count()
        users_without_2fa = total_users - users_with_2fa
        
        self.stdout.write(f'Total Users: {total_users}')
        self.stdout.write(f'Users with 2FA: {users_with_2fa}')
        self.stdout.write(f'Users without 2FA: {users_without_2fa}')
        
        if users_without_2fa > 0:
            self.stdout.write(
                self.style.WARNING(
                    f'\n⚠️  {users_without_2fa} user(s) will be redirected to 2FA setup'
                )
            )
