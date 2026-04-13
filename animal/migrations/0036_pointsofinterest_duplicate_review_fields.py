from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0035_imageryloadrequest_catalog_ids'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='pointsofinterest',
            name='duplicate_reviewed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='pointsofinterest',
            name='duplicate_reviewed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='duplicate_reviews', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='pointsofinterest',
            name='duplicate_reviewed_valid',
            field=models.BooleanField(default=False),
        ),
    ]
