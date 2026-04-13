from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0034_imagery_request_group_and_aoi'),
    ]

    operations = [
        migrations.AddField(
            model_name='imageryloadrequest',
            name='catalog_ids',
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
