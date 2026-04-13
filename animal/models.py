# ------------------------------------------------------------------------------
# ----- models.py --------------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Django model definitions for GAIA's animal app. 11 models
#              covering AOIs, imagery catalogs (EE, GEGD, MGP), ETL,
#              POIs, annotations, fishnets, and project access.
#
#    tickets:  GAIFAGP-514 (FK deletion policy fix)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - ETL table is unmanaged (managed = False) — populated by triggers
#      - POI.catalog_id is a soft link to ETL.id (see DL-019)
#      - All geometry stored in WGS84 (EPSG:4326)
#
# ------------------------------------------------------------------------------

from django.db import models
from django.contrib.auth.models import User
from django.contrib.gis.db import models as gis_models
from django.core.exceptions import ValidationError

AGE_CHOICES = [
    ('adult', 'Adult'),
    ('juvenile', 'Juvenile'),
    ('calf', 'Calf'),
    ('unknown', 'Unknown'),
]

class AreaOfInterest(gis_models.Model):
    id = gis_models.AutoField(primary_key=True)
    name = gis_models.CharField(max_length=50)
    geometry = gis_models.GeometryField(srid=4326)
    sqkm = gis_models.FloatField()

    def __str__(self):
        return self.name

class Target(gis_models.Model):
    id = gis_models.AutoField(primary_key=True)
    value = gis_models.CharField(max_length=30, unique=True)
    label = gis_models.CharField(max_length=25)

    class Meta:
        verbose_name = "Species"
        verbose_name_plural = "Species"

    def __str__(self):
        return self.label

class Species_Locations(models.Model):
    id = models.AutoField(primary_key=True)
    aoi = models.ForeignKey(AreaOfInterest, on_delete=models.CASCADE, related_name='aoi', help_text="Area of Interest")
    species = models.ForeignKey(Target, on_delete=models.CASCADE, related_name='target', help_text="Species")

    class Meta:
        verbose_name = "Species Location"
        verbose_name_plural = "Species Locations"
        unique_together = [('aoi', 'species')]

    def __str__(self):
        return f"{self.species.label} in {self.aoi.name}"

class Confidence(models.Model):
    id = models.AutoField(primary_key=True)
    value = models.CharField(max_length=10, unique=True)
    label = models.CharField(max_length=25)

    def __str__(self):
        return self.label

class Category(models.Model):
    name = models.CharField(max_length=75)
    order = models.PositiveIntegerField(default=0, help_text="Sorting order of categories")

    class Meta:
        verbose_name = "Category"
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name

class Classification(models.Model):
    id = models.AutoField(primary_key=True)
    label = models.CharField(max_length=25)
    value = models.CharField(max_length=25, unique=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='cat_order', help_text="Group name for categorizing classifications", null=True, blank=True)
    order = models.PositiveIntegerField(default=0, help_text="Sorting order within Classification category")

    def __str__(self):
        return self.label

    class Meta:
        ordering = ['category', 'order', 'label']

class ZoomLevel(models.Model):
    id = models.AutoField(primary_key=True)
    label = models.CharField(max_length=25, unique=True)
    description = models.CharField(max_length=255, blank=True)
    value = models.PositiveIntegerField(unique=True, help_text="Scale denominator (for example: 1250 for 1:1250)")

    class Meta:
        ordering = ['value']

    def __str__(self):
        return self.label

class Project(models.Model):
    id = models.AutoField(primary_key=True)
    label = models.CharField(max_length=75)
    value = models.CharField(max_length=75)
    description = models.CharField(max_length=500, null=True, blank=True)
    owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='project_owner', help_text="Project Owner")
    zoom_level = models.ForeignKey(ZoomLevel, on_delete=models.PROTECT, related_name='projects')

    def __str__(self):
        return self.label

# Needs to be revisited with cleaned data from Lauren
class Tasking(models.Model):
    MONO_STERO_CHOICES = [
        ('mono', 'Mono'),
        ('stereo', 'Stereo'),
    ]

    id = models.AutoField(primary_key=True)
    dar = models.IntegerField()
    aoi = models.ForeignKey(AreaOfInterest, on_delete=models.DO_NOTHING)
    location = models.CharField(max_length=12)
    target = models.CharField(max_length=30)
    requestor = models.ForeignKey(User, on_delete=models.DO_NOTHING)
    vendor = models.CharField(max_length=10)
    mono_stereo = models.CharField(max_length=6, choices=MONO_STERO_CHOICES)
    date_entered = models.DateField()
    acquisition_start = models.DateField()
    acquisition_end = models.DateField()
    ona_wv2 = models.CharField(max_length=10)
    ona_wv3 = models.CharField(max_length=10)
    tasking_description = models.CharField(max_length=10)
    comments = models.CharField(max_length=250)
    status = models.CharField(max_length=8)
    output_format = models.CharField(max_length=7)
    processing_level = models.CharField(max_length=41)
    website_link = models.CharField(max_length=50)
    permission_to_share = models.CharField(max_length=500)

    def __str__(self):
        return self.id

