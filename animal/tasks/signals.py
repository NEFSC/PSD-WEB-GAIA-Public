"""
Signal handlers for Celery task monitoring and diagnostics.
"""
import logging
from celery.signals import (
    before_task_publish,
    after_task_publish,
    task_prerun,
    task_postrun,
    task_success,
    task_failure,
    worker_init,
    worker_process_init,
    worker_ready,
    worker_shutdown,
    celeryd_after_setup
)
from redis import Redis
from django.conf import settings
from animal.utils.imagery_request_tracking import mark_failed, mark_loaded

logger = logging.getLogger(__name__)

# Task lifecycle monitoring
@before_task_publish.connect
def task_publish_handler(sender=None, headers=None, body=None, **kwargs):
    logger.info(f'[TaskTrace] Publishing task {sender} with id {headers.get("id")} to queue {headers.get("queue")}')

@after_task_publish.connect
def task_published_handler(sender=None, headers=None, **kwargs):
    logger.info(f'[TaskTrace] Published task {sender} with id {headers.get("id")}')

@task_prerun.connect
def task_prerun_handler(task_id=None, task=None, **kwargs):
    logger.info(f'[TaskTrace] Starting task {task.name} [{task_id}]')

@task_postrun.connect
def task_postrun_handler(task_id=None, task=None, retval=None, state=None, **kwargs):
    logger.info(f'[TaskTrace] Task {task.name} [{task_id}] completed with state {state}')
    if task_id and task_id.endswith(':load_points') and state == 'SUCCESS':
        chain_id = task_id.split(':', 1)[0]
        updated = mark_loaded(chain_id)
        if updated:
            logger.info(f'[TaskTrace] Marked imagery request {chain_id} as LOADED')

@task_success.connect
def task_success_handler(sender=None, result=None, **kwargs):
    logger.info(f'[TaskTrace] Task {sender.name} succeeded')

@task_failure.connect
def task_failure_handler(sender=None, task_id=None, exception=None, **kwargs):
    logger.error(f'[TaskTrace] Task {sender.name} [{task_id}] failed: {exception}')
    if task_id and ':' in task_id:
        chain_id = task_id.split(':', 1)[0]
        updated = mark_failed(chain_id, str(exception) if exception else 'Pipeline task failed')
        if updated:
            logger.error(f'[TaskTrace] Marked imagery request {chain_id} as FAILED')

# Worker lifecycle monitoring
@worker_init.connect
def worker_init_handler(sender=None, **kwargs):
    logger.info('[WorkerTrace] Worker process starting initialization')

@worker_process_init.connect
def worker_process_init_handler(sender=None, **kwargs):
    logger.info('[WorkerTrace] Worker subprocess initialized')

@worker_ready.connect
def worker_ready_handler(**kwargs):
    """Perform Redis connection verification when worker is ready."""
    logger.info('[WorkerTrace] Worker ready for tasks')
    try:
        redis_client = Redis.from_url(settings.CELERY_BROKER_URL)
        redis_info = redis_client.info()
        logger.info(f'[RedisStatus] Connected to Redis version {redis_info.get("redis_version", "unknown")}')
        
        # Check memory usage
        raw_peak = str(redis_info.get('used_memory_peak_perc', '0'))
        # Redis 7 returns values like '96.01%'; strip any trailing percent sign
        if raw_peak.endswith('%'):
            raw_peak = raw_peak[:-1]
        try:
            memory_peak_perc = float(raw_peak)
        except ValueError:
            memory_peak_perc = 0.0
        if memory_peak_perc > 90.0:
            logger.warning(f'[RedisStatus] High memory usage detected ({memory_peak_perc}%)')
            
    except Exception as e:
        logger.error(f'[RedisStatus] Connection check failed: {str(e)}', exc_info=True)
        # Don't raise the exception - let the worker continue even if Redis check fails

@worker_shutdown.connect
def worker_shutdown_handler(**kwargs):
    logger.info('[WorkerTrace] Worker shutting down')

@celeryd_after_setup.connect
def worker_setup_handler(sender, instance, **kwargs):
    logger.info(f'[WorkerTrace] Worker {sender} setup completed')
