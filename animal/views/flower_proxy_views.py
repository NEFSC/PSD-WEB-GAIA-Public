import httpx
import logging
import json
import re
from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.csrf import csrf_exempt

FLOWER_INTERNAL_URL = getattr(settings, 'FLOWER_INTERNAL_URL', None) or 'http://flower:5555'
# Flower now runs at root path; we rewrite /monitor[/...] -> /[/...]
FLOWER_PREFIX = ''

ALLOWED_RESP_HEADERS = {
    'content-type', 'cache-control', 'content-disposition', 'etag', 'content-length'
}

logger = logging.getLogger(__name__)


def _extract_requested_by_username(task_payload):
    """Extract requester username from task payload fields or kwargs text."""
    if not isinstance(task_payload, dict):
        return None

    direct = task_payload.get('requested_by_username')
    if direct:
        return str(direct)

    kwargs_obj = task_payload.get('kwargs')
    if isinstance(kwargs_obj, dict):
        req = kwargs_obj.get('requested_by_username')
        return str(req) if req else None

    if isinstance(kwargs_obj, str):
        match = re.search(
            r"[\"']requested_by_username[\"']\s*:\s*[\"']((?:\\.|[^\"'])*)[\"']",
            kwargs_obj,
        )
        if match and match.group(1):
            return match.group(1).replace("\\'", "'").replace('\\\"', '"')

    return None


def _filter_tasks_payload_for_user(tasks_payload, username):
    """Keep only tasks that were explicitly started by the given username."""
    if not isinstance(tasks_payload, dict):
        return tasks_payload

    filtered = {}
    for task_id, task_data in tasks_payload.items():
        requested_by = _extract_requested_by_username(task_data)
        if requested_by == username:
            filtered[task_id] = task_data
    return filtered

def _allow_user(user):
    # Restrict to active staff or superusers
    return user.is_authenticated and user.is_active and (user.is_superuser or user.is_staff)


