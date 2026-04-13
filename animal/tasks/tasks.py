"""
Process ETL (Extract, Transform, Load) data for selected records.

This module contains functions for processing ETL data based on filtered IDs.
It fetches the corresponding ExtractTransformLoad model objects and processes
them individually.

Functions:
    process_etl_data(filtered_data_ids): Process multiple ETL records
    process_data(etl): Process a single ETL record

Args:
    filtered_data_ids (list): List of ExtractTransformLoad record IDs to process
    etl (ExtractTransformLoad): Individual ETL model instance to process

Returns:
    str: "Processing Complete" confirmation message

Example:
    >>> ids = [1, 2, 3]
    >>> result = process_etl_data(ids)
    >>> print(result)
    "Processing Complete"
"""
import json
import logging
from datetime import datetime, timedelta
from django.core.mail import send_mail
from django.utils import timezone
from smtplib import SMTPException
from django.core.exceptions import ValidationError
from django.conf import settings
from celery import shared_task
from animal.models import ExtractTransformLoad


def test_redis_connection():
    """
    Test Redis connection and return detailed connection information.
    This function can be called from the Django admin or management commands.
    Note: This is not a Celery task to avoid circular dependency on Redis infrastructure.
    """
    try:
        # Import redis here to handle missing package gracefully
        try:
            import redis
        except ImportError:
            return json.dumps({
                'status': 'FAILED',
                'error': 'Redis package not installed (redis-py required)',
                'timestamp': timezone.now().isoformat()
            }, indent=2)

        # Get Redis URL from settings
        redis_url = getattr(settings, 'REDIS_URL', 'redis://redis:6379/0')
        celery_broker_url = getattr(settings, 'CELERY_BROKER_URL', redis_url)

        # Test Redis connection
        r = redis.from_url(redis_url)

        # Perform connection test
        r.ping()

        # Get Redis info
        redis_info = r.info()

        # Test set/get operations
        test_key = f"test_connection_{timezone.now().timestamp()}"
        test_value = "Redis connection test successful"
        r.set(test_key, test_value, ex=60)  # Expire in 60 seconds
        retrieved_value = r.get(test_key).decode('utf-8')

        # Clean up test key
        r.delete(test_key)

        # Prepare results
        connection_info = {
            'status': 'SUCCESS',
            'redis_url': redis_url,
            'celery_broker_url': celery_broker_url,
            'redis_version': redis_info.get('redis_version', 'Unknown'),
            'connected_clients': redis_info.get('connected_clients', 'Unknown'),
            'used_memory_human': redis_info.get('used_memory_human', 'Unknown'),
            'uptime_in_seconds': redis_info.get('uptime_in_seconds', 'Unknown'),
            'test_operation': 'SET/GET test successful',
            'test_value_match': retrieved_value == test_value,
            'timestamp': timezone.now().isoformat()
        }

        return json.dumps(connection_info, indent=2)

    except redis.ConnectionError as e:
        error_info = {
            'status': 'CONNECTION_ERROR',
            'error': str(e),
            'redis_url': getattr(settings, 'REDIS_URL', 'Not configured'),
            'suggestion': 'Check if Redis server is running and accessible',
            'timestamp': timezone.now().isoformat()
        }
        return json.dumps(error_info, indent=2)

    except redis.AuthenticationError as e:
        error_info = {
            'status': 'AUTHENTICATION_ERROR',
            'error': str(e),
            'suggestion': 'Check Redis authentication credentials',
            'timestamp': timezone.now().isoformat()
        }
        return json.dumps(error_info, indent=2)

    except Exception as e:
        error_info = {
            'status': 'GENERAL_ERROR',
            'error': str(e),
            'error_type': type(e).__name__,
            'timestamp': timezone.now().isoformat()
        }
        return json.dumps(error_info, indent=2)

