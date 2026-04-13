from django.urls import path
from django.contrib.auth.decorators import login_required, user_passes_test
from . import views

# Define is_superuser helper function
def is_superuser(user):
    return user.is_superuser
from .views.annotation_views import proxy_openlayers_js, proxy_webgls_js, clear_multiview_cache
from .views.dissemination_views import export_whale_annotations_bas, export_whale_annotations_whalemap
from .views.account_views import account_page, disable_all_2fa
from .views.monitoring_views import (
    monitoring_dashboard, api_system_status, api_pipeline_tasks, 
    api_worker_health, api_task_history, api_kill_task, api_clear_queue
)
from .decorators import require_project_access_or_redirect
from .two_factor_views import TwoFactorOnboardingView, two_factor_status_check

# Import unified 2FA views
from .two_factor_settings_views import (
    TwoFactorSettingsView, 
    remove_totp_device, 
    disable_all_2fa_unified
)
from .views.flower_proxy_views import flower_proxy

# Import WebAuthn views with fallback
try:
    from .webauthn_views import (
        WebAuthnSetupView, 
        WebAuthnSetupViewLegacy,
        webauthn_registration_begin, 
        webauthn_registration_complete,
        webauthn_authentication_begin, 
        webauthn_authentication_complete,
        remove_webauthn_credential
    )
    from .webauthn_verification_views import (
        WebAuthnVerificationRequiredView,
        webauthn_verification_complete,
        skip_webauthn_verification
    )
    WEBAUTHN_AVAILABLE = True
except ImportError:
    WEBAUTHN_AVAILABLE = False

def is_superuser(user):
    return user.is_superuser

urlpatterns = [
    # Unified Two-Factor Authentication Settings
    path('account/2fa/', TwoFactorSettingsView.as_view(), name='two_factor_settings'),
    
    # TOTP Management
    path('account/2fa/totp/remove/<int:device_id>/', remove_totp_device, name='remove_totp_device'),
    
    # Legacy two-factor routes (for backwards compatibility)
    path('2fa/onboarding/', TwoFactorOnboardingView.as_view(), name='two_factor_onboarding'),
    path('2fa/check/', two_factor_status_check, name='two_factor_status_check'),
]

# Add WebAuthn routes if available
if WEBAUTHN_AVAILABLE:
    urlpatterns += [
        # WebAuthn verification enforcement
        path('account/webauthn-verify/', WebAuthnVerificationRequiredView.as_view(), name='webauthn_verify_required'),
        path('account/webauthn-verify/complete/', webauthn_verification_complete, name='webauthn_verification_complete'),
        
        # New unified WebAuthn routes
        path('account/2fa/webauthn/', WebAuthnSetupView.as_view(), name='webauthn_setup_unified'),
        path('account/2fa/webauthn/register/begin/', webauthn_registration_begin, name='webauthn_registration_begin_unified'),
        path('account/2fa/webauthn/register/complete/', webauthn_registration_complete, name='webauthn_registration_complete_unified'),
        path('account/2fa/webauthn/authenticate/begin/', webauthn_authentication_begin, name='webauthn_authentication_begin_unified'),
        path('account/2fa/webauthn/authenticate/complete/', webauthn_authentication_complete, name='webauthn_authentication_complete_unified'),
        path('account/2fa/webauthn/remove/<int:credential_id>/', remove_webauthn_credential, name='remove_webauthn_unified'),
        
        # Legacy WebAuthn routes (for backwards compatibility)
        path('2fa/passkey-setup/', WebAuthnSetupViewLegacy.as_view(), name='webauthn_setup'),
        path('2fa/webauthn/register/begin/', webauthn_registration_begin, name='webauthn_registration_begin'),
        path('2fa/webauthn/register/complete/', webauthn_registration_complete, name='webauthn_registration_complete'),
        path('2fa/webauthn/authenticate/begin/', webauthn_authentication_begin, name='webauthn_authentication_begin'),
        path('2fa/webauthn/authenticate/complete/', webauthn_authentication_complete, name='webauthn_authentication_complete'),
        path('2fa/remove-passkey/<int:credential_id>/', remove_webauthn_credential, name='remove_webauthn'),
    ]

