from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0031_add_age_fields_and_backfill_poi_age'),
    ]

    operations = [
        migrations.CreateModel(
            name='ImageryLoadRequest',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('requested_by_username', models.CharField(blank=True, max_length=150)),
                ('chain_id', models.CharField(max_length=64, unique=True)),
                ('status', models.CharField(choices=[('PROCESSING', 'Processing'), ('LOADED', 'Loaded'), ('FAILED', 'Failed')], default='PROCESSING', max_length=16)),
                ('error_summary', models.CharField(blank=True, max_length=500)),
                ('requested_at', models.DateTimeField(auto_now_add=True)),
                ('last_status_update_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='imagery_load_requests', to='animal.project')),
                ('requested_by_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='auth.user')),
            ],
            options={
                'ordering': ['-requested_at'],
            },
        ),
        migrations.AddIndex(
            model_name='imageryloadrequest',
            index=models.Index(fields=['project', '-requested_at'], name='animal_image_project_1f2f42_idx'),
        ),
        migrations.AddIndex(
            model_name='imageryloadrequest',
            index=models.Index(fields=['status'], name='animal_image_status_a91e37_idx'),
        ),
    ]
