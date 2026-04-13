from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def dedupe_fishnet_reviews(apps, schema_editor):
    from django.db.models import Count, Min

    FishnetReviews = apps.get_model('animal', 'FishnetReviews')
    duplicate_groups = (
        FishnetReviews.objects.exclude(user_id__isnull=True)
        .values('fishnet_id', 'user_id')
        .annotate(min_id=Min('id'), review_count=Count('id'))
        .filter(review_count__gt=1)
    )

    for group in duplicate_groups:
        FishnetReviews.objects.filter(
            fishnet_id=group['fishnet_id'],
            user_id=group['user_id'],
        ).exclude(id=group['min_id']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0036_pointsofinterest_duplicate_review_fields'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='pointsofinterest',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_pois', to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(dedupe_fishnet_reviews, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='fishnetreviews',
            constraint=models.UniqueConstraint(fields=('fishnet', 'user'), name='uniq_fishnet_review_user'),
        ),
    ]
