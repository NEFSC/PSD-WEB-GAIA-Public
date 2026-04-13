# Basic stack
import os
import json
import django
from django.core.serializers import serialize
from django.shortcuts import render
from ..models import AreaOfInterest

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gaia.settings')
os.environ["CPL_DEBUG"] = "ON" # Should enable GDAL debuggin
django.setup()

def tasking_page(request):
    """ A simple page to show where Areas of Interest (AOIs) currently loaded
            in the SpatiaLite database are within the world.
    """
    aoi_objects = AreaOfInterest.objects.all()
    aoi_data = serialize('geojson', aoi_objects)
    aoi_data = json.loads(aoi_data)

    flattended_aoi_data = []
    for feature in aoi_data['features']:
        flattended_aoi_data.append({
            'id': feature['id'],
            'name': feature['properties']['name'],
            'requestor': feature['properties']['requestor'],
            'sqkm': feature['properties']['sqkm'],
            'geometry': feature['geometry'],
        })
    
    return render(request, 'tasking_page.html', {
        'aoi_data': flattended_aoi_data,
        'geojson_aoi_data': aoi_data
    })