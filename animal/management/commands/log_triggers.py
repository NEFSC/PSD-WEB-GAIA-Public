from django.core.management.base import BaseCommand
from django.db import connection
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Log trigger events from SpatiaLite to Django log file'

    def handle(self, *args, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute("SELECT * from trigger_log ORDER BY log_time DESC")
            rows = cursor.fetchall()

            for row in rows:
                trigger_name, action, log_message, log_time = row[1], row[2], row[3], row[4]
                logger.info(f"Trigger: {trigger_name}, Action: {action}, Message: {log_message}, Time: {log_time}")