class EarthExplorer(gis_models.Model):
    """Satellite imagery metadata from USGS EarthExplorer (WorldView-2/3, GeoEye-1).

    One record per entity_id. Populated by USGS API search results.
    Unmanaged provider table — ETL triggers derive records into the ETL table.
    """
    entity_id = gis_models.CharField(max_length=20, primary_key=True)
    aoi_id = gis_models.ForeignKey(AreaOfInterest, on_delete=models.DO_NOTHING)
    catalog_id = gis_models.CharField(max_length=16)
    acquisition_date = gis_models.DateField(null=True, blank=True)
    vendor = gis_models.CharField(max_length=18)
    vendor_id = gis_models.CharField(max_length=39)
    cloud_cover = gis_models.IntegerField()
    satellite = gis_models.CharField(max_length=11)
    sensor = gis_models.CharField(max_length=3)
    number_of_bands = gis_models.IntegerField()
    map_projection = gis_models.CharField(max_length=3)
    datum = gis_models.CharField(max_length=5)
    processing_level = gis_models.CharField(max_length=3)
    file_format = gis_models.CharField(max_length=7)
    license_id = gis_models.IntegerField()
    sun_azimuth = gis_models.FloatField()
    sun_elevation = gis_models.FloatField()
    pixel_size_x = gis_models.FloatField()
    pixel_size_y = gis_models.FloatField()
    license_uplift_update = gis_models.DateField(null=True, blank=True)
    event = gis_models.CharField(max_length=5)
    event_date = gis_models.DateField(null=True, blank=True)
    date_entered = gis_models.DateField(null=True, blank=True)
    center_latitude_dec = gis_models.FloatField()
    center_longitude_dec = gis_models.FloatField()
    thumbnail = gis_models.CharField(max_length=76)
    publish_date = gis_models.DateTimeField(null=True, blank=True)
    bounds = gis_models.GeometryField(srid=4326)

    def __str__(self):
        return f"{self.entity_id} in Category ID: {self.aoi_id.id}"

class GEOINTDiscovery(gis_models.Model):
    """Satellite imagery metadata from G-EGD (Global Enhanced GEOINT Delivery).

    One record per imagery footprint. Populated by GEOINT discovery API results.
    Unmanaged provider table — ETL triggers derive records into the ETL table.
    """
    id = gis_models.CharField(max_length=32, primary_key=True)
    aoi_id = gis_models.ForeignKey(AreaOfInterest, on_delete=models.DO_NOTHING)
    legacy_id = gis_models.CharField(max_length=16)
    factory_order_number = gis_models.CharField(max_length=12)
    acquisition_date = gis_models.DateField()
    source = gis_models.CharField(max_length=9)
    source_unit = gis_models.CharField(max_length=5)
    product_type = gis_models.CharField(max_length=36)
    cloud_cover = gis_models.FloatField()
    off_nadir_angle = gis_models.FloatField()
    sun_elevation = gis_models.FloatField()
    sun_azimuth = gis_models.FloatField()
    ground_sample_distance = gis_models.FloatField()
    data_layer = gis_models.CharField(max_length=10)
    legacy_description = gis_models.CharField(max_length=12)
    color_band_order = gis_models.CharField(max_length=3)
    asset_name = gis_models.CharField(max_length=8)
    per_pixel_x = gis_models.FloatField()
    per_pixel_y = gis_models.FloatField()
    crs_from_pixels = gis_models.CharField(max_length=10)
    age_days = gis_models.FloatField()
    ingest_date = gis_models.DateField()
    company_name = gis_models.CharField(max_length=12)
    copyright = gis_models.CharField(max_length=37)
    niirs = gis_models.FloatField()
    geometry = gis_models.GeometryField(srid=4326)

    def __str__(self):
        return f"{self.id} in AOI: {self.aoi_id.id}"

