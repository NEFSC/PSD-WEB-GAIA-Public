import os
import django
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.contrib.auth.decorators import login_required
from ..models import Annotations

########################################################################################################################
#
#  In Django, a view is what takes a Web request and returns a Web response. The response can be many things, but most
#  of the time it will be a Web page, a redirect, or a document. In this case, the response will almost always be data
#  in JSON format.
#
########################################################################################################################

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gaia.settings')
os.environ["CPL_DEBUG"] = "ON" # Should enable GDAL debuggin
django.setup()

def access_denied(request):
    return render(request, 'access_denied.html', status=403)

@login_required
@csrf_protect
def reset_annotation_counter(request):
    """Reset the session annotation counter to 0"""
    if request.method == 'POST':
        request.session['annotation_count'] = 0
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})

@login_required
def get_annotation_counts(request):
    """API endpoint to get current annotation counts"""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)
    
    try:
        # Get session count
        session_count = request.session.get('annotation_count', 0)
        
        # Get total count from database (could be implemented later)
        total_count = Annotations.objects.filter(user=request.user).count()
        
        return JsonResponse({
            'success': True,
            'session_count': session_count,
            'total_count': total_count
        })
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)













