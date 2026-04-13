"""
Django forms module for animal observation and satellite imagery data management.

This module provides form classes for handling various aspects of animal observation data
and satellite imagery queries. It includes forms for API queries, data processing,
and animal observation recording with custom USWDS-styled widgets.

Classes:
    APIQueryForm: Form for querying various satellite imagery APIs with authentication
        and search parameters.
    
    ProcessingForm: Form for processing and filtering ETL data based on various parameters
        including spatial bounds, dates, and vendor information.
    
    USWDSButtonGroupWidget: Custom widget implementing USWDS-styled button groups with
        special handling for "Unsure" and "Animal" options.
    
    USWDSRadioButtonGroupWidget: Custom widget for rendering USWDS-styled radio button
        groups.
    
    PointsOfInterestForm: ModelForm for managing animal observation data from multiple
        users with fields for classification, species identification, and confidence
        levels.

Dependencies:
    - Django forms and GIS forms
    - datetime for default date handling
    - Custom models (AreaOfInterest, ExtractTransformLoad, PointsOfInterest)
    - Django utilities (safestring, forms.utils)

Note:
    All custom widgets follow USWDS (U.S. Web Design System) styling guidelines
    for consistent government web design standards.
"""

from datetime import datetime
from django import forms
from .models import AreaOfInterest, ExtractTransformLoad, PointsOfInterest, Annotations, Classification, Confidence, Target, FishnetReviews, Category, Project, AGE_CHOICES
from django.utils.safestring import mark_safe
from django.forms.utils import flatatt
import logging
logger = logging.getLogger('animal')


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Django FileField variant that returns a list of uploaded files."""

    def clean(self, data, initial=None):
        clean_one = super().clean
        if isinstance(data, (list, tuple)):
            return [clean_one(item, initial) for item in data if item]
        if data:
            return [clean_one(data, initial)]
        return []

class USWDSRadioButtonGroupWidget(forms.Widget):
    """
    A custom Django form widget that renders a group of radio buttons styled
    according to the U.S. Web Design System (USWDS) standards.

    Attributes:
        choices (list): A list of tuples containing the value and label for each radio button option.
        attrs (dict, optional): Additional HTML attributes for the widget.

    Methods:
        render(name, value, attrs=None, renderer=None):
            Renders the HTML for the radio button group.

            Args:
                name (str): The name of the form field.
                value (str): The currently selected value.
                attrs (dict, optional): Additional HTML attributes for the widget.
                renderer (optional): An optional renderer instance.

            Returns:
                str: The HTML for the radio button group, marked safe for rendering.
    """
    def __init__(self, choices, attrs=None):
        super().__init__(attrs)
        self.choices = choices

    def render(self, name, value, attrs=None, renderer=None):
        if attrs is None:
            attrs = {}
        attrs['id'] = attrs.get('id', f'id_{name}')
        radios = []
        # Sort choices alphabetically by label
        sorted_choices = sorted(self.choices, key=lambda x: x[1])
        for val, label in sorted_choices:
            if val in [None, '']:
                continue
            input_attrs = {
                'type': 'radio',
                'name': name,
                'value': val,
                'id': f'{attrs["id"]}_{val}',
                'class': 'usa-radio__input'
            }
            if str(value) == str(val):
                input_attrs['checked'] = 'checked'
            radio_input = f'<input {flatatt(input_attrs)}>'
            label_html = f'<label for="{input_attrs["id"]}" class="usa-radio__label">{label}</label>'
            radios.append(
                f'<div class="usa-radio">{radio_input}{label_html}</div>'
            )
        return mark_safe('<fieldset class="usa-fieldset">' + ''.join(radios) + '</fieldset>')


class USWDSRadioButtonGroupWidgetNoSort(USWDSRadioButtonGroupWidget):
    """Render choices in provided order rather than alphabetically."""

    def render(self, name, value, attrs=None, renderer=None):
        if attrs is None:
            attrs = {}
        attrs['id'] = attrs.get('id', f'id_{name}')
        radios = []
        for val, label in self.choices:
            if val in [None, '']:
                continue
            input_attrs = {
                'type': 'radio',
                'name': name,
                'value': val,
                'id': f'{attrs["id"]}_{val}',
                'class': 'usa-radio__input'
            }
            if str(value) == str(val):
                input_attrs['checked'] = 'checked'
            radio_input = f'<input {flatatt(input_attrs)}>'
            label_html = f'<label for="{input_attrs["id"]}" class="usa-radio__label">{label}</label>'
            radios.append(
                f'<div class="usa-radio">{radio_input}{label_html}</div>'
            )
        return mark_safe('<fieldset class="usa-fieldset">' + ''.join(radios) + '</fieldset>')

class USWDSCheckboxGroupWidget(forms.Widget):
    """
    A custom Django form widget that renders a group of checkboxes styled
    according to the U.S. Web Design System (USWDS) standards.

    Attributes:
        choices (list): A list of tuples containing the value and label for each checkbox option.
        attrs (dict, optional): Additional HTML attributes for the widget.

    Methods:
        render(name, value, attrs=None, renderer=None):
            Renders the HTML for the checkbox group.

            Args:
                name (str): The name of the form field.
                value (str/list): The currently selected value(s).
                attrs (dict, optional): Additional HTML attributes for the widget.
                renderer (optional): An optional renderer instance.

            Returns:
                str: The HTML for the checkbox group, marked safe for rendering.
    """
    def __init__(self, choices, attrs=None):
        super().__init__(attrs)
        self.choices = choices

    def render(self, name, value, attrs=None, renderer=None):
        if attrs is None:
            attrs = {}
        attrs['id'] = attrs.get('id', f'id_{name}')
        checkboxes = []
        
        # Use format_value to ensure consistent value handling
        formatted_value = self.format_value(value)
        
        # Sort choices alphabetically by label
        sorted_choices = sorted(self.choices, key=lambda x: x[1])
        for val, label in sorted_choices:
            if val in [None, '']:
                continue
            input_attrs = {
                'type': 'checkbox',
                'name': name,
                'value': val,
                'id': f'{attrs["id"]}_{val}',
                'class': 'usa-checkbox__input'
            }
            # Check if this value should be checked
            if str(val) in [str(v) for v in formatted_value]:
                input_attrs['checked'] = 'checked'
            checkbox_input = f'<input {flatatt(input_attrs)}>'
            label_html = f'<label for="{input_attrs["id"]}" class="usa-checkbox__label">{label}</label>'
            checkboxes.append(
                f'<div class="usa-checkbox">{checkbox_input}{label_html}</div>'
            )
        return mark_safe('<fieldset class="usa-fieldset">' + ''.join(checkboxes) + '</fieldset>')

    def value_from_datadict(self, data, files, name):
        """
        Extract the value from form data for multiple checkboxes.
        Returns a list of selected values.
        """
        if data is None:
            return []

        # QueryDict/MultiValueDict path used in normal requests.
        if hasattr(data, 'getlist'):
            values = data.getlist(name)
            return values if values else []

        # Plain dict path used in some unit tests and programmatic form usage.
        raw_value = data.get(name)
        if raw_value is None:
            return []
        if isinstance(raw_value, (list, tuple)):
            return list(raw_value)
        return [raw_value]
    
    def value_omitted_from_data(self, data, files, name):
        """
        Return True if no checkbox was checked, False otherwise.
        This is important for MultipleChoiceField to work correctly.
        """
        return name not in data

    def format_value(self, value):
        """
        Format the value for display in the widget.
        Ensures the value is always a list for consistency.
        """
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, str) and value:
            return [value]
        return []

class APIQueryForm(forms.Form):
    """A Django Form for querying various satellite imagery APIs.

    This form provides fields for API selection, authentication, and search parameters
    for querying satellite imagery from multiple providers including USGS EarthExplorer,
    Global Enhanced GEOINT Delivery, and Maxar Geospatial Platform.

    Attributes:
        api (ChoiceField): Selection field for choosing the target API
        username (CharField): Input field for API username/credentials 
        password (CharField): Secured input field for API password
        aoi (ModelChoiceField): Selection field for choosing an Area of Interest
        start_date (DateField): Start date for the imagery search period
        end_date (DateField): End date for the imagery search period, defaults to current date
    """
    API_CHOICES = [
        ('ee', 'USGS EarthExplorer'),
        # ('gegd', 'Global Enhanced GEOINT Delivery'),
        # ('mgp', 'Maxar Geospatial Platform'),
    ]

    SENSOR_CHOICES = [
        ('worldview_2', 'WorldView-2'),
        ('worldview_3', 'WorldView-3'),
        ('geoeye', 'GeoEye-1')

    ]

    VENDOR_CHOICES = [
        ('maxar', 'Maxar Technologies'),
        ('digitalglobe', 'Digital Globe'),
    ]

    SEARCH_MODE_CHOICES = [
        ('aoi', 'Area of Interest'),
        ('id', 'Vendor ID / Catalog ID'),
    ]

    api = forms.ChoiceField(
        choices=API_CHOICES,
        label="Select API",
        required=False,
        initial="ee",
        widget=USWDSRadioButtonGroupWidget(choices=API_CHOICES)
    )
    username = forms.CharField(widget=forms.HiddenInput(),
                               max_length=100,
                               label="API Username",
                               required=False)
    password = forms.CharField(widget=forms.HiddenInput(),
                               label="API Password",
                               required=False)
    search_mode = forms.ChoiceField(
        choices=SEARCH_MODE_CHOICES,
        label="Search Mode",
        required=False,
        initial='aoi',
        widget=USWDSRadioButtonGroupWidgetNoSort(choices=SEARCH_MODE_CHOICES),
    )
    aoi = forms.ModelChoiceField(queryset=AreaOfInterest.objects.all(),
                                 label="Area of Interest",
                                 required=False,
                                 widget=forms.Select(attrs={
                                    "class": "usa-select",
                                    "aria-label": "Sensor"
                                }))
    id_input = forms.CharField(
        label="Vendor ID / Catalog ID",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "usa-textarea",
                "rows": 4,
                "placeholder": "Paste one or more IDs (comma or newline separated)",
            }
        ),
    )
    id_geojson_file = forms.FileField(
        label="Vendor/Catalog GeoJSON File",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={
                "class": "usa-file-input",
                "accept": ".geojson,application/geo+json,application/json",
            }
        ),
    )
    start_date = forms.DateField(
        label="Start Date",
        required=False,
        widget=forms.DateInput(
            attrs={
                "class": "usa-input",
                "id": "event-date-start",
                "type": "text",
                "aria-label": "Start date",
                "data-date-range": "true",
                "data-date-range-end": "#id_end_date",
                "max": datetime.now().strftime("%Y-%m-%d"),
            }
        )
    )
    end_date = forms.DateField(
        label="End Date",
        required=False,
        widget=forms.DateInput(
            attrs={
                "class": "usa-input",
                "id": "event-date-end",
                "type": "text",
                "aria-label": "End date",
                "data-date-range": "true",
                "max": datetime.now().strftime("%Y-%m-%d")
            }
        )
    )
    sensor = forms.MultipleChoiceField(
        choices=SENSOR_CHOICES,
        label="Sensor",
        required=False,
        initial=['worldview_3'],
        widget=USWDSCheckboxGroupWidget(choices=SENSOR_CHOICES)
    )
    vendor = forms.ChoiceField(
        choices=VENDOR_CHOICES,
        label="Vendor",
        required=False,
        initial='maxar',
        widget=USWDSRadioButtonGroupWidget(choices=VENDOR_CHOICES)
    )

    def clean_sensor(self):
        """
        Clean the sensor field to ensure it returns a list even if only one item is selected.
        This handles both single and multiple checkbox selections correctly.
        """
        sensor_data = self.cleaned_data.get('sensor', [])
        
        # Ensure we always return a list
        if isinstance(sensor_data, str):
            return [sensor_data]
        elif isinstance(sensor_data, (list, tuple)):
            return list(sensor_data)
        else:
            return []

    @staticmethod
    def parse_identifier_tokens(raw_value):
        """Return de-duplicated ID tokens split by comma/newline."""
        if not raw_value:
            return []

        normalized = str(raw_value).replace('\r', '\n')
        parts = []
        for line in normalized.split('\n'):
            parts.extend(line.split(','))

        tokens = []
        seen = set()
        for part in parts:
            token = part.strip()
            if not token:
                continue
            token_key = token.lower()
            if token_key in seen:
                continue
            seen.add(token_key)
            tokens.append(token)
        return tokens

    def clean(self):
        """
        Custom form validation for sensor and mode-specific requirements.
        """
        cleaned_data = super().clean()
        sensor_list = cleaned_data.get('sensor', [])
        search_mode = cleaned_data.get('search_mode', 'aoi')
        id_input = cleaned_data.get('id_input', '')
        id_geojson_file = cleaned_data.get('id_geojson_file')
        id_tokens = self.parse_identifier_tokens(id_input)

        cleaned_data['id_tokens'] = id_tokens
        
        # Validate that all selected sensors are valid choices
        valid_sensor_values = [choice[0] for choice in self.SENSOR_CHOICES]
        for sensor in sensor_list:
            if sensor not in valid_sensor_values:
                raise forms.ValidationError(f"Invalid sensor selection: {sensor}")

        if search_mode == 'aoi' and not cleaned_data.get('aoi'):
            self.add_error('aoi', 'Please select an Area of Interest.')

        if search_mode == 'id':
            if not id_geojson_file:
                self.add_error('id_geojson_file', 'Please upload a GeoJSON file.')
            else:
                file_name = (id_geojson_file.name or '').lower()
                if file_name and not file_name.endswith('.geojson'):
                    self.add_error('id_geojson_file', 'Only .geojson files are supported.')
            # ID mode now uses file-derived identifiers, not free-text tokens.
            cleaned_data['id_tokens'] = []

        if search_mode == 'aoi' and not cleaned_data.get('start_date'):
            self.add_error('start_date', 'Please provide a start date.')

        if search_mode == 'aoi' and not cleaned_data.get('end_date'):
            self.add_error('end_date', 'Please provide an end date.')

        if not sensor_list:
            self.add_error('sensor', 'Please select at least one sensor.')
        
        return cleaned_data


class LoadPointsForm(forms.Form):
    geojson_files = MultipleFileField(
        label="GeoJSON Files",
        required=True,
        widget=MultipleFileInput(
            attrs={
                "class": "usa-file-input",
                "accept": ".geojson,application/geo+json,application/json",
            }
        ),
    )
    vendor_id_select = forms.ChoiceField(
        label="Vendor ID (optional)",
        required=False,
        choices=(),
        widget=forms.Select(
            attrs={
                "class": "usa-select",
            }
        ),
    )

    def __init__(self, *args, vendor_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "Auto-detect from uploaded GeoJSON")]
        for vendor_id in vendor_choices or []:
            choices.append((vendor_id, vendor_id))
        self.fields["vendor_id_select"].choices = choices

    def clean_geojson_files(self):
        uploads = self.cleaned_data.get('geojson_files') or []
        if not uploads:
            raise forms.ValidationError('Please upload at least one GeoJSON file.')

        invalid = [
            (upload.name or '')
            for upload in uploads
            if not (upload.name or '').lower().endswith('.geojson')
        ]
        if invalid:
            raise forms.ValidationError(
                'Only .geojson files are supported. Invalid files: ' + ', '.join(invalid)
            )

        return uploads

class ProcessingForm(forms.Form):
    """
    A form class for processing data from ExtractTransformLoad model.

    This form provides fields for filtering and querying ETL data based on various parameters
    including table name, IDs, vendor information, spatial bounds, dates and area of interest.

    Attributes:
        table_name (ChoiceField): Dropdown of distinct table names from ETL model
        id (CharField): Optional ID field
        vendor_id (CharField): Optional vendor ID field
        entity_id (CharField): Optional entity ID field
        vendor (ChoiceField): Optional dropdown of distinct vendor names
        platform (ChoiceField): Optional dropdown of distinct platform names
        pixel_x_min (FloatField): Optional minimum x coordinate
        pixel_x_max (FloatField): Optional maximum x coordinate  
        pixel_y_min (FloatField): Optional minimum y coordinate
        pixel_y_max (FloatField): Optional maximum y coordinate
        date_min (DateField): Optional start date with year selection (2007-2033)
        date_max (DateField): Optional end date defaulting to current date
        publish_date_min (DateField): Optional publish start date
        publish_date_max (DateField): Optional publish end date defaulting to current date
        aoi (ModelChoiceField): Optional area of interest selection

    Meta:
        model: ExtractTransformLoad
        fields: All form fields listed above
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Dynamically populate choices to avoid module-level database queries
        try:
            table_choices = [(table, table) for table in ExtractTransformLoad.objects.values_list('table_name', flat=True).distinct()]
            vendor_choices = [(vendor, vendor) for vendor in ExtractTransformLoad.objects.values_list('vendor', flat=True).distinct()]
            platform_choices = [(platform, platform) for platform in ExtractTransformLoad.objects.values_list('platform', flat=True).distinct()]
        except:
            # Fallback choices if database is not available
            table_choices = []
            vendor_choices = []
            platform_choices = []
        
        self.fields['table_name'] = forms.ChoiceField(choices=table_choices, required=False)
        self.fields['vendor'] = forms.ChoiceField(choices=vendor_choices, required=False)
        self.fields['platform'] = forms.ChoiceField(choices=platform_choices, required=False)
    
    id = forms.CharField(required=False)
    vendor_id = forms.CharField(required=False)
    entity_id = forms.CharField(required=False)
    pixel_x_min = forms.FloatField(required=False)
    pixel_x_max = forms.FloatField(required=False)
    pixel_y_min = forms.FloatField(required=False)
    pixel_y_max = forms.FloatField(required=False)
    date_min = forms.DateField(required=False,
                               widget=forms.SelectDateWidget(years=range(2007, 2034)))
    date_max = forms.DateField(required=False,
                               widget=forms.SelectDateWidget(years=range(2007, 2034)),
                               initial = datetime.now())
    publish_date_min = forms.DateField(required=False,
                                       widget=forms.SelectDateWidget(years=range(2007, 2034)))
    publish_date_max = forms.DateField(required=False,
                                       widget=forms.SelectDateWidget(years=range(2007, 2034)),
                                       initial = datetime.now())
    aoi = forms.ModelChoiceField(required=False, queryset=AreaOfInterest.objects.all())

