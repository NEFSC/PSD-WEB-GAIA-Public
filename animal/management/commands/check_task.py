"""
Django management command to check Celery task status.

Usage:
    python manage.py check_task <task_id>
"""

from django.core.management.base import BaseCommand
from celery.result import AsyncResult


class Command(BaseCommand):
    help = 'Check Celery task status and result'

    def add_arguments(self, parser):
        parser.add_argument('task_id', type=str, help='Task ID to check')

    def handle(self, *args, **options):
        task_id = options['task_id']
        
        task = AsyncResult(task_id)
        
        self.stdout.write(f'Task ID: {task_id}')
        self.stdout.write(f'Status: {task.status}')
        self.stdout.write(f'Result: {task.result}')
        
        if task.failed():
            self.stdout.write(f'Error: {task.info}')
        
        # Additional info
        if hasattr(task, 'date_done') and task.date_done:
            self.stdout.write(f'Completed at: {task.date_done}')
