from django.db import migrations, models


ADULT_AGE_VALUE = 'adult'


def backfill_poi_final_age(apps, schema_editor):
    PointsOfInterest = apps.get_model('animal', 'PointsOfInterest')
    PointsOfInterest.objects.all().update(final_age=ADULT_AGE_VALUE)


def reverse_backfill_poi_final_age(apps, schema_editor):
    PointsOfInterest = apps.get_model('animal', 'PointsOfInterest')
    PointsOfInterest.objects.all().update(final_age=None)


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0030_backfill_generation_method'),
    ]

    operations = [
        migrations.AddField(
            model_name='annotations',
            name='age',
            field=models.CharField(
                blank=True,
                choices=[
                    ('adult', 'Adult'),
                    ('juvenile', 'Juvenile'),
                    ('calf', 'Calf'),
                    ('unknown', 'Unknown'),
                ],
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='pointsofinterest',
            name='final_age',
            field=models.CharField(
                blank=True,
                choices=[
                    ('adult', 'Adult'),
                    ('juvenile', 'Juvenile'),
                    ('calf', 'Calf'),
                    ('unknown', 'Unknown'),
                ],
                max_length=20,
                null=True,
            ),
        ),
        migrations.RunPython(
            code=backfill_poi_final_age,
            reverse_code=reverse_backfill_poi_final_age,
        ),
    ]