urlpatterns += [
    # Account management
    path('account/', login_required(account_page), name='account_page'),
    path('account/2fa/disable-all/', disable_all_2fa_unified, name='disable_all_2fa_unified'),
    
    # Main application routes
    path('', login_required(views.landing_page), name='landing_page'),
    path('project', login_required(views.project_page), name='project_list'),
    path('project/<int:project_id>/tasking/', login_required(require_project_access_or_redirect()(views.tasking_page)), name='tasking_page'),
    path('project/<int:project_id>/load-imagery/', login_required(require_project_access_or_redirect()(views.collection_page)), name='collection_page'),
    path('project/<int:project_id>/load-points/', login_required(require_project_access_or_redirect()(views.load_points_page)), name='load_points_page'),
#     #path('project/<int:project_id>/processing/', login_required(require_project_access_or_redirect()(views.processing_page)), name='processing_page'),
    path('project/<int:project_id>/', login_required(require_project_access_or_redirect()(views.project_page)), name='project_detail'),
    path('project/<int:project_id>/annotation/', login_required(require_project_access_or_redirect()(views.annotation_page)), name='annotation_page'),
    path('project/<int:project_id>/annotation/<int:item_id>/', login_required(require_project_access_or_redirect()(views.annotation_page)), 
         name='annotation_item_page'),
    path('project/<int:project_id>/poi/create/', login_required(require_project_access_or_redirect()(views.create_point)), 
         name='create_point'),
    path('project/<int:project_id>/poi/<int:poi_id>/move/', login_required(require_project_access_or_redirect()(views.move_point)),
         name='move_point'),
    path('project/<int:project_id>/poi/<int:poi_id>/delete/', login_required(require_project_access_or_redirect()(views.delete_point)), 
         name='delete_point'),
    path('project/<int:project_id>/detect/', login_required(require_project_access_or_redirect()(views.detect_page)), name='detect_page'),
    path('project/<int:project_id>/detect/<int:id>/', login_required(require_project_access_or_redirect()(views.detect_page)), name='detect_item_page'),
    path('project/<int:project_id>/multiview/', login_required(require_project_access_or_redirect()(views.multiview_list)), name='multiview_list_page'),
    path('project/<int:project_id>/multiview/clear-cache/', user_passes_test(is_superuser, login_url='/access-denied/')(views.clear_multiview_cache), name='clear_multiview_cache'),
    path('project/<int:project_id>/multiview/<str:vendor_id>/', login_required(require_project_access_or_redirect()(views.multiview)), name='multiview_page'),
    path('project/<int:project_id>/multiview/<str:vendor_id>/annotated/', login_required(require_project_access_or_redirect()(views.multiview_annotated)), name='multiview_annotated'),
    path('project/<int:project_id>/multiview/<str:vendor_id>/poi/<int:poi_id>/', login_required(require_project_access_or_redirect()(views.multiview)), name='multiview_poi_page'),
    path('project/<int:project_id>/details/', login_required(require_project_access_or_redirect()(views.project_details_page)), name='project_details_page'),
    path('cogs/<path:vendor_id>/', views.cog_view, name='cog_view'),
    path('cogs-preview/', login_required(views.cog_preview_list), name='cog_preview_list'),
    path('project/<int:project_id>/dissemination/', login_required(require_project_access_or_redirect()(views.dissemination_page)), name='dissemination_page'),
    path('project/<int:project_id>/export/whale-annotations/bas/', login_required(require_project_access_or_redirect()(export_whale_annotations_bas)), name='export_whale_annotations_bas'),
    path('project/<int:project_id>/export/whale-annotations-whalemap/', login_required(require_project_access_or_redirect()(export_whale_annotations_whalemap)), name='export_whale_annotations_whalemap'),
    path('project/<int:project_id>/validation/', login_required(require_project_access_or_redirect()(views.validation)), name='validation'),
    path('project/<int:project_id>/deduplication/', user_passes_test(is_superuser, login_url='/access-denied/')(views.deduplication_list), name='deduplication_list_page'),
    path('project/<int:project_id>/deduplication/<int:poi_id>/', user_passes_test(is_superuser, login_url='/access-denied/')(views.deduplication), name='deduplication_page'),
    path('project/<int:project_id>/cache/clear/', user_passes_test(is_superuser, login_url='/access-denied/')(views.clear_project_page_cache), name='clear_project_page_cache'),
    path('project/<int:project_id>/cache/clear/user/<int:user_id>/', user_passes_test(is_superuser, login_url='/access-denied/')(views.clear_user_project_cache), name='clear_user_project_cache'),
    path('proxy/openlayers.js', proxy_openlayers_js, name='proxy_openlayers_js'),
    path('proxy/ol-webgl.js', proxy_webgls_js, name='proxy_webgls_js'),
    # Flower monitoring (Django-auth protected)
    path('monitor', flower_proxy, name='flower_monitor_root_noslash'),
    path('monitor/', flower_proxy, name='flower_monitor_root'),
    path('monitor/<path:subpath>', flower_proxy, name='flower_monitor_noslash'),
    path('monitor/<path:subpath>/', flower_proxy, name='flower_monitor'),
    
    path('access-denied/', views.access_denied, name='access_denied'),
]