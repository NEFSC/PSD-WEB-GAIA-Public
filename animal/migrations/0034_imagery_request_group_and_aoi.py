from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0033_rename_animal_image_project_1f2f42_idx_animal_imag_project_33c492_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='imageryloadrequest',
            name='aoi_name',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='imageryloadrequest',
            name='request_group_id',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddIndex(
            model_name='imageryloadrequest',
            index=models.Index(fields=['project', 'request_group_id'], name='imgreq_proj_grp_idx'),
        ),
    ]
