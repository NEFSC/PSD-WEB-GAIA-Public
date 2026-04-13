from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0025_pointsofinterest_final_comments'),
    ]

    operations = [
        migrations.CreateModel(
            name='ZoomLevel',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('label', models.CharField(max_length=25, unique=True)),
                ('description', models.CharField(blank=True, max_length=255)),
                ('value', models.PositiveIntegerField(help_text='Scale denominator (for example: 1250 for 1:1250)', unique=True)),
            ],
            options={
                'ordering': ['value'],
            },
        ),
        migrations.AddField(
            model_name='project',
            name='zoom_level',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='projects',
                to='animal.zoomlevel',
            ),
        ),
    ]
