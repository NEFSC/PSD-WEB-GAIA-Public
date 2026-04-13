"""
URL configuration for gaia project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.views.generic import RedirectView
from two_factor.urls import urlpatterns as tf_urls

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include(tf_urls)),  # Two-factor URLs already include 'account/' prefix
    path('accounts/', include('django.contrib.auth.urls')),
    path('', include('animal.urls')),
    # Legacy login redirect for backwards compatibility
    path('login/', RedirectView.as_view(url='/account/login/', permanent=True)),
    path('logout/', RedirectView.as_view(url='/account/logout/', permanent=True)),
    # Two-factor onboarding
    # path('two-factor-onboarding/', include('animal.urls')),
]

# Add debug toolbar URLs only if debug mode is enabled and debug_toolbar is available
if settings.DEBUG:
    try:
        import debug_toolbar
        urlpatterns.append(path('__debug__/', include(debug_toolbar.urls)))
    except ImportError:
        pass

urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)