"""
Collection of functions for building dataset-search and scene-search queries for Earth Explorer (EE) and Maxar Geospatial Portal (MGP).
Functions:
    geojson_for_ee(geojson: dict) -> dict:
        Helper function to build the 'geoJson' value payload for spatial filters.
        Only supports polygon geometries currently.
    build_cloud_cover_filter(minimum: int = 0, maximum: int = 100, include_unknown: bool = True) -> dict:
        Builds cloud cover bandpass filter with parameters for min/max coverage and unknown values.
    build_spatial_filter(geojson: dict) -> dict:
        Converts GeoJSON to EarthExplorer spatial filter format. Only supports polygons.
        Expects EPSG:4326 coordinates.
    build_acqusition_filter(start: str, end: str) -> dict:
        Creates temporal filter using ISO 8601 datetime strings.
    build_dataset_filter(acquisition: dict, spatial: dict) -> dict:
        Wrapper to build dataset search API query from acquisition and spatial filters.
    build_scene_filter(acquisition: dict, spatial: dict, cloud: dict) -> dict:
        Wrapper to build scene search API query from acquisition, spatial and cloud filters.
    build_ee_query_payload(start_date: str, end_date: str, aoi: dict) -> str:
        Builds complete Earth Explorer API query payload as JSON string.
    query_mgp(username: str, password: str, collections: list, start: str, end: str, 
              where: str, limit: int, export: str = None, bbox: list = None, geometry: dict = None) -> list:
        Performs spatio-temporal query against Maxar Geospatial Portal STAC API.
        Returns API response and list of catalog IDs matching query parameters.
These functions make use of work found at: https://github.com/yannforget/landsatxplore
"""

import sys
import json
import requests
import pandas as pd
from .security import mgp_login

def geojson_for_ee(geojson:dict):
    """ Helper function to build the 'geoJson' value payload
            for the BUILD_SPATIAL_FILTER function.

        Ref. https://github.com/yannforget/landsatxplore/blob/master/landsatxplore/api.py#L439-L469
    """
    geojson_payload = {}
    geojson_payload['type'] = geojson['type']
    
    if geojson['type'] == "Polygon":
        geojson_payload['coordinates'] =  geojson['coordinates']
    else:
        print("Your GeoJSON type is not a polygon and therefore is not")
        print("\tsupported at this time. Please supply a polygon!")

    return geojson_payload

def build_cloud_cover_filter(minimum=0, maximum=100, include_unknown=True):
    """ Builds a cloud cover bandpass filter and unknown flag. Include images between
            the minimum and maximum cloud cover percentages as well as those
            where cloud cover has not been precalculated.

        MINIMUM - Minimum cloud cover percentage for your area of interest.
        MAXIMUM - Maximum cloud cover percentage for your area of interest.
        INCLUDE_UNKNON - Include images where cloud cover has not been calculated.

        Ref. https://github.com/yannforget/landsatxplore/blob/master/landsatxplore/api.py#L255-L259
        Ref. https://github.com/yannforget/landsatxplore/blob/master/landsatxplore/api.py#L523-L539
    """
    cc_payload = {}
    cc_payload["min"] = minimum
    cc_payload["max"] = maximum
    cc_payload["includeUnknown"] = include_unknown
    return cc_payload

def build_spatial_filter(geojson:dict):
    """ Builds a spatial filter by converting GeoJSON to an
            EarthExplorer-ready spatial filter. Expects GeoJSON
            to be in EPSG:4326 since it flips latitude and
            longitude to longitude and latitude.

        Is dependent on the GEOJSON_FOR_EE function above.

        *ONLY SUPPORTS POLYGONS AT THIS TIME*
            Future work should include Points.
            Lines are unlikely to be needed for this work.

        PAYLOAD - A request payload dictionary
        GEOJSON - A GeoJSON string likely loaded by reading in a
            geojson file.

        Ref. https://github.com/yannforget/landsatxplore/blob/master/landsatxplore/api.py#L493-L504
        Ref. https://github.com/yannforget/landsatxplore/blob/master/landsatxplore/api.py#L439-L469
    """
    spatial_payload = {}
    spatial_payload['filterType'] = "geojson"
    spatial_payload['geoJson'] = geojson_for_ee(geojson)
    
    return spatial_payload

def build_acqusition_filter(start:str, end:str):
    """ Builds a temporal filter using ISO 8601 datetime format.

        START - Start date for data acquisition. At minimum,
            YYYY-MM-DD, but can also be 2020-07-10 15:00:00.000
            which includes hours, minutes, seconds, and milliseconds.
        END - End date in the same format listed for START.

        Ref. https://github.com/yannforget/landsatxplore/blob/master/landsatxplore/api.py#L507-L520
    """
    aq_payload = {}
    aq_payload["start"] = start
    aq_payload["end"] = end
    return aq_payload