class USWDSButtonGroupWidget(forms.Widget):
    def __init__(self, choices, attrs=None):
        super().__init__(attrs)
        self.choices = choices

    def render(self, name, value, attrs=None, renderer=None):
        if attrs is None:
            attrs = {}
        attrs['id'] = attrs.get('id', f'id_{name}')

        # Get all classifications with their categories
        from .models import Classification, Category
        classifications = Classification.objects.select_related('category').all()
        
        # Group classifications by category
        categories_dict = {}
        uncategorized = []
        
        for classification in classifications:
            if classification.category:
                category_id = classification.category.id
                if category_id not in categories_dict:
                    categories_dict[category_id] = {
                        'category': classification.category,
                        'classifications': []
                    }
                categories_dict[category_id]['classifications'].append(classification)
            else:
                uncategorized.append(classification)
        
        # Sort categories by their order field
        sorted_categories = sorted(categories_dict.values(), key=lambda x: x['category'].order)
        
        buttons = []
        
        # Render buttons grouped by category
        for category_group in sorted_categories:
            category = category_group['category']
            category_classifications = category_group['classifications']
            
            # Sort classifications within category by their order field
            sorted_classifications = sorted(category_classifications, key=lambda x: x.order)
            
            # Add category header
            if category.name:
                buttons.append(f'<div class="usa-button-group__label margin-top-2 margin-bottom-1"><strong>{category.name}</strong></div>')
            
            # Add buttons for this category
            for classification in sorted_classifications:
                if classification.id in [None, '']:
                    continue
                button_attrs = {
                    'type': 'button',
                    'class': 'usa-button margin-1',
                    'data-value': classification.id,
                }
                if classification.label == "Unsure":
                    button_attrs['class'] += ' usa-button--outline'
                if str(value) == str(classification.id):
                    button_attrs['class'] += ' usa-button--active'
                
                # Auto-submit for all classifications except Animal, but through our event listener
                submit_script = "" if classification.label == "Animal" else "setTimeout(() => this.form.requestSubmit(), 10);"
                button_html = f'''<button {flatatt(button_attrs)} onclick="
                    this.parentElement.nextElementSibling.value = this.dataset.value;
                    {submit_script}">{classification.label}</button>'''
                buttons.append(button_html)
        
        # Add uncategorized classifications at the end
        if uncategorized:
            # Sort uncategorized by their order field
            sorted_uncategorized = sorted(uncategorized, key=lambda x: x.order)
            
            buttons.append(f'<div class="usa-button-group__label margin-top-2 margin-bottom-1"><strong>Other</strong></div>')
            for classification in sorted_uncategorized:
                if classification.id in [None, '']:
                    continue
                button_attrs = {
                    'type': 'button',
                    'class': 'usa-button margin-1',
                    'data-value': classification.id,
                }
                if classification.label == "Unsure":
                    button_attrs['class'] += ' usa-button--outline'
                if str(value) == str(classification.id):
                    button_attrs['class'] += ' usa-button--active'
                
                # Auto-submit for all classifications except Animal, but through our event listener
                submit_script = "" if classification.label == "Animal" else "setTimeout(() => this.form.requestSubmit(), 10);"
                button_html = f'''<button {flatatt(button_attrs)} onclick="
                    this.parentElement.nextElementSibling.value = this.dataset.value;
                    {submit_script}">{classification.label}</button>'''
                buttons.append(button_html)

        hidden_input = f'<input type="hidden" name="{name}" value="{value or ""}" {flatatt(attrs)}>'
        return mark_safe('<div id="classification-buttongroup" class="">' + ''.join(buttons) + '</div>' + hidden_input)