class MaxarGeospatialPlatform(gis_models.Model):
    """Satellite imagery metadata from Maxar Geospatial Platform (MGP).

    One record per catalog_id. Populated by MGP STAC API search results.
    Unmanaged provider table — ETL triggers derive records into the ETL table.
    """
    id = gis_models.CharField(max_length=16, primary_key=True)
    aoi_id = gis_models.ForeignKey(AreaOfInterest, on_delete=models.DO_NOTHING)
    platform = gis_models.CharField(max_length=11)
    instruments = gis_models.CharField(max_length=4)
    gsd = gis_models.FloatField()
    pan_resolution_avg = gis_models.FloatField()
    multi_resolution_avg = gis_models.FloatField()
    datetime = gis_models.DateTimeField()
    off_nadir = gis_models.FloatField()
    azimuth = gis_models.FloatField()
    sun_azimuth = gis_models.FloatField()
    sun_elevation = gis_models.FloatField()
    bbox = gis_models.GeometryField(srid=4326)

    def __str__(self):
        return f"{self.id} in AOI: {self.aoi_id.id}"

# This table was built from other tables
#      Some places this is called a denormalized table, materalized view,
#      aggregate table, staging table, etc. It is effectively a starting
#      point for other work and should not be expected to matriculate
#      changes to earlier tables.

class ExtractTransformLoad(gis_models.Model):
    """Denormalized imagery catalog derived from provider tables by database triggers.

    Unmanaged (managed=False). Populated by insert triggers on EarthExplorer,
    GEOINTDiscovery, and MaxarGeospatialPlatform. Canonical source for POI linkage
    via catalog_id (DL-019). Do not write to this table from application code.
    """
    table_name = gis_models.CharField(max_length=4)
    aoi_id = gis_models.IntegerField()
    id = gis_models.CharField(max_length=16, primary_key=True)
    vendor_id = gis_models.CharField(max_length=39)
    entity_id = gis_models.CharField(max_length=20)
    vendor = gis_models.CharField(max_length=18)
    platform = gis_models.CharField(max_length=11)
    pixel_size_x = gis_models.FloatField()
    pixel_size_y = gis_models.FloatField()
    date = gis_models.DateField()
    publish_date = gis_models.DateField()
    geometry = gis_models.TextField()  # Might need to *rebuild* as Geometry type
    sea_state_qual = gis_models.CharField(max_length=15, null=True, blank=True)
    sea_state_quant = gis_models.IntegerField(null=True, blank=True)
    shareable = gis_models.CharField(max_length=3, null=True, blank=True)

    class Meta:
        db_table = 'etl'
        managed = False

        constraints = [
            gis_models.UniqueConstraint(fields=['id', 'vendor_id', 'entity_id'], name='img_id')
        ]

    def __str__(self):
        return f"{self.id} {self.vendor_id} {self.entity_id}"

class PointsOfInterest(gis_models.Model):
    GENERATION_METHOD_CHOICES = [
        ('automated', 'automated'),
        ('fishnet', 'fishnet'),
        ('manual', 'manual'),
    ]

    # Mandatory and from ETL
    id = gis_models.AutoField(primary_key=True)
    catalog_id = gis_models.CharField(max_length=16, null=True, blank=True)
    vendor_id = gis_models.CharField(max_length=39, null=True, blank=True)
    entity_id = gis_models.CharField(max_length=20, null=True, blank=True)
    date_image_taken = models.DateField(null=True, blank=True)
    sensor = models.CharField(max_length=40, null=True, blank=True)

    # From Generate Interesting Points
    sample_idx = gis_models.CharField(max_length=40, null=True, blank=True)
    area = gis_models.FloatField(null=True, blank=True)
    deviation = gis_models.FloatField(null=True, blank=True)
    epsg_code = gis_models.CharField(max_length=6, null=True, blank=True)

    # For review process
    final_species = models.ForeignKey(Target, on_delete=models.PROTECT, null=True, blank=True)
    final_classification = models.ForeignKey(Classification, on_delete=models.PROTECT, null=True, blank=True)
    final_confidence = models.ForeignKey(Confidence, on_delete=models.PROTECT, null=True, blank=True)
    final_age = models.CharField(max_length=20, choices=AGE_CHOICES, null=True, blank=True)
    final_comments = models.TextField(null=True, blank=True)
    final_review_date = models.DateField(null=True, blank=True)
    duplicate_reviewed_valid = models.BooleanField(default=False)
    duplicate_reviewed_at = models.DateTimeField(null=True, blank=True)
    duplicate_reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='duplicate_reviews',
    )

    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_pois',
    )
    generation_method = models.CharField(
        max_length=20,
        choices=GENERATION_METHOD_CHOICES,
        null=True,
        blank=True,
        help_text="How this POI was created: automated (external tool), fishnet (grid scan), or manual (annotator Add Point).",
    )

    # Mandatory
    point = gis_models.GeometryField(srid=4326)

    def __str__(self):
        return str(self.id)

