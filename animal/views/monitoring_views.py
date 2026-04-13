"""
Pipeline Monitoring Dashboard Views
Provides real-time monitoring of Celery tasks, pipeline status, and system health
"""

import json
import redis
from datetime import datetime, timedelta
from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from celery import current_app
from animal.utils.logging import get_animal_logger

logger = get_animal_logger(__name__)

def get_redis_connection():
    """Get Redis connection for queue monitoring"""
    try:
        r = redis.Redis(host='psd-web-gaia-redis-1', port=6379, db=0, decode_responses=True)
        r.ping()
        return r
    except:
        # Fallback to localhost if container name doesn't work
        try:
            r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            r.ping()
            return r
        except:
            return None

@login_required
def monitoring_dashboard(request):
    """Main monitoring dashboard view"""
    return render(request, 'monitoring/dashboard.html')


def api_system_status(request):
    """API endpoint for system status"""
    try:
        # Get Redis connection
        redis_conn = get_redis_connection()
        redis_status = "Connected" if redis_conn else "Disconnected"
        
        # Get Celery inspector
        inspector = current_app.control.inspect()
        
        # Get active tasks
        active_tasks = inspector.active() or {}
        
        # Get queue lengths
        queue_lengths = {}
        if redis_conn:
            try:
                queue_lengths = {
                    'celery': redis_conn.llen('celery'),
                    'imagery': redis_conn.llen('imagery'),
                }
            except Exception as e:
                logger.error(f"Error getting queue lengths: {e}")
                queue_lengths = {'error': str(e)}
        
        # Get worker stats
        worker_stats = inspector.stats() or {}
        
        # Calculate totals
        total_active_tasks = sum(len(tasks) for tasks in active_tasks.values())
        total_workers = len(worker_stats)
        
        system_status = {
            'timestamp': datetime.now().isoformat(),
            'redis_status': redis_status,
            'total_workers': total_workers,
            'total_active_tasks': total_active_tasks,
            'queue_lengths': queue_lengths,
            'worker_stats': worker_stats,
            'active_tasks': active_tasks
        }
        
        return JsonResponse(system_status)
        
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        return JsonResponse({
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }, status=500)


def api_pipeline_tasks(request):
    """API endpoint for pipeline-specific task monitoring"""
    try:
        inspector = current_app.control.inspect()
        active_tasks = inspector.active() or {}
        
        # Filter for pipeline tasks
        pipeline_tasks = []
        
        for worker, tasks in active_tasks.items():
            for task in tasks:
                task_name = task.get('name', '')
                task_id = task.get('id', '')
                
                # Check if this is a pipeline task
                if any(pipeline_step in task_name for pipeline_step in [
                    'prepare_workspace', 'login_and_search', 'download_imagery',
                    'organize_and_calibrate', 'run_pansharpen', 'run_cog_creation',
                    'upload_to_azure', 'cleanup_local_data', 'load_points'
                ]):
                    # Extract chain ID from task ID if possible
                    chain_id = task_id.split(':')[0] if ':' in task_id else task_id[:8]
                    
                    pipeline_task = {
                        'chain_id': chain_id,
                        'task_name': task_name,
                        'task_id': task_id,
                        'worker': worker,
                        'args': task.get('args', []),
                        'kwargs': task.get('kwargs', {}),
                        'time_start': task.get('time_start'),
                        'acknowledged': task.get('acknowledged'),
                    }
                    pipeline_tasks.append(pipeline_task)
        
        return JsonResponse({
            'pipeline_tasks': pipeline_tasks,
            'total_pipeline_tasks': len(pipeline_tasks),
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error getting pipeline tasks: {e}")
        return JsonResponse({
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }, status=500)


def api_worker_health(request):
    """API endpoint for worker health monitoring"""
    try:
        inspector = current_app.control.inspect()
        
        # Get comprehensive worker information
        registered_tasks = inspector.registered() or {}
        active_queues = inspector.active_queues() or {}
        worker_stats = inspector.stats() or {}
        reserved_tasks = inspector.reserved() or {}
        
        worker_health = []
        
        for worker in worker_stats.keys():
            stats = worker_stats.get(worker, {})
            
            health_info = {
                'worker': worker,
                'status': 'online',
                'registered_tasks': len(registered_tasks.get(worker, [])),
                'active_queues': [q['name'] for q in active_queues.get(worker, [])],
                'total_tasks': stats.get('total', {}),
                'reserved_tasks': len(reserved_tasks.get(worker, [])),
                'clock_info': stats.get('clock', 'N/A'),
                'rusage': stats.get('rusage', {}),
                'pool_info': stats.get('pool', {})
            }
            worker_health.append(health_info)
        
        return JsonResponse({
            'workers': worker_health,
            'total_workers': len(worker_health),
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error getting worker health: {e}")
        return JsonResponse({
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }, status=500)


def api_task_history(request):
    """API endpoint for recent task history"""
    try:
        # This would typically come from a task result backend
        # For now, we'll provide structure for task history
        
        # In a full implementation, you'd query task results from:
        # - Celery result backend (Redis/Database)
        # - Application logs
        # - Custom task tracking database
        
        task_history = {
            'recent_completed': [],
            'recent_failed': [],
            'total_completed_today': 0,
            'total_failed_today': 0,
            'average_execution_time': 0,
            'timestamp': datetime.now().isoformat()
        }
        
        # TODO: Implement actual task history retrieval
        # This would involve:
        # 1. Querying Celery result backend
        # 2. Parsing log files for completed tasks
        # 3. Database queries for task tracking
        
        return JsonResponse(task_history)
        
    except Exception as e:
        logger.error(f"Error getting task history: {e}")
        return JsonResponse({
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }, status=500)


def api_kill_task(request):
    """API endpoint to terminate a specific task"""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    
    try:
        data = json.loads(request.body)
        task_id = data.get('task_id')
        
        if not task_id:
            return JsonResponse({'error': 'task_id required'}, status=400)
               
        return JsonResponse({
            'message': f'Task {task_id} terminated',
            'task_id': task_id,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error terminating task: {e}")
        return JsonResponse({
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }, status=500)

def api_clear_queue(request):
    """API endpoint to clear all Celery queues and Redis data"""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    
    try:
        data = json.loads(request.body)
        confirm = data.get('confirm', False)
        
        if not confirm:
            return JsonResponse({'error': 'Confirmation required'}, status=400)
        
        logger.warning("Manual queue clear requested - clearing all Redis/Celery data")
        
        # Get Redis connection and clear all data
        redis_conn = get_redis_connection()
        if not redis_conn:
            return JsonResponse({'error': 'Cannot connect to Redis'}, status=500)
    
        
        # Clear all Redis data (queues, results, cache)
        redis_conn.flushall()
        
        return JsonResponse({
            'message': 'Queue cleared successfully',
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error clearing queue: {e}")
        return JsonResponse({
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }, status=500)