class USWDSRadioButtonGroupWidget(forms.Widget):
    """
    A custom Django form widget that renders a group of radio buttons styled
    according to the U.S. Web Design System (USWDS) standards.

    Attributes:
        choices (list): A list of tuples containing the value and label for each radio button option.
        attrs (dict, optional): Additional HTML attributes for the widget.

    Methods:
        render(name, value, attrs=None, renderer=None):
            Renders the HTML for the radio button group.

            Args:
                name (str): The name of the form field.
                value (str): The currently selected value.
                attrs (dict, optional): Additional HTML attributes for the widget.
                renderer (optional): An optional renderer instance.

            Returns:
                str: The HTML for the radio button group, marked safe for rendering.
    """
    def __init__(self, choices, attrs=None):
        super().__init__(attrs)
        self.choices = choices

    def render(self, name, value, attrs=None, renderer=None):
        if attrs is None:
            attrs = {}
        attrs['id'] = attrs.get('id', f'id_{name}')
        radios = []
        
        # Sort choices alphabetically by label
        sorted_choices = sorted(self.choices, key=lambda x: x[1])
        
        for val, label in sorted_choices:
            if val in [None, '']:
                continue
            input_attrs = {
                'type': 'radio',
                'name': name,
                'value': val,
                'id': f'{attrs["id"]}_{val}',
                'class': 'usa-radio__input'
            }
            if str(value) == str(val):
                input_attrs['checked'] = 'checked'
            radio_input = f'<input {flatatt(input_attrs)}>'
            label_html = f'<label for="{input_attrs["id"]}" class="usa-radio__label">{label}</label>'
            radios.append(
                f'<div class="usa-radio">{radio_input}{label_html}</div>'
            )
        return mark_safe('<fieldset class="usa-fieldset">' + ''.join(radios) + '</fieldset>')

