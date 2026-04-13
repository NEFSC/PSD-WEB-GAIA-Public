"""
Django admin configuration module

Includes admin interfaces for managing species locations and relationships between species and areas of interest.
"""

from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin
from .models import AreaOfInterest, Target, Classification, Confidence, Project, PointsOfInterest, Category, ProjectAccess, Species_Locations, ZoomLevel
from adminsortable2.admin import SortableInlineAdminMixin, SortableAdminBase

# Inline admin for Species_Locations to show species in an area of interest
class SpeciesLocationsInline(admin.TabularInline):
    model = Species_Locations
    extra = 1
    fk_name = 'aoi'
    autocomplete_fields = ['species']
    verbose_name = 'Species in this Area'
    verbose_name_plural = 'Species in this Area'

# Inline admin for Species_Locations to show areas for a species
class AreaLocationsInline(admin.TabularInline):
    model = Species_Locations
    extra = 1
    fk_name = 'species'
    autocomplete_fields = ['aoi']
    verbose_name = 'Area of Interest'
    verbose_name_plural = 'Areas of Interest'

@admin.register(AreaOfInterest)
class AreaOfInterestAdmin(GISModelAdmin):
    list_display = ('name', 'sqkm')
    search_fields = ['name']
    inlines = [SpeciesLocationsInline]
    verbose_name = 'Area of Interest'
    verbose_name_plural = 'Areas of Interest'

@admin.register(Target)
class TargetAdmin(admin.ModelAdmin):
    list_display = ('value', 'label')
    search_fields = ['value', 'label']
    inlines = [AreaLocationsInline]

@admin.register(Species_Locations)
class SpeciesLocationsAdmin(admin.ModelAdmin):
    list_display = ('id', 'species', 'aoi')
    list_filter = ['species', 'aoi']
    search_fields = ['species__label', 'species__value', 'aoi__name']
    autocomplete_fields = ['species', 'aoi']

@admin.register(Classification)
class ClassificationAdmin(admin.ModelAdmin):
    list_display = ('value', 'category')
    search_fields = ['value', 'label']

class ClassificationInline(SortableInlineAdminMixin, admin.TabularInline):
    model = Classification
    extra = 1
    ordering = ('order',)

@admin.register(Category)
class CategoryAdmin(SortableAdminBase, admin.ModelAdmin):
    inlines = [ClassificationInline]

@admin.register(Confidence)
class ConfidenceAdmin(admin.ModelAdmin):
    list_display = ('value',)
    search_fields = ['value', 'label']
    list_filter = ['value', 'label']

@admin.register(ZoomLevel)
class ZoomLevelAdmin(admin.ModelAdmin):
    list_display = ('label', 'value', 'description')
    search_fields = ['label', 'description']
    list_filter = ['value']

# Inline admin for ProjectAccess to show users in a project
class ProjectAccessInline(admin.TabularInline):
    model = ProjectAccess
    extra = 1
    autocomplete_fields = ['user']
    verbose_name = 'Project Member'
    verbose_name_plural = 'Project Members'

# Inline admin for ProjectAccess to show projects for a user
class UserProjectAccessInline(admin.TabularInline):
    model = ProjectAccess
    extra = 1
    autocomplete_fields = ['project']
    verbose_name = 'Project Access'
    verbose_name_plural = 'Project Access'

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('value', 'zoom_level')
    search_fields = ['value', 'label']
    list_filter = ['value', 'label', 'zoom_level']
    inlines = [ProjectAccessInline]

    def get_readonly_fields(self, request, obj=None):
        # Project zoom level is immutable after creation.
        if obj:
            return ('zoom_level',)
        return ()

@admin.register(PointsOfInterest)
class PointsOfInterestAdmin(GISModelAdmin):
    list_display = ('id', 'vendor_id')

# Unregister the default User admin and register our custom one
admin.site.unregister(User)

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    inlines = [UserProjectAccessInline]
    
    # Add project access to the user detail view
    def get_inline_instances(self, request, obj=None):
        if obj:  # Only show inlines when editing an existing user
            return super().get_inline_instances(request, obj)
        return []