class Annotations(models.Model):
    id = models.AutoField(primary_key=True)
    poi = models.ForeignKey(PointsOfInterest, related_name='annotations', on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    classification = models.ForeignKey(Classification, on_delete=models.PROTECT)
    comments = models.CharField(max_length=500, null=True, blank=True)
    confidence = models.ForeignKey(Confidence, on_delete=models.PROTECT, null=True, blank=True)
    target = models.ForeignKey(Target, on_delete=models.PROTECT, null=True, blank=True)
    age = models.CharField(max_length=20, choices=AGE_CHOICES, null=True, blank=True)
    date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"Annotation {self.id} by {self.user}"

    def clean(self):
        super().clean()
        # classification_id 14 = "Animal" — requires target/confidence always, age on new rows
        if self.classification_id == 14:
            if not self.target:
                raise ValidationError({'Target': 'This field cannot be null when classification is Animal.'})
            if not self.confidence:
                raise ValidationError({'Confidence': 'This field cannot be null when classification is Animal.'})
            if self._state.adding and not self.age:
                raise ValidationError({'Age': 'This field cannot be null when classification is Animal.'})

class Fishnet(gis_models.Model):
    id = gis_models.AutoField(primary_key=True)
    vendor_id = gis_models.CharField(max_length=39, null=True, blank=True)
    cell = gis_models.GeometryField(srid=4326, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, null=True, blank=True)

    def __str__(self):
        return str(self.id)

class FishnetReviews(models.Model):
    id = models.AutoField(primary_key=True)
    fishnet = models.ForeignKey(Fishnet, related_name='fishnetreviews', on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    date = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['fishnet', 'user'], name='uniq_fishnet_review_user'),
        ]

    def __str__(self):
        return str(self.id)

class ProjectAccess(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'project')
        verbose_name = 'Project Access'
        verbose_name_plural = 'Project Access'

    def __str__(self):
        return f"{self.user.username} - {self.project.label}"


class ImageryLoadRequest(models.Model):
    STATUS_PROCESSING = 'PROCESSING'
    STATUS_LOADED = 'LOADED'
    STATUS_FAILED = 'FAILED'
    STATUS_CHOICES = [
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_LOADED, 'Loaded'),
        (STATUS_FAILED, 'Failed'),
    ]

    id = models.AutoField(primary_key=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='imagery_load_requests')
    requested_by_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    requested_by_username = models.CharField(max_length=150, blank=True)
    request_group_id = models.CharField(max_length=64, blank=True)
    aoi_name = models.CharField(max_length=100, blank=True)
    catalog_ids = models.CharField(max_length=500, blank=True)
    chain_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PROCESSING)
    error_summary = models.CharField(max_length=500, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    last_status_update_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-requested_at']
        indexes = [
            models.Index(fields=['project', '-requested_at']),
            models.Index(fields=['status']),
            models.Index(fields=['project', 'request_group_id'], name='imgreq_proj_grp_idx'),
        ]

    def __str__(self):
        return f"{self.project.label} [{self.chain_id}] - {self.status}"


class StagedImageryGeoJSONUpload(models.Model):
    """Temporary GeoJSON payload staged by Load Imagery ID-mode searches."""

    id = models.AutoField(primary_key=True)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='staged_imagery_geojson_uploads',
    )
    uploaded_by_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    source_filename = models.CharField(max_length=255)
    parsed_vendor_id = models.CharField(max_length=64)
    geojson_payload = models.TextField()
    consumed = models.BooleanField(default=False)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['project', '-created_at']),
            models.Index(fields=['project', 'consumed']),
        ]

    def __str__(self):
        return f"{self.project.label} [{self.id}] {self.parsed_vendor_id}"


# WebAuthn/FIDO2 Models (import from separate file to keep organized)
try:
    from .webauthn_models import WebAuthnCredential, WebAuthnChallenge
except ImportError:
    # WebAuthn models are optional
    pass