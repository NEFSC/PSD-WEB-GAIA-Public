from datetime import date
import re

from django.db import migrations


MONTH_MAP = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

# Expected vendor_id prefix format: YYMMMDD
VENDOR_DATE_PATTERN = re.compile(r"^(\d{2})([A-Za-z]{3})(\d{2})")


def rebuild_date_image_taken_from_vendor_id(apps, schema_editor):
    PointsOfInterest = apps.get_model("animal", "PointsOfInterest")

    queryset = PointsOfInterest.objects.filter(vendor_id__isnull=False).exclude(vendor_id="")

    scanned_count = 0
    updated_count = 0
    unchanged_count = 0
    skipped_invalid_count = 0

    to_update = []
    batch_size = 500

    for poi in queryset.iterator(chunk_size=batch_size):
        scanned_count += 1

        vendor_id = (poi.vendor_id or "").strip()
        match = VENDOR_DATE_PATTERN.match(vendor_id)
        if not match:
            skipped_invalid_count += 1
            continue

        year_2, month_abbrev, day_2 = match.groups()
        month = MONTH_MAP.get(month_abbrev.upper())
        if month is None:
            skipped_invalid_count += 1
            continue

        try:
            parsed_date = date(2000 + int(year_2), month, int(day_2))
        except ValueError:
            skipped_invalid_count += 1
            continue

        if poi.date_image_taken == parsed_date:
            unchanged_count += 1
            continue

        poi.date_image_taken = parsed_date
        to_update.append(poi)

        if len(to_update) >= batch_size:
            PointsOfInterest.objects.bulk_update(to_update, ["date_image_taken"], batch_size=batch_size)
            updated_count += len(to_update)
            to_update = []

    if to_update:
        PointsOfInterest.objects.bulk_update(to_update, ["date_image_taken"], batch_size=batch_size)
        updated_count += len(to_update)

    print(
        "POI date_image_taken rebuild complete: "
        f"scanned={scanned_count}, updated={updated_count}, "
        f"unchanged={unchanged_count}, skipped_invalid={skipped_invalid_count}"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("animal", "0023_alter_pointsofinterest_point"),
    ]

    operations = [
        migrations.RunPython(rebuild_date_image_taken_from_vendor_id, migrations.RunPython.noop),
    ]
