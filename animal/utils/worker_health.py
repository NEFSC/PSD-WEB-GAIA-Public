from celery.app.control import Inspect
from kombu.exceptions import OperationalError
import time
import logging

def verify_celery_connection(app, max_attempts=20, retry_delay=5, timeout=60):
    """
    Verify that Celery workers are available and properly registered.
    
    Args:
        app: Celery app instance
        max_attempts: Maximum number of connection attempts
        retry_delay: Seconds to wait between attempts
        timeout: Seconds to wait for worker response
        
    Returns:
        bool: True if workers are healthy, False otherwise
    """
    logger = logging.getLogger(__name__)
    inspector = app.control.inspect(timeout=timeout)
    
    # Required tasks that should be registered
    required_tasks = {
        'gaia.imagery.download',
        'gaia.imagery.calibrate',
        'gaia.imagery.pansharpen',
        'gaia.imagery.create_cogs',
        'gaia.imagery.upload_azure',
        'gaia.imagery.cleanup'
    }

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"Checking worker health (attempt {attempt}/{max_attempts})...")
            
            # Test broker connection with detailed logging
            try:
                # Initialize connection with debug info
                conn = app.connection()
                logger.info(f"Attempting connection to broker: {conn.as_uri()}")
                logger.info(f"Connection settings: {conn.info()}")
                
                # Test connection
                conn.ensure_connection(timeout=timeout)
                logger.info("Successfully connected to Redis broker")
                
                # Check registered tasks
                registered = inspector.registered()
                if registered:
                    logger.info("Found registered workers:")
                    for worker_name, tasks in registered.items():
                        logger.info(f"Worker: {worker_name}")
                        logger.info(f"Tasks: {tasks}")
                        
                        # Check for required tasks
                        worker_tasks = {task.split('[')[0].strip() for task in tasks}
                        missing_tasks = required_tasks - worker_tasks
                        if missing_tasks:
                            logger.error(f"Worker {worker_name} is missing required tasks: {missing_tasks}")
                        else:
                            logger.info(f"Worker {worker_name} has all required tasks")
                else:
                    logger.error("No registered workers found!")
                
                # Get detailed connection info
                transport_info = {
                    'driver': conn.transport.driver_type,
                    'host': conn.transport.client.connection_pool.connection_kwargs.get('host'),
                    'port': conn.transport.client.connection_pool.connection_kwargs.get('port'),
                    'db': conn.transport.client.connection_pool.connection_kwargs.get('db'),
                }
                logger.debug(f"Transport details: {transport_info}")
                
                # Check broker health
                try:
                    ping_ok = conn.channel().connection.ping()
                    logger.debug(f"Broker ping test: {'OK' if ping_ok else 'FAILED'}")
                    
                    # Get Redis info
                    redis_info = conn.channel().connection.info()
                    logger.debug(f"Redis server info: Connected clients: {redis_info.get('connected_clients')}, "
                              f"Memory used: {redis_info.get('used_memory_human')}, "
                              f"Uptime: {redis_info.get('uptime_in_seconds')}s")
                except Exception as e:
                    logger.warning(f"Could not get detailed Redis info: {e}")
                
            except OperationalError as e:
                logger.error(f"Failed to connect to broker: {e}")
                logger.error(f"Connection parameters: {app.conf.broker_url}")
                raise
                
            # Check for active workers
            active = inspector.active()
            registered = inspector.registered()
            
            if not active or not registered:
                logger.warning(f"Attempt {attempt}/{max_attempts}: Workers not fully initialized, waiting {retry_delay}s...")
                time.sleep(retry_delay)
                continue
                
            # Get all registered task names
            all_tasks = set()
            if registered:
                for worker, tasks in registered.items():
                    # Tasks are returned as strings with rate limit info
                    # Example: "task.name [rate_limit=10/m]"
                    all_tasks.update(task.split('[')[0].strip() for task in tasks)
                
                logger.info(f"Registered tasks: {all_tasks}")
                
                # Check if all required tasks are registered
                missing_tasks = required_tasks - all_tasks
                if missing_tasks:
                    logger.warning(f"Missing required tasks: {missing_tasks}")
                    logger.info(f"Available tasks: {all_tasks}")
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.info("All required tasks are registered")
            else:
                logger.warning("No tasks registered with any workers")
                time.sleep(retry_delay)
                continue
                
            # Check worker stats
            stats = inspector.stats()
            if not stats:
                logger.warning("No worker statistics available")
                time.sleep(retry_delay)
                continue
                
            # Log worker information
            for worker, info in stats.items():
                logger.info(f"Worker {worker}:")
                logger.info(f"  - Processes: {info.get('pool', {}).get('processes', [])}")
                logger.info(f"  - Max tasks: {info.get('pool', {}).get('max-concurrency', 'unknown')}")
                logger.info(f"  - Broker: {info.get('broker', {}).get('hostname', 'unknown')}")
            
            logger.info("Worker health check passed!")
            return True
            
        except Exception as e:
            logger.warning(f"Health check failed: {str(e)}")
            if attempt < max_attempts:
                time.sleep(retry_delay)
            continue
            
    logger.error("No workers detected after all retries")
    return False
