"""
Django management command to test Celery tasks.

Usage:
    python manage.py test_celery
"""

from django.core.management.base import BaseCommand
from animal.tasks.tasks import process_etl_data_async, send_email_task, cleanup_task


class Command(BaseCommand):
    help = 'Test Celery tasks'

    def add_arguments(self, parser):
        parser.add_argument(
            '--task',
            type=str,
            choices=['etl', 'email', 'cleanup'],
            help='Which task to test',
        )

    def handle(self, *args, **options):
        task_type = options.get('task')
        
        if task_type == 'etl':
            self.stdout.write('Testing ETL processing task...')
            task = process_etl_data_async.delay([1, 2, 3])
            self.stdout.write(f'Task submitted with ID: {task.id}')
            self.stdout.write(f'Check result with: python manage.py check_task {task.id}')
            
        elif task_type == 'email':
            self.stdout.write('Testing email task...')
            task = send_email_task.delay(
                'Test Subject',
                'Test message from Celery',
                ['test@example.com']
            )
            self.stdout.write(f'Email task submitted with ID: {task.id}')
            self.stdout.write(f'Check result with: python manage.py check_task {task.id}')
            
        elif task_type == 'cleanup':
            self.stdout.write('Testing cleanup task...')
            task = cleanup_task.delay()
            self.stdout.write(f'Cleanup task submitted with ID: {task.id}')
            self.stdout.write(f'Check result with: python manage.py check_task {task.id}')
            
        else:
            self.stdout.write('Available tasks: etl, email, cleanup')
            self.stdout.write('Use: python manage.py test_celery --task etl')
            
        # General instructions
        if task_type:
            self.stdout.write('\nOther ways to check task status:')
            self.stdout.write('1. Check worker logs: docker logs -f <project_name>-celery-1')
            self.stdout.write('2. Monitor tasks: docker exec -it <project_name>-web-1 celery -A gaia monitor')
            self.stdout.write('3. Check Redis: docker exec -it <project_name>-redis-1 redis-cli')
