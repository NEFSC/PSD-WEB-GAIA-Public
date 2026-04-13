from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0027_seed_zoomlevel_and_backfill_projects'),
    ]

    operations = [
        migrations.AlterField(
            model_name='project',
            name='zoom_level',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='projects',
                to='animal.zoomlevel',
            ),
        ),
    ]