@login_required(login_url='/account/login/')
def flower_proxy(request, subpath=''):
    """Reverse proxy to Flower so it is accessible under Django auth at /monitor/.

    Supports streaming responses (SSE) and basic methods. WebSockets are not proxied.
    """

    # Enforce permission (superuser or staff)
    if not _allow_user(request.user):
        return HttpResponseForbidden("Not authorized to access monitoring console")

    # Normalize upstream path (Flower root). Map /monitor/XYZ -> /XYZ
    clean_sub = subpath.strip('/') if subpath else ''
    upstream_path = '/' + clean_sub if clean_sub else '/'
    upstream_url = f"{FLOWER_INTERNAL_URL}{upstream_path}"

    method = request.method.upper()
    headers = {}
    # Forward selected headers (avoid Host to let httpx set correctly)
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in ('host', 'connection', 'keep-alive', 'upgrade'):  # skip hop-by-hop
            continue
        headers[k] = v

    data = request.body if method in {'POST', 'PUT', 'PATCH'} else None

    # Add Flower basic auth if configured
    flower_auth = getattr(settings, 'FLOWER_BASIC_AUTH', None)
    auth = None
    
    if flower_auth and ':' in flower_auth:
        username, password = flower_auth.split(':', 1)
        auth = (username, password)

    # Remove compression & hop-by-hop headers that can cause issues
    headers.pop('Accept-Encoding', None)
    headers.pop('Transfer-Encoding', None) 
    headers.pop('Connection', None)
    headers.pop('Content-Length', None)  # Let httpx handle this
    
    # Handle form data for DataTable POST requests
    form_data = None
    if method == 'POST' and request.content_type and 'application/x-www-form-urlencoded' in request.content_type:
        # For DataTable requests, use form data instead of raw body
        form_data = dict(request.POST.items())
        data = None
    
    try:
        # Use stream=False to get full response body and avoid chunked transfer issues
        # Don't follow redirects so we can rewrite Location headers
        logger.debug(f"FLOWER PROXY: Making {method} request to {upstream_url}")
        #logger.debug(f"FLOWER PROXY: Request headers: {dict(headers)}")

        auth = httpx.BasicAuth(username=auth[0], password=auth[1]) if auth else None
        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            if form_data:
                resp = client.request(method, upstream_url, params=request.GET, headers=headers, data=form_data, auth=auth)
            else:
                resp = client.request(method, upstream_url, params=request.GET, headers=headers, content=data, auth=auth)
                
            
    except httpx.RequestError as exc:
        logger.error("Flower proxy connection error: %s", exc)
        return HttpResponse(f"Flower upstream error: {exc}", status=502)

    # Get response content as bytes
    body = resp.content
    content_type = resp.headers.get('content-type', 'text/html')
    
    # Rewrite URLs in HTML content to add /monitor prefix
    if content_type and 'text/html' in content_type:
        try:
            html_content = body.decode('utf-8')
            # Rewrite common URL patterns that Flower uses
            import re
            
            # Fix absolute paths in href and src attributes
            html_content = re.sub(r'href="/', r'href="/monitor/', html_content)
            html_content = re.sub(r'src="/', r'src="/monitor/', html_content)
            html_content = re.sub(r"href='/'", r"href='/monitor/'", html_content)
            html_content = re.sub(r"src='/'", r"src='/monitor/'", html_content)
            
            # Fix JavaScript URL references and AJAX calls
            html_content = re.sub(r'url\s*:\s*"/', r'url: "/monitor/', html_content)
            html_content = re.sub(r"url\s*:\s*'/", r"url: '/monitor/", html_content)
            html_content = re.sub(r'fetch\s*\(\s*"/', r'fetch("/monitor/', html_content)
            html_content = re.sub(r"fetch\s*\(\s*'/", r"fetch('/monitor/", html_content)
            
            # Fix form actions
            html_content = re.sub(r'action="/', r'action="/monitor/', html_content)
            html_content = re.sub(r"action='/", r"action='/monitor/", html_content)
            
            # CRITICAL: Set the url_prefix hidden field for JavaScript to use
            # Handle different quote variations
            html_content = re.sub(
                r"<input type=['\"]hidden['\"] value=['\"]['\"] id=['\"]url_prefix['\"]>",
                '<input type="hidden" value="/monitor" id="url_prefix">',
                html_content
            )
            html_content = re.sub(
                r"<input type='hidden' value='' id='url_prefix'>",
                '<input type="hidden" value="/monitor" id="url_prefix">',
                html_content
            )
            html_content = re.sub(
                r'<input type="hidden" value="" id=\'url_prefix\'>',
                '<input type="hidden" value="/monitor" id="url_prefix">',
                html_content
            )

            # Tighten Flower layout and reduce task-name column footprint.
            compact_layout_css = """
<style id=\"gaia-flower-compact-layout\">
  .container, .container-fluid {
    max-width: 70% !important;
    margin-left: auto !important;
    margin-right: auto !important;
  }
  table.dataTable th:nth-child(2),
  table.dataTable td:nth-child(2) {
    width: 220px !important;
    max-width: 220px !important;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
"""
            if "gaia-flower-compact-layout" not in html_content:
                if "</head>" in html_content:
                    html_content = html_content.replace("</head>", compact_layout_css + "\n</head>", 1)
                else:
                    html_content = compact_layout_css + html_content
            
            body = html_content.encode('utf-8')
        except (UnicodeDecodeError, UnicodeEncodeError):
            # If encoding fails, use original body
            pass
    
    # Also handle JavaScript files
    elif content_type and ('application/javascript' in content_type or 'text/javascript' in content_type):
        logger.info(f"Processing JavaScript file with content-type: {content_type}")
        try:
            js_content = body.decode('utf-8')
            logger.info(f"Original JS file size: {len(js_content)} characters")
            import re
            
            # Only add debug marker to non-minified files to avoid syntax issues
            if '.min.js' not in upstream_path:
                js_content = "console.log('GAIA PROXY: JavaScript file modified by proxy');\n" + js_content
            
            # Fix common JavaScript URL patterns - be more careful with regex
            js_content = re.sub(r'\burl\s*:\s*"(/[^"]*)"', r'url: "/monitor\1"', js_content)
            js_content = re.sub(r"\burl\s*:\s*'(/[^']*)'", r"url: '/monitor\1'", js_content)
            js_content = re.sub(r'\bfetch\s*\(\s*"(/[^"]*)"', r'fetch("/monitor\1"', js_content)
            js_content = re.sub(r"\bfetch\s*\(\s*'(/[^']*)'", r"fetch('/monitor\1'", js_content)

            # Flower's /tasks/datatable endpoint can reject DataTables payloads.
            # Switch the tasks table to /api/tasks with client-side processing.
            if 'flower.js' in upstream_path:
                js_content = re.sub(
                    r"serverSide:\s*true",
                    "serverSide: false",
                    js_content,
                    count=1,
                )
                js_content = re.sub(
                    r"ajax:\s*\{\s*type:\s*'POST',\s*url:\s*url_prefix\(\)\s*\+\s*'/tasks/datatable'\s*\}",
                    "ajax: { url: url_prefix() + '/api/tasks', dataSrc: function(json) { return Object.keys(json || {}).map(function(key) { return json[key]; }); } }",
                    js_content,
                    count=1,
                )
                old_render = """render: function (data, type, full, meta) {
                    return data;
                }"""
                new_render = r'''render: function (data, type, full, meta) {
                    var kwargsText = (full && full.kwargs) ? String(full.kwargs) : '';
                    var startedBy = (full && full.requested_by_username) ? full.requested_by_username : '';
                    var projectDisplay = (full && full.project_display) ? full.project_display : '';
                    var fullTaskName = data ? String(data) : '';
                    var shortTaskName = fullTaskName.replace(/^gaia\.imagery\./, '');
                    var taskUuid = (full && full.uuid) ? String(full.uuid) : '';
                    var taskNameHtml = htmlEscapeEntities(shortTaskName || fullTaskName);

                    if (taskUuid) {
                        taskNameHtml = '<a href="' + url_prefix() + '/task/' + encodeURIComponent(taskUuid) + '" title="' + htmlEscapeEntities(taskUuid) + '">' + taskNameHtml + '</a>';
                    }

                    if (!startedBy && kwargsText) {
                        var startedMatch = kwargsText.match(/[\"']requested_by_username[\"']\s*:\s*[\"']((?:\\.|[^\"'])*)[\"']/);
                        if (startedMatch && startedMatch[1]) {
                            startedBy = startedMatch[1].replace(/\\'/g, "'").replace(/\\"/g, '"');
                        }
                    }

                    if (!projectDisplay && kwargsText) {
                        var projectMatch = kwargsText.match(/[\"']project_display[\"']\s*:\s*[\"']((?:\\.|[^\"'])*)[\"']/);
                        if (projectMatch && projectMatch[1]) {
                            projectDisplay = projectMatch[1].replace(/\\'/g, "'").replace(/\\"/g, '"');
                        }
                    }

                    if (!startedBy) {
                        startedBy = 'unknown';
                    }
                    if (!projectDisplay) {
                        projectDisplay = 'unknown';
                    }

                    return taskNameHtml + '<div class="text-muted small">Started by: ' + htmlEscapeEntities(startedBy) + ' | Project: ' + htmlEscapeEntities(projectDisplay) + '</div>';
                }'''
                js_content = js_content.replace(old_render, new_render, 1)

                # Reduce task table noise: hide args/kwargs/result columns.
                js_content = re.sub(
                    r"visible:\s*isColumnVisible\('args'\)",
                    "visible: false",
                    js_content,
                    count=1,
                )
                js_content = re.sub(
                    r"visible:\s*isColumnVisible\('kwargs'\)",
                    "visible: false",
                    js_content,
                    count=1,
                )
                js_content = re.sub(
                    r"visible:\s*isColumnVisible\('result'\)",
                    "visible: false",
                    js_content,
                    count=1,
                )

                # Hide UUID column; task detail link is now on task name.
                js_content = re.sub(
                    r"visible:\s*isColumnVisible\('uuid'\)",
                    "visible: false",
                    js_content,
                    count=1,
                )

                # Keep task detail link support for fallback UUID rendering.
                js_content = js_content.replace(
                    "return '<a href=\"' + url_prefix() + '/task/' + encodeURIComponent(data) + '\">' + data + '</a>';",
                    "return '<a href=\"' + url_prefix() + '/task/' + encodeURIComponent(data) + '\" title=\"' + htmlEscapeEntities(data) + '\">' + htmlEscapeEntities(data) + '</a>';",
                    1,
                )
            
            # Fix url_prefix() function by adding an override after it's defined
            # Instead of trying to replace the complex function, just override it
            js_content = js_content + '\n// GAIA PROXY: Override url_prefix function\nwindow.url_prefix = function() { return "/monitor"; };\n'
            
            logger.info(f"Modified JS file size: {len(js_content)} characters")
            body = js_content.encode('utf-8')
        except (UnicodeDecodeError, UnicodeEncodeError):
            # If encoding fails, use original body
            logger.error("Failed to decode/encode JavaScript content")
            pass

    # Restrict task visibility for non-admin users.
    elif upstream_path == '/api/tasks' and content_type and 'application/json' in content_type:
        try:
            payload = json.loads(body.decode('utf-8'))
            if not request.user.is_superuser:
                before_count = len(payload) if isinstance(payload, dict) else 0
                payload = _filter_tasks_payload_for_user(payload, request.user.username)
                after_count = len(payload) if isinstance(payload, dict) else 0
                logger.info(
                    "FLOWER PROXY: Filtered /api/tasks for user %s: %s -> %s",
                    request.user.username,
                    before_count,
                    after_count,
                )
            body = json.dumps(payload).encode('utf-8')
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("FLOWER PROXY: Could not filter /api/tasks payload: %s", exc)

    # Normalize DataTables payloads defensively for Flower tasks endpoint.
    elif 'tasks/datatable' in upstream_path and content_type and 'application/json' in content_type:
        try:
            payload = json.loads(body.decode('utf-8'))
            data_key = None
            if isinstance(payload, dict):
                if isinstance(payload.get('data'), list):
                    data_key = 'data'
                elif isinstance(payload.get('aaData'), list):
                    data_key = 'aaData'

            if data_key:
                rows = payload[data_key]
                if rows and all(isinstance(row, list) for row in rows):
                    target_cols = max(6, max(len(row) for row in rows))
                    changed = False
                    for row in rows:
                        while len(row) < target_cols:
                            row.append('')
                            changed = True
                    if changed:
                        payload[data_key] = rows
                        body = json.dumps(payload).encode('utf-8')
                        logger.info(
                            "FLOWER PROXY: Normalized tasks datatable rows to %s columns",
                            target_cols,
                        )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("FLOWER PROXY: Could not normalize tasks datatable payload: %s", exc)
    
    # Create response with proper content length
    out = HttpResponse(body, status=resp.status_code, content_type=content_type)
    
    # Only copy safe response headers, avoiding content-length conflicts
    for k, v in resp.headers.items():
        lk = k.lower()
        if lk in ('content-type', 'cache-control', 'etag'):
            out[k] = v
        elif lk == 'location':
            # Rewrite redirect locations to include /monitor prefix
            if v.startswith('/'):
                out[k] = '/monitor' + v
            else:
                out[k] = v
    
    # Add cache-busting headers for JavaScript files to ensure modifications are loaded
    if upstream_path.endswith('.js'):
        out['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        out['Pragma'] = 'no-cache'
        out['Expires'] = '0'
    
    return out