def build_dataset_filter(acquisition, spatial):
    """ Wrapper function to build DATASET-SEARCH API query
            from acquisition and spatial filter outputs.
            outputs.

        ACQUSITION - Acquisition filter.
        SPATIAL - Spatial filter.

        Ref. https://github.com/yannforget/landsatxplore/blob/master/landsatxplore/api.py#L563-L597
    """
    payload = {}
    payload['acquisitionFilter'] = acquisition
    payload['spatialFilter'] = spatial
    return payload

def build_scene_filter(acquisition, spatial, cloud):
    """ Wrapper function to build SCENE-SEARCH API query
            from acquisition, spatial, and cloud filter
            outputs.

        ACQUSITION - Acquisition filter.
        SPATIAL - Spatial filter.
        CLOUD - Cloud cover filter.
        MONTHS - Seasonal filter (month numbers from 1 to 12) # Not used

        Ref. https://github.com/yannforget/landsatxplore/blob/master/landsatxplore/api.py#L563-L597
    """
    payload = {}
    payload['acquisitionFilter'] = acquisition
    payload['spatialFilter'] = spatial
    payload['cloudCoverFilter'] = cloud
    return payload

def build_ee_query_payload(start_date, end_date, aoi, imagery_dataset='crssp_orderable_w3'):
    payload = {}
    datasetName = imagery_dataset
    max_results = 100

    data_filter = build_scene_filter(
        acquisition = build_acqusition_filter(start_date, end_date),
        spatial = build_spatial_filter(aoi),
        cloud = build_cloud_cover_filter()
    )
    
    params = {"datasetName": datasetName,
              "scene": data_filter,
              "maxResults": max_results,
              "metadataType": "full",}
    
    return json.dumps(params)

def query_mgp(username, password, collections, start, end, where, limit, export=None, bbox=None, geometry=None):
    """ Performs a spatio-temporal query against Maxar Geospatial Portal (MGP)
            STAC. Queries should include time range (start, end) and spatial
            constraints (BBOX or Geometry). Queries can include no collections,
            to return all, or any collections of interest (e.g., wv02, wv03-swir).
            Limits on the maximum number of returned datasets can also be placed.
            Limits help reduce querying times. Export is used for exporting query
            results to disk.

        COLLECTIONS - STAC API collections to query for when given a BBOX or
            geometry. For example, wv02 or wv03-swir.
        START - Start date in the format YYYY-MM-DD
        END - End date in the format YYYY-MM-DD
        WHERE - Common Query Langauge (CQL) where clause to filter results.
            For example, "eo:cloud_cover <= 50 AND view:off_nadir <= 40" filters
            electro-optical images with cloud cover over 50% and off Nadir angles
            of greater-than 40-degrees.
        LIMIT - Limit as a number
        EXPORT - Path to output GeoJSON file.
        BBOX - Bounding box given by lower lefthand and upper righthand points.
            These can be easily returned by Shapely. This variable is exclusive
            of GEOMETRY.
        GEOMETRY - Single polygon geometry in GeoJSON format which is easily
            returned by Shapely. This variable is exclusive of BBOX.
    """
    headers = mgp_login(username, password)
    params = {
        #"collections":"",
        "bbox": "",
        "datetime": f"{start}/{end}",
        "where": where,
        "intersects": "",
        "limit": 100,
        "page": 1,
    }
    
    if collections is not None:
        collections = [collection.lower() for collection in collections]
        params["collections"]=collections
    
    if bbox is not None:
        bbox = ",".join(bbox)
        params["bbox"] = [float(v) for v in bbox.split(",")]
    
    if geometry is not None:
        try:
            with open(geometry, "r") as geo:
                data = json.load(geo)
        except:
            try:
                data = json.loads(geometry)
            except:
                data = geometry
        try:
            for things in data["features"]:
                geom = things["geometry"]["coordinates"]
                params["intersects"] = {"type": "Polygon", "coordinates": geom}
        except:
            params["intersects"] = data
    
    if limit is not None:
        params["limit"] = int(limit)
    
    if geometry and bbox is not None:
        sys.exit("You can only pass bounding box or intersection geometry")
    
    if all(v is None for v in [geometry, bbox]):
        sys.exit("You must pass either intersection geometry or bounding box")
    
    params = {k: v for k, v in params.items() if v}
    print(json.dumps(params,indent=2))
    response = requests.post(
        f"https://api.maxar.com/discovery/v1/search",
        data=json.dumps(params),
        headers=headers,
        timeout=120,
    )
    
    if response.status_code == 200:
        df = pd.DataFrame(response.json()["features"])
        r, c = df.shape
        if r>0:
            print(f"Total of {r} objects returned")
            catid_list = df['id'].tolist()
            if export != None:
                with open(export, 'w') as output_file:
                    json.dump(response.json(), output_file, indent=2)
            return [response,catid_list]
        else:
            print('Search returned no results')
            return [0,0]
    
    else:
        print(
            f"Failed with status code {response.status_code} & error {response.text}")