class PointsOfInterestForm(forms.ModelForm):
    class Meta:
        model = PointsOfInterest
        fields = ['id', 'vendor_id', 'point', 'final_review_date', 'final_species', 'final_classification', 'final_confidence']
        widgets = {

        }

class AnnotationForm(forms.ModelForm):
    classification = forms.ModelChoiceField(
        queryset=Classification.objects.select_related('category').all(),
        widget=USWDSButtonGroupWidget(
            choices=[]  # Choices will be handled dynamically in the widget
        )
    )
    class Meta:
        model = Annotations
        fields = ['poi', 'user', 'classification', 'comments', 'confidence', 'target', 'age']
        widgets = {
            'comments': forms.Textarea(attrs={'maxlength': 500, 'class': 'usa-textarea', 'id':'comments-textarea'}),
            'target': USWDSRadioButtonGroupWidget(choices=Target),
            'confidence': USWDSRadioButtonGroupWidget(choices=Confidence),
            'age': USWDSRadioButtonGroupWidgetNoSort(choices=AGE_CHOICES),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and not self.initial.get('age'):
            self.initial['age'] = 'adult'
    
    def clean(self):
        cleaned_data = super().clean()
        classification = cleaned_data.get('classification')
        target = cleaned_data.get('target')
        confidence = cleaned_data.get('confidence')
        age = cleaned_data.get('age')
        is_new_annotation = not bool(self.instance and self.instance.pk)

        if str(classification).lower() == "animal":
            if not target or not confidence:
                raise forms.ValidationError("Target and Confidence are required when Classification is Animal.")
            if is_new_annotation and not age:
                raise forms.ValidationError("Age is required when creating an Animal annotation.")
        else:
            cleaned_data['target'] = None
            cleaned_data['confidence'] = None
            cleaned_data['age'] = None

        return cleaned_data
        
class FishnetForm(forms.ModelForm):
    class Meta:
        model = FishnetReviews
        fields = ['fishnet', 'user', 'id', 'date']