@shared_task(bind=True)
def process_etl_data_async(self, filtered_data_ids):
    """
    Process ETL data asynchronously using Celery.

    Args:
        filtered_data_ids (list): List of ExtractTransformLoad record IDs to process

    Returns:
        int: Number of records processed

    Raises:
        ExtractTransformLoad.DoesNotExist: If any ID doesn't exist
        ValidationError: If data validation fails
        Exception: For any other processing errors
    """
    logger = logging.getLogger(__name__)
    try:
        # Fetch model objects for processing
        filtered_data = ExtractTransformLoad.objects.filter(id__in=filtered_data_ids)
        
        if not filtered_data.exists():
            raise ExtractTransformLoad.DoesNotExist(
                f"No ETL records found for IDs: {filtered_data_ids}"
            )

        # Verify all requested IDs were found
        found_ids = set(filtered_data.values_list('id', flat=True))
        missing_ids = set(filtered_data_ids) - found_ids
        if missing_ids:
            raise ExtractTransformLoad.DoesNotExist(
                f"ETL records not found for IDs: {missing_ids}"
            )

        # Perform the processing logic here
        processed_count = 0
        for etl in filtered_data:
            try:
                process_data(etl)
                processed_count += 1
                logger.info(f"Successfully processed ETL record {etl.id}")
            except Exception as e:
                logger.error(f"Failed to process ETL record {etl.id}: {str(e)}", exc_info=True)
                raise  # Re-raise to fail the entire task

        logger.info(f"Successfully processed {processed_count} ETL records")
        return processed_count

    except ExtractTransformLoad.DoesNotExist as e:
        logger.error(f"ETL records not found: {str(e)}")
        raise  # Re-raise to mark task as FAILED

    except ValidationError as e:
        logger.error(f"Validation error in ETL processing: {str(e)}")
        raise  # Re-raise to mark task as FAILED

    except Exception as e:
        logger.error(f"Unexpected error in ETL processing: {str(e)}", exc_info=True)
        raise  # Re-raise to mark task as FAILED


@shared_task(bind=True)
def send_email_task(self, subject, message, recipient_list):
    """
    Send an email asynchronously.

    Args:
        subject (str): Email subject
        message (str): Email message body
        recipient_list (str or list): Single email or list of emails

    Returns:
        int: Number of recipients the email was sent to

    Raises:
        ValueError: If recipient_list is invalid
        SMTPException: If email sending fails
    """
    logger = logging.getLogger(__name__)
    try:
        # Ensure recipient_list is a list
        if isinstance(recipient_list, str):
            recipient_list = [recipient_list]
        elif not isinstance(recipient_list, (list, tuple)):
            recipient_list = list(recipient_list)

        # Validate recipients
        if not recipient_list:
            raise ValueError("No recipients provided")
        
        # Validate email settings
        if not settings.EMAIL_HOST or not settings.EMAIL_HOST_USER:
            raise ValueError("Email settings not properly configured")

        # Send email
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        logger.info(f"Email sent successfully to {len(recipient_list)} recipients")
        return len(recipient_list)

    except ValueError as e:
        logger.error(f"Email validation failed: {str(e)}")
        raise  # Re-raise to mark task as FAILED

    except SMTPException as e:
        logger.error(f"SMTP error sending email: {str(e)}", exc_info=True)
        raise  # Re-raise to mark task as FAILED

    except Exception as e:
        logger.error(f"Unexpected error sending email: {str(e)}", exc_info=True)
        raise  # Re-raise to mark task as FAILED


@shared_task(bind=True)
def cleanup_task(self):
    """
    Periodic cleanup task for maintenance operations.
    This task runs periodically via Celery Beat to:
    1. Remove completed ETL records older than 30 days
    2. Clean up any temporary processing artifacts
    3. Log cleanup statistics

    Raises:
        Exception: If any cleanup operation fails
    """
    logger = logging.getLogger(__name__)
    try:
        # 1. Clean up old ETL records
        cutoff_date = timezone.now() - timedelta(days=30)
        old_records = ExtractTransformLoad.objects.filter(
            date_completed__lt=cutoff_date,
            status='completed'
        )
        deleted_count = old_records.count()
        old_records.delete()
        logger.info(f"Removed {deleted_count} completed ETL records older than 30 days")

        # 2. Add additional cleanup operations here as needed
        # For example:
        # - Remove temporary files
        # - Archive old logs
        # - Clean up failed processing artifacts

        # 3. Log cleanup summary
        cleanup_stats = {
            'etl_records_removed': deleted_count,
            'cleanup_time': timezone.now().isoformat()
        }
        logger.info(f"Cleanup task completed successfully: {cleanup_stats}")
        return cleanup_stats

    except Exception as e:
        logger.error(f"Cleanup task failed: {str(e)}", exc_info=True)
        raise  # Re-raise the exception to mark the task as FAILED


def process_etl_data(filtered_data_ids):
    # Fetch model objects for processing
    filtered_data = ExtractTransformLoad.objects.filter(id__in=filtered_data_ids)

    # Perform the processing logic here
    for etl in filtered_data:
        process_data(etl)
    return "Processing Complete"

def process_data(etl):
    # Simulate processing by performing operations on the ETL data
    pass


@shared_task(bind=True, queue='celery')
def test_celery_queue(self):
    """Test task for celery queue."""
    return {'queue': 'celery', 'status': 'success', 'task_id': str(self.request.id)}


@shared_task(bind=True, queue='imagery')
def test_imagery_queue(self):
    """Test task for imagery queue."""
    return {'queue': 'imagery', 'status': 'success', 'task_id': str(self.request.id)}