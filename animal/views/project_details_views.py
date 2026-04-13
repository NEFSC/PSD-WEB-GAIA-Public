from django.contrib.auth.decorators import login_required
from django.db.models import Count, Min
from django.shortcuts import get_object_or_404, render

from animal.models import ImageryLoadRequest, PointsOfInterest, Project, ProjectAccess


@login_required
def project_details_page(request, project_id):
    project = get_object_or_404(Project.objects.select_related('owner'), id=project_id)

    access_users = (
        ProjectAccess.objects.filter(project_id=project_id, user__isnull=False)
        .select_related('user')
        .values('user__id', 'user__username', 'user__first_name', 'user__last_name', 'user__email')
        .order_by('user__username')
    )

    if project.owner_id and not any(row['user__id'] == project.owner_id for row in access_users):
        access_users = list(access_users)
        access_users.append({
            'user__id': project.owner_id,
            'user__username': project.owner.username,
            'user__first_name': project.owner.first_name,
            'user__last_name': project.owner.last_name,
            'user__email': project.owner.email,
        })
    else:
        access_users = list(access_users)

    vendor_rows = (
        PointsOfInterest.objects.filter(project_id=project_id)
        .exclude(vendor_id__isnull=True)
        .exclude(vendor_id='')
        .values('vendor_id')
        .annotate(
            total_pois=Count('id', distinct=True),
            catalog_id=Min('catalog_id'),
            sensor=Min('sensor'),
            acquisition_date=Min('date_image_taken'),
        )
        .order_by('vendor_id')
    )

    cogs = []
    for row in vendor_rows:
        vendor_id = row['vendor_id']

        cogs.append({
            'vendor_id': vendor_id,
            'catalog_id': row['catalog_id'] or 'Unknown',
            'sensor': row['sensor'] or 'Unknown',
            'acquisition_date': row['acquisition_date'],
            'total_pois': row['total_pois'],
            'fully_annotated': None,
            'fully_validated': None,
        })

    request_rows = (
        ImageryLoadRequest.objects.filter(project_id=project_id)
        .select_related('requested_by_user')
        .order_by('-requested_at')
    )

    grouped_requests = {}
    for req in request_rows:
        group_key = req.request_group_id or req.chain_id
        group = grouped_requests.get(group_key)
        if group is None:
            group = {
                'group_key': group_key,
                'requested_by_user': req.requested_by_user,
                'requested_by_username': req.requested_by_username,
                'aoi_name': req.aoi_name or 'Unknown',
                'requested_at': req.requested_at,
                'total_count': 0,
                'failed_count': 0,
                'loaded_count': 0,
                'catalog_ids': set(),
            }
            grouped_requests[group_key] = group

        group['total_count'] += 1
        if req.status == ImageryLoadRequest.STATUS_FAILED:
            group['failed_count'] += 1
        elif req.status == ImageryLoadRequest.STATUS_LOADED:
            group['loaded_count'] += 1

        if req.requested_at < group['requested_at']:
            group['requested_at'] = req.requested_at

        if req.aoi_name and group['aoi_name'] == 'Unknown':
            group['aoi_name'] = req.aoi_name

        if req.catalog_ids:
            for raw_catalog_id in str(req.catalog_ids).split(','):
                catalog_id = raw_catalog_id.strip()
                if catalog_id:
                    group['catalog_ids'].add(catalog_id)

    recent_requests = []
    for group in grouped_requests.values():
        if group['failed_count'] > 0:
            status = ImageryLoadRequest.STATUS_FAILED
        elif group['loaded_count'] == group['total_count']:
            status = ImageryLoadRequest.STATUS_LOADED
        else:
            status = ImageryLoadRequest.STATUS_PROCESSING

        if status == ImageryLoadRequest.STATUS_LOADED:
            continue

        recent_requests.append({
            'requested_by_user': group['requested_by_user'],
            'requested_by_username': group['requested_by_username'],
            'requested_at': group['requested_at'],
            'aoi_name': group['aoi_name'],
            'catalog_ids_display': ', '.join(sorted(group['catalog_ids'])) if group['catalog_ids'] else 'Unknown',
            'status': status,
        })

    recent_requests.sort(key=lambda row: row['requested_at'], reverse=True)

    context = {
        'project': project,
        'access_users': access_users,
        'cogs': cogs,
        'recent_requests': recent_requests,
    }
    return render(request, 'project_details_page.html', context)
