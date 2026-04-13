from django.db import migrations


def seed_zoom_levels_and_backfill_projects(apps, schema_editor):
    ZoomLevel = apps.get_model('animal', 'ZoomLevel')
    Project = apps.get_model('animal', 'Project')

    zoom_levels = [
        {
            'label': '1:1250',
            'description': 'Approximate grid size 125 x 100 m at 96 DPI and ~1200x800 viewport.',
            'value': 1250,
        },
        {
            'label': '1:1500',
            'description': 'Approximate grid size 150 x 125 m at 96 DPI and ~1200x800 viewport.',
            'value': 1500,
        },
        {
            'label': '1:2000',
            'description': 'Approximate grid size 175 x 150 m at 96 DPI and ~1200x800 viewport.',
            'value': 2000,
        },
    ]

    for zoom_level in zoom_levels:
        ZoomLevel.objects.update_or_create(
            value=zoom_level['value'],
            defaults={
                'label': zoom_level['label'],
                'description': zoom_level['description'],
            },
        )

    default_zoom_level = ZoomLevel.objects.get(value=1250)
    Project.objects.filter(zoom_level__isnull=True).update(zoom_level=default_zoom_level)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0026_zoomlevel_project_zoom_level'),
    ]

    operations = [
        migrations.RunPython(seed_zoom_levels_and_backfill_projects, noop_reverse),
    ]
