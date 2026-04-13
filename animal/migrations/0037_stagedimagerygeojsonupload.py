from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0036_pointsofinterest_duplicate_review_fields'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='StagedImageryGeoJSONUpload',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('source_filename', models.CharField(max_length=255)),
                ('parsed_vendor_id', models.CharField(max_length=64)),
                ('geojson_payload', models.TextField()),
                ('consumed', models.BooleanField(default=False)),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('project', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='staged_imagery_geojson_uploads', to='animal.project')),
                ('uploaded_by_user', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='stagedimagerygeojsonupload',
            index=models.Index(fields=['project', '-created_at'], name='stageimg_proj_created_idx'),
        ),
        migrations.AddIndex(
            model_name='stagedimagerygeojsonupload',
            index=models.Index(fields=['project', 'consumed'], name='stageimg_proj_consumed_idx'),
        ),
    ]
