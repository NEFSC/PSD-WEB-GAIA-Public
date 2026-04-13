# Generated manually to update POI dates from vendor_id

from django.db import migrations
from datetime import datetime
import re


def update_poi_dates_from_vendor_id(apps, schema_editor):
    """
    Update all PointsOfInterest records to populate date_image_taken field from vendor_id.
    
    The vendor_id format appears to be: DDMMMYY... where:
    - DD = day (2 digits)
    - MMM = month abbreviation (3 letters)
    - YY = year (2 digits, assuming 20XX)
    
    Example: "24MAR01151632-P1BS-508221188010_01_P004" -> 2024-03-24
    """
    PointsOfInterest = apps.get_model('animal', 'PointsOfInterest')
    
    # Month abbreviations mapping
    month_map = {
        'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
        'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
    }
    
    # Get POI records that need updating (only those without date_image_taken)
    pois = PointsOfInterest.objects.filter(
        vendor_id__isnull=False,
        date_image_taken__isnull=True
    ).exclude(vendor_id='')
    
    # Pattern to match vendor_id format: DDMMMYY at the beginning
    vendor_id_pattern = re.compile(r'^(\d{2})([A-Z]{3})(\d{2})')
    
    updated_count = 0
    error_count = 0
    
    for poi in pois:
        try:
            vendor_id = poi.vendor_id
            match = vendor_id_pattern.match(vendor_id)
            
            if match:
                day_str, month_str, year_str = match.groups()
                
                day = int(day_str)
                month = month_map.get(month_str.upper())
                
                if month is None:
                    print(f'WARNING: Invalid month "{month_str}" in vendor_id: {vendor_id}')
                    error_count += 1
                    continue
                
                # Assume 20XX for years 00-99
                year = 2000 + int(year_str)
                
                try:
                    # Create date object to validate
                    image_date = datetime(year, month, day).date()
                    
                    poi.date_image_taken = image_date
                    poi.save(update_fields=['date_image_taken'])
                    
                    print(f'Updated POI {poi.id}: vendor_id="{vendor_id}" -> date_image_taken={image_date}')
                    updated_count += 1
                    
                except ValueError as e:
                    print(f'ERROR: Invalid date from vendor_id "{vendor_id}": {e}')
                    error_count += 1
                    
            else:
                print(f'WARNING: Could not parse date from vendor_id: {vendor_id}')
                error_count += 1
                
        except Exception as e:
            print(f'ERROR: Error processing POI {poi.id}: {e}')
            error_count += 1
    
    print(f'Migration completed: {updated_count} records updated, {error_count} errors')


def reverse_poi_dates_update(apps, schema_editor):
    """
    Reverse the migration by setting date_image_taken back to NULL.
    This is optional since the field allows NULL values.
    """
    PointsOfInterest = apps.get_model('animal', 'PointsOfInterest')
    
    # Set all date_image_taken fields back to NULL
    updated = PointsOfInterest.objects.update(date_image_taken=None)
    print(f'Reversed migration: {updated} records had date_image_taken set back to NULL')


class Migration(migrations.Migration):

    dependencies = [
        ('animal', '0015_alter_target_options_remove_areaofinterest_requestor_and_more'),
    ]

    operations = [
        migrations.RunPython(
            code=update_poi_dates_from_vendor_id,
            reverse_code=reverse_poi_dates_update,
        ),
    ]
