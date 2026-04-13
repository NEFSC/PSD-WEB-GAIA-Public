import traceback
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.conf import settings

try:
    from azure.storage.blob import BlobServiceClient
except ImportError:  # Fail gracefully if azure lib isn't available in some environments
    BlobServiceClient = None  # type: ignore


@login_required
def cog_preview_list(request):
    """Barebones page listing all COGs in the Azure 'cogs/' directory and previewing one on a map.

    - Lists blobs under prefix 'cogs/' (one level) sorted by newest (last_modified desc)
    - Clicking a COG loads it into an OpenLayers GeoTIFF layer on the right pane
    - Uses existing /cogs/<vendor_id>/ endpoint to stream bytes with SAS
    """
    cogs = []
    error = None

    if BlobServiceClient is None:
        error = "azure-storage-blob package not installed; cannot list COGs."
    else:
        try:
            account_name = getattr(settings, 'AZURE_STORAGE_ACCOUNT_NAME', None)
            account_key = getattr(settings, 'AZURE_STORAGE_ACCOUNT_KEY', None)
            container_name = getattr(settings, 'AZURE_CONTAINER_NAME', None)

            if not all([account_name, account_key, container_name]):
                error = "Azure storage settings are not fully configured."
            else:
                conn_str = (
                    f"DefaultEndpointsProtocol=https;AccountName={account_name};"
                    f"AccountKey={account_key};EndpointSuffix=core.windows.net"
                )
                service = BlobServiceClient.from_connection_string(conn_str)
                container = service.get_container_client(container_name)
                # List with prefix 'cogs/'
                blobs = container.list_blobs(name_starts_with='cogs/')
                for b in blobs:
                    # Skip directories / non .tif files if any
                    name_part = b.name.split('cogs/', 1)[1] if 'cogs/' in b.name else b.name
                    if not name_part or '/' in name_part:  # keep only direct children (no deeper paths)
                        continue
                    if not (name_part.lower().endswith('.tif') or name_part.lower().endswith('.tiff')):
                        continue
                    cogs.append({
                        'name': name_part,
                        'last_modified': b.last_modified,
                        'size': b.size,
                    })
                # Sort newest first
                cogs.sort(key=lambda x: x['last_modified'] or 0, reverse=True)
        except Exception as e:  # pragma: no cover - defensive
            error = f"Error listing COGs: {e}"
            traceback.print_exc()

    context = {
        'cogs': cogs,
        'error': error,
    }
    return render(request, 'cog_preview.html', context)
