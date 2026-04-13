from azure.storage.blob import BlobServiceClient
from pathlib import Path
from typing import Optional


def get_blob_service_client(account_name: str, account_key: str) -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(
        f"DefaultEndpointsProtocol=https;AccountName={account_name};"
        f"AccountKey={account_key};EndpointSuffix=core.windows.net"
    )


def download_blob_to_path(blob_service: BlobServiceClient, blob_uri: str, local_path: Path) -> Path:
    container = blob_uri.split(".net/")[1].split("/")[0]
    blob_name = "/".join(blob_uri.split(".net/")[1].split("/")[1:])
    blob_client = blob_service.get_blob_client(container, blob_name)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(blob_client.download_blob().readall())
    return local_path

def download_shapefile_from_blob(blob_uri: str, download_dir: Path, blob_service_client: BlobServiceClient):
    container_name = blob_uri.split(".net/")[1].split("/")[0]
    prefix = "/".join(blob_uri.split(".net/")[1].split("/")[1:])  # "shapefiles/UCIPlus"

    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=prefix)

    download_dir.mkdir(parents=True, exist_ok=True)
    for blob in blobs:
        filename = blob.name.split("/")[-1]
        destination = download_dir / filename
        with open(destination, "wb") as f:
            blob_data = container_client.download_blob(blob.name)
            f.write(blob_data.readall())
    return download_dir / f"{Path(prefix).name}.shp"  # Return full .shp path