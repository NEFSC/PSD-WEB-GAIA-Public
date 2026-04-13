# Functions for downloading data
#
# These functions make use of work found at: https://m2m.cr.usgs.gov/api/docs/example/download_data-py

import os
import re
import time
import datetime
from zipfile import ZipFile
import json

def unzip_download(zippedfile):
    """ Unzips downloaded data from EarthExplorer.

        ZIPPEDFILE - Locally stored zipped dataset
    """
    root = '/'.join(zippedfile.split('/')[:-1])
    dirname = zippedfile.split('/')[-1].split('.')[0]
    outdir = root + '/' + dirname
    with ZipFile(zippedfile, 'r') as zObject:
        zObject.extractall(outdir)
    print("Unzipping complete!\n\n")
    os.remove(zippedfile)
    return os.path.abspath(outdir)

def download_zip(session, url, out_dir):
    """ Downloads zipped data from EarthExplorer when provided with
            the URL returning the output name.

        URL - EarthExplorer supplied URL to download data.
    """
    response = session.get(url, stream=True)
    headers = response.headers['content-disposition']
    filename = re.findall("filename=(.+)", headers)[0].replace('"','')
    print("Your files are: {}".format(filename))
    print("Your data are being saved to: {}".format(os.path.abspath(out_dir)))
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    outname = out_dir + filename
    print("Downloading: {}".format(filename))
    with open(outname, "wb") as dst:
        dst.write(response.content)
    return outname

def retrieve_download(session, label):
    """ Retreives download URLs for prepared, or staged, datasets.

        LABEL - Dataset label as provided to the "download-request"
            API endpoint.
    """
    data = {'label': label}
    url = "https://m2m.cr.usgs.gov/api/api/json/stable/download-retrieve"
    response = session.post(url=url, data=json.dumps(data))
    ra = response.json()['data']['available']
    print("Your data had to be retrieved.")
    return [dataset['downloadId'] for dataset in ra]

def request_download(session, entity_id, dataset_id, attempts=0):
    """ Requests the download URL when provided with the Entity ID,
            retrieved by querying against the "scene-search" API
            endpoint, and the Product ID, created from GET PRODUCT
            ID, returning the stagged dataset label and URL. Uses
            recursion to ensure the download is returned where the
            a sleep function of five seconds is applied per attempted
            retreval.

        Is partially dependent on the RETRIEVE DOWNLOAD function to
            retrieve download URLs for prepared, or staged, datasets.

        ENTITY ID - Dataset Entity ID which can be retrieved by querying
            against the "scene-search" API endpoint.
        DATASET ID - A Dataset ID, or Product ID, created by GET PRODUCT
            ID.
        ATTEMPTS - The number of recursive attempts that have been made
            to retreve the download URL for the requested data.
    """
    entity_product = [{'entityId': entity_id, 'productId': dataset_id}]
    label = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    data = {'downloads': entity_product, 'label': label}
    url = "https://m2m.cr.usgs.gov/api/api/json/stable/download-request"
    response = session.post(url=url, data=json.dumps(data))
    #print(f"Your request response looks like {response.json()}\n\n")
    try:
        download_id = response.json()['data']['preparingDownloads'][0]['downloadId']
        print("Your download is being prepared!")
        download_id = retrieve_download(label)
        sleep_time = 5 * attempts
        print("Sleeping for {} seconds.".format(sleep_time))
        time.sleep()
        attempts = attempts + 1
        label, download_id = request_download(session, entity_id, dataset_id, attempts)
    except:
        try:
            download_id = response.json()['data']['availableDownloads'][0]['url']
            print("Your download is available!")
        except Exception as e:
            print("Failed on Product ID {}".format(dataset_id))
            print(f"\t\tYour payload looks like: {json.dumps(data)}\n")
            print(f"\t\tYour response looks like: {response.json()}\n")
            # print("\tThe exception is: {}".format(e))
            download_id = 999999999
            raise e
    return label, download_id

def get_product_id(session, dataset_name, entity_id):
    """ Creates a Product ID, or Dataset ID, by querying against the
            "download-options" API endpoint using the Dataset Name,
            retrieved by querying against the "dataset-search" API
            endpoint, and the Entity ID, retrieved by querying
            against the "scene-search" API endpoint.

        DATASETNAME - Dataset Name which can be retrieved by querying
            against the "dataset-search" API endpoint.
        ENTITY ID - Dataset Entity ID which can be retrieved by querying
            against the "scene-search" API endpoint.
    """
    data = {'datasetName': dataset_name, 'entityIds': entity_id}
    url = "https://m2m.cr.usgs.gov/api/api/json/stable/download-options"
    request = session.post(url=url, data=json.dumps(data))
    rd = request.json()['data'][0]
    print(f"Your response looks like {rd}\n")
    print("Your data are {} bytes in size!".format(rd['filesize']))
    return rd['id']

def download_imagery(session, datasetName, entity_id, out_dir, max_retries=5):
    """ Wrapper function which generates an EarthExplorer product ID,
            requests the download, retrieves the download URL, downloads
            the zip file locally, and then unzips it locally.

        Is dependent on the GET PRODUCT ID, REQUEST DOWNLOAD, RETRIEVE
            DOWNLOAD, DOWNLOAD ZIP, and UNZIP DOWNLOAD functions.

        DATASETNAME - Dataset Name which can be retrieved by querying
            against the "dataset-search" API endpoint.
        ENTITY ID - Dataset Entity ID which can be retrieved by querying
            against the "scene-search" API endpoint.
    """
    for attempt in range(max_retries):
        try:
            dataset_id = get_product_id(session, datasetName, entity_id)
            label, download_id = request_download(session, entity_id, dataset_id)
            if download_id != 999999999:
                ready_download_ids = retrieve_download(session, label)
                zippedfile = download_zip(session, download_id, out_dir)
                return unzip_download(zippedfile)
        except Exception as e:
            print(f"Attempt {attempt + 1} failed: {e}")
            print("\tFailure could have been due to staging\n")
            if attempt < max_retries - 1:
                time.sleep(10)
            else:
                print("Max reties reached. Download failed.\n\n")