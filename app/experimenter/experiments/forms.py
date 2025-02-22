import json

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.forms import BaseInlineFormSet
from django.forms import inlineformset_factory
from django.forms.models import ModelChoiceIterator
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from experimenter.base.models import Country, Locale
from experimenter.experiments.constants import ExperimentConstants
from experimenter.experiments import tasks
from experimenter.experiments.bugzilla import get_bugzilla_id
from experimenter.experiments.models import (
    Experiment,
    ExperimentComment,
    ExperimentChangeLog,
    ExperimentVariant,
)
from experimenter.experiments.serializers import ChangeLogSerializer
from experimenter.notifications.models import Notification


class NameSlugFormMixin(object):
    """
    Automatically generate a slug from the name field
    """

    def clean_name(self):
        name = self.cleaned_data["name"]
        slug = slugify(name)

        if not slug:
            raise forms.ValidationError(
                "This name must include non-punctuation characters."
            )

        return name

    def clean(self):
        cleaned_data = super().clean()

        if self.instance.slug:
            del cleaned_data["slug"]
        else:
            name = cleaned_data.get("name")
            cleaned_data["slug"] = slugify(name)

        return cleaned_data


class JSONField(forms.CharField):

    def clean(self, value):
        cleaned_value = super().clean(value)

        if cleaned_value:
            try:
                json.loads(cleaned_value)
            except json.JSONDecodeError:
                raise forms.ValidationError("This is not valid JSON.")

        return cleaned_value


class BugzillaURLField(forms.URLField):

    def clean(self, value):
        cleaned_value = super().clean(value)

        if cleaned_value:
            err_str = "Please Provide a Valid URL ex: {}show_bug.cgi?id=1234"
            if (
                settings.BUGZILLA_HOST not in cleaned_value
                or get_bugzilla_id(cleaned_value) is None
            ):
                raise forms.ValidationError(err_str.format(settings.BUGZILLA_HOST))

        return cleaned_value


class ChangeLogMixin(object):

    def __init__(self, request, *args, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)
        if self.instance.id:
            self.old_serialized_vals = ChangeLogSerializer(self.instance).data

    def get_changelog_message(self):
        return ""

    def save(self, *args, **kwargs):

        experiment = super().save(*args, **kwargs)

        changed_values = {}
        old_status = None

        self.new_serialized_vals = ChangeLogSerializer(self.instance).data
        latest_change = experiment.changes.latest()

        # account for changes in variant values
        if latest_change:
            old_status = latest_change.new_status
            if (
                self.old_serialized_vals["variants"]
                != self.new_serialized_vals["variants"]
            ):
                old_value = self.old_serialized_vals["variants"]
                new_value = self.new_serialized_vals["variants"]
                display_name = "Branches"
                changed_values["variants"] = {
                    "old_value": old_value,
                    "new_value": new_value,
                    "display_name": display_name,
                }

        elif self.new_serialized_vals.get("variants"):
            old_value = None
            new_value = self.new_serialized_vals["variants"]
            display_name = "Branches"
            changed_values["variants"] = {
                "old_value": old_value,
                "new_value": new_value,
                "display_name": display_name,
            }

        if self.changed_data:
            if latest_change:
                old_status = latest_change.new_status

                for field in self.changed_data:
                    old_val = None
                    new_val = None

                    if field in self.old_serialized_vals:
                        if field in ("countries", "locales"):
                            old_field_values = self.old_serialized_vals[field]
                            codes = [obj["code"] for obj in old_field_values]
                            old_val = codes
                        else:
                            old_val = self.old_serialized_vals[field]
                    if field in self.new_serialized_vals:
                        if field in ("countries", "locales"):
                            new_field_values = self.new_serialized_vals[field]
                            codes = [obj["code"] for obj in new_field_values]
                            new_val = codes
                        else:
                            new_val = self.new_serialized_vals[field]

                    display_name = self._get_display_name(field)

                    if new_val or old_val:
                        changed_values[field] = {
                            "old_value": old_val,
                            "new_value": new_val,
                            "display_name": display_name,
                        }

            else:
                for field in self.changed_data:
                    old_val = None
                    new_val = None
                    if field in self.new_serialized_vals:
                        if field in ("countries", "locales"):
                            new_field_values = self.new_serialized_vals[field]
                            codes = [obj["code"] for obj in new_field_values]
                            new_val = codes
                        else:
                            new_val = self.new_serialized_vals[field]
                        display_name = self._get_display_name(field)
                        changed_values[field] = {
                            "old_value": old_val,
                            "new_value": new_val,
                            "display_name": display_name,
                        }

        ExperimentChangeLog.objects.create(
            experiment=experiment,
            changed_by=self.request.user,
            old_status=old_status,
            new_status=experiment.status,
            changed_values=changed_values,
            message=self.get_changelog_message(),
        )

        return experiment

    def _get_display_name(self, field):
        if self.fields[field].label:
            return self.fields[field].label
        return field.replace("_", " ").title()


class ExperimentOverviewForm(NameSlugFormMixin, ChangeLogMixin, forms.ModelForm):

    type = forms.ChoiceField(
        label="Type", choices=Experiment.TYPE_CHOICES, help_text=Experiment.TYPE_HELP_TEXT
    )
    name = forms.CharField(label="Name", help_text=Experiment.NAME_HELP_TEXT)
    slug = forms.CharField(required=False, widget=forms.HiddenInput())
    short_description = forms.CharField(
        label="Short Description",
        help_text=Experiment.SHORT_DESCRIPTION_HELP_TEXT,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    data_science_bugzilla_url = BugzillaURLField(
        label="Data Science Bugzilla URL",
        help_text=Experiment.DATA_SCIENCE_BUGZILLA_HELP_TEXT,
    )
    owner = forms.ModelChoiceField(
        required=True,
        label="Experiment Owner",
        help_text=Experiment.OWNER_HELP_TEXT,
        queryset=get_user_model().objects.all().order_by("email"),
        # This one forces the <select> widget to not include a blank
        # option which would otherwise be included because the model field
        # is nullable.
        empty_label=None,
    )
    engineering_owner = forms.CharField(
        required=False,
        label="Engineering Owner",
        help_text=Experiment.ENGINEERING_OWNER_HELP_TEXT,
    )
    analysis_owner = forms.CharField(
        required=True,
        label="Data Science Owner",
        help_text=Experiment.ANALYSIS_OWNER_HELP_TEXT,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    public_name = forms.CharField(
        label="Public Name", required=False, help_text=Experiment.PUBLIC_NAME_HELP_TEXT
    )
    public_description = forms.CharField(
        label="Public Description",
        required=False,
        help_text=Experiment.PUBLIC_DESCRIPTION_HELP_TEXT,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    feature_bugzilla_url = BugzillaURLField(
        required=False,
        label="Feature Bugzilla URL",
        help_text=Experiment.FEATURE_BUGZILLA_HELP_TEXT,
    )
    related_work = forms.CharField(
        required=False,
        label="Related Work URLs",
        help_text=Experiment.RELATED_WORK_HELP_TEXT,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    related_to = forms.ModelMultipleChoiceField(
        label="Related Experiments",
        required=False,
        help_text="Is this related to a previously run experiment?",
        queryset=Experiment.objects.all(),
    )

    class Meta:
        model = Experiment
        fields = [
            "type",
            "name",
            "slug",
            "short_description",
            "data_science_bugzilla_url",
            "owner",
            "analysis_owner",
            "engineering_owner",
            "public_name",
            "public_description",
            "feature_bugzilla_url",
            "related_work",
            "related_to",
        ]

    related_to.widget.attrs.update({"data-live-search": "true"})

    def clean_name(self):
        name = super().clean_name()
        slug = slugify(name)

        if (
            self.instance.pk is None
            and slug
            and self.Meta.model.objects.filter(slug=slug).exists()
        ):
            raise forms.ValidationError("This name is already in use.")

        return name


class ExperimentVariantGenericForm(NameSlugFormMixin, forms.ModelForm):

    experiment = forms.ModelChoiceField(queryset=Experiment.objects.all(), required=False)
    is_control = forms.BooleanField(required=False)
    ratio = forms.IntegerField(
        label="Branch Size",
        initial="50",
        min_value=1,
        max_value=100,
        help_text=Experiment.CONTROL_RATIO_HELP_TEXT,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    name = forms.CharField(
        label="Name",
        help_text=Experiment.CONTROL_NAME_HELP_TEXT,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    description = forms.CharField(
        label="Description",
        help_text=Experiment.CONTROL_DESCRIPTION_HELP_TEXT,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
    slug = forms.CharField(required=False)

    class Meta:
        model = ExperimentVariant
        fields = ["description", "experiment", "is_control", "name", "ratio", "slug"]


class ExperimentVariantPrefForm(ExperimentVariantGenericForm):

    value = forms.CharField(
        label="Pref Value",
        help_text=Experiment.CONTROL_VALUE_HELP_TEXT,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    class Meta:
        model = ExperimentVariant
        fields = [
            "description",
            "experiment",
            "is_control",
            "name",
            "ratio",
            "slug",
            "value",
        ]


class ExperimentVariantsFormSet(BaseInlineFormSet):

    def clean(self):
        alive_forms = [form for form in self.forms if not form.cleaned_data["DELETE"]]

        total_percentage = sum(
            [form.cleaned_data.get("ratio", 0) for form in alive_forms]
        )

        if total_percentage != 100:
            for form in alive_forms:
                form._errors["ratio"] = ["The size of all branches must add up to 100"]

        if all([f.is_valid() for f in alive_forms]):
            unique_names = set(
                form.cleaned_data["name"]
                for form in alive_forms
                if form.cleaned_data.get("name")
            )

            if not len(unique_names) == len(alive_forms):
                for form in alive_forms:
                    form._errors["name"] = ["All branches must have a unique name"]


class ExperimentVariantsPrefFormSet(ExperimentVariantsFormSet):

    def clean(self):
        super().clean()

        alive_forms = [
            form
            for form in self.forms
            if form.is_valid() and not form.cleaned_data["DELETE"]
        ]

        forms_by_value = {}
        for form in alive_forms:
            value = form.cleaned_data["value"]
            forms_by_value.setdefault(value, []).append(form)

        for dupe_forms in forms_by_value.values():
            if len(dupe_forms) > 1:
                for form in dupe_forms:
                    form.add_error("value", "All branches must have a unique pref value")


class CustomModelChoiceIterator(ModelChoiceIterator):

    def __iter__(self):
        yield (CustomModelMultipleChoiceField.ALL_KEY, self.field.all_label)
        for choice in super().__iter__():
            yield choice


class CustomModelMultipleChoiceField(forms.ModelMultipleChoiceField):
    """Return a ModelMultipleChoiceField but with the exception that
    there's one extra "All" choice inserted as the first choice.
    And when submitted, if "All" was one of the choices, reset
    it to chose nothing."""

    ALL_KEY = "__all__"

    def __init__(self, *args, **kwargs):
        self.all_label = kwargs.pop("all_label")
        super().__init__(*args, **kwargs)

    def clean(self, value):
        if value is not None:
            if self.ALL_KEY in value:
                value = []
            return super().clean(value)

    iterator = CustomModelChoiceIterator


class ExperimentTimelinePopulationForm(ChangeLogMixin, forms.ModelForm):
    proposed_start_date = forms.DateField(
        required=True,
        label="Proposed Start Date",
        help_text=Experiment.PROPOSED_START_DATE_HELP_TEXT,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    proposed_duration = forms.IntegerField(
        required=True,
        min_value=1,
        label="Proposed Experiment Duration (days)",
        help_text=Experiment.PROPOSED_DURATION_HELP_TEXT,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    proposed_enrollment = forms.IntegerField(
        required=False,
        min_value=1,
        label="Proposed Enrollment Duration (days)",
        help_text=Experiment.PROPOSED_ENROLLMENT_HELP_TEXT,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    population_percent = forms.DecimalField(
        label="Population Percentage",
        help_text=Experiment.POPULATION_PERCENT_HELP_TEXT,
        initial="0.00",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    firefox_min_version = forms.ChoiceField(
        choices=Experiment.MIN_VERSION_CHOICES,
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text=Experiment.VERSION_HELP_TEXT,
    )
    firefox_max_version = forms.ChoiceField(
        choices=Experiment.MAX_VERSION_CHOICES,
        widget=forms.Select(attrs={"class": "form-control"}),
        required=False,
    )
    firefox_channel = forms.ChoiceField(
        choices=Experiment.CHANNEL_CHOICES,
        widget=forms.Select(attrs={"class": "form-control"}),
        label="Firefox Channel",
        help_text=Experiment.CHANNEL_HELP_TEXT,
    )
    client_matching = forms.CharField(
        label="Population Filtering",
        help_text=Experiment.CLIENT_MATCHING_HELP_TEXT,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 10}),
    )
    locales = CustomModelMultipleChoiceField(
        label="Locales",
        required=False,
        all_label="All locales",
        help_text="Applicable only if you don't select All",
        queryset=Locale.objects.all(),
        to_field_name="code",
    )
    countries = CustomModelMultipleChoiceField(
        label="Countries",
        required=False,
        all_label="All countries",
        help_text="Applicable only if you don't select All",
        queryset=Country.objects.all(),
        to_field_name="code",
    )
    # See https://developer.snapappointments.com/bootstrap-select/examples/
    # for more options that relate to the initial rendering of the HTML
    # as a way to customize how it works.
    locales.widget.attrs.update({"data-live-search": "true"})
    countries.widget.attrs.update({"data-live-search": "true"})

    class Meta:
        model = Experiment
        fields = [
            "proposed_start_date",
            "proposed_duration",
            "proposed_enrollment",
            "population_percent",
            "firefox_min_version",
            "firefox_max_version",
            "firefox_channel",
            "locales",
            "countries",
            "platform",
            "client_matching",
        ]

    def __init__(self, *args, **kwargs):
        data = kwargs.pop("data", None)
        instance = kwargs.pop("instance", None)
        if instance:
            # The reason we must do this is because the form fields
            # for locales and countries don't know about the instance
            # not having anything set, and we want the "All" option to
            # appear in the generated HTML widget.
            kwargs.setdefault("initial", {})
            if not instance.locales.all().exists():
                kwargs["initial"]["locales"] = [CustomModelMultipleChoiceField.ALL_KEY]
            if not instance.countries.all().exists():
                kwargs["initial"]["countries"] = [CustomModelMultipleChoiceField.ALL_KEY]
        super().__init__(data=data, instance=instance, *args, **kwargs)

    def clean_population_percent(self):
        population_percent = self.cleaned_data["population_percent"]

        if not (0 < population_percent <= 100):
            raise forms.ValidationError(
                "The population size must be between 0 and 100 percent."
            )

        return population_percent

    def clean_firefox_max_version(self):
        firefox_min_version = self.cleaned_data["firefox_min_version"]
        firefox_max_version = self.cleaned_data["firefox_max_version"]

        if firefox_max_version:
            if firefox_max_version <= firefox_min_version:
                raise forms.ValidationError(
                    "The max version must " "be larger than the min version."
                )

            return firefox_max_version

    def clean_proposed_start_date(self):
        start_date = self.cleaned_data["proposed_start_date"]

        if start_date and start_date < timezone.now().date():
            raise forms.ValidationError(
                ("The experiment start date must " "be no earlier than the current date.")
            )

        return start_date

    def clean(self):
        cleaned_data = super().clean()

        # enrollment may be None
        enrollment = cleaned_data.get("proposed_enrollment", None)
        duration = cleaned_data.get("proposed_duration", None)

        if (enrollment and duration) and enrollment > duration:
            msg = (
                "Enrollment duration is optional, but if set, "
                "must be lower than the experiment duration. "
                "If enrollment duration is not specified - users "
                "are enrolled for the entire experiment."
            )
            self._errors["proposed_enrollment"] = [msg]

        return cleaned_data


class ExperimentDesignBaseForm(ChangeLogMixin, forms.ModelForm):

    class Meta:
        model = Experiment
        fields = []

    def __init__(self, *args, **kwargs):
        data = kwargs.pop("data", None)
        instance = kwargs.pop("instance", None)

        extra = 0
        if instance and instance.variants.count() == 0:
            extra = 2

        FormSet = inlineformset_factory(
            can_delete=True,
            extra=extra,
            form=self.FORMSET_FORM_CLASS,
            formset=self.FORMSET_CLASS,
            model=ExperimentVariant,
            parent_model=Experiment,
        )

        self.variants_formset = FormSet(data=data, instance=instance)
        super().__init__(data=data, instance=instance, *args, **kwargs)

    def is_valid(self):
        return super().is_valid() and self.variants_formset.is_valid()

    @transaction.atomic
    def save(self, *args, **kwargs):
        self.variants_formset.save()
        return super().save(*args, **kwargs)


class ExperimentDesignGenericForm(ExperimentDesignBaseForm):

    FORMSET_FORM_CLASS = ExperimentVariantGenericForm
    FORMSET_CLASS = ExperimentVariantsFormSet

    design = forms.CharField(
        required=False,
        label="Design",
        help_text=Experiment.DESIGN_HELP_TEXT,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 10}),
    )

    class Meta:
        model = Experiment
        fields = ["design"]


class ExperimentDesignAddonForm(ExperimentDesignBaseForm):

    FORMSET_FORM_CLASS = ExperimentVariantGenericForm
    FORMSET_CLASS = ExperimentVariantsFormSet

    addon_experiment_id = forms.CharField(
        empty_value=None,
        max_length=settings.NORMANDY_SLUG_MAX_LEN,
        required=False,
        label="Active Experiment Name",
        help_text=Experiment.ADDON_EXPERIMENT_ID_HELP_TEXT,
    )
    addon_release_url = forms.URLField(
        required=False,
        label="Signed Release URL",
        help_text=Experiment.ADDON_RELEASE_URL_HELP_TEXT,
    )

    class Meta:
        model = Experiment
        fields = ExperimentDesignBaseForm.Meta.fields + [
            "addon_experiment_id",
            "addon_release_url",
        ]


class ExperimentDesignPrefForm(ExperimentDesignBaseForm):

    FORMSET_FORM_CLASS = ExperimentVariantPrefForm
    FORMSET_CLASS = ExperimentVariantsPrefFormSet

    pref_key = forms.CharField(
        label="Pref Name",
        help_text=Experiment.PREF_KEY_HELP_TEXT,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    pref_type = forms.ChoiceField(
        label="Pref Type",
        help_text=Experiment.PREF_TYPE_HELP_TEXT,
        choices=Experiment.PREF_TYPE_CHOICES,
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    pref_branch = forms.ChoiceField(
        label="Pref Branch",
        help_text=Experiment.PREF_BRANCH_HELP_TEXT,
        choices=Experiment.PREF_BRANCH_CHOICES,
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    class Meta:
        model = Experiment
        fields = ExperimentDesignBaseForm.Meta.fields + [
            "pref_key",
            "pref_type",
            "pref_branch",
        ]

    def clean(self):
        cleaned_data = super().clean()
        expected_type_mapping = expected_type = {
            Experiment.PREF_TYPE_BOOL: bool,
            Experiment.PREF_TYPE_INT: int,
            Experiment.PREF_TYPE_STR: str,
        }

        # Check that each pref value matches the global pref type of the form.
        pref_type = cleaned_data["pref_type"]
        expected_type = expected_type_mapping.get(pref_type, None)
        if pref_type != Experiment.PREF_TYPE_STR:

            for form in self.variants_formset.forms:
                try:
                    if form.is_valid():
                        found_type = type(json.loads(form.cleaned_data["value"]))

                        # type validation only for non json type
                        if expected_type and found_type != expected_type:
                            raise ValueError

                except (json.JSONDecodeError, ValueError):
                    form.add_error(
                        "value", f"Unexpected value type (should be {pref_type})"
                    )

        return cleaned_data


class RadioWidget(forms.widgets.RadioSelect):
    template_name = "experiments/radio_widget.html"


class RadioWidgetCloser(forms.widgets.RadioSelect):
    """
        This radio widget is similar to the RadioWidget
        except for the No and Yes buttons are closer together.
    """
    template_name = "experiments/radio_widget_closer.html"


class ExperimentObjectivesForm(ChangeLogMixin, forms.ModelForm):
    RADIO_OPTIONS = ((False, "No"), (True, "Yes"))

    def coerce_truthy(value):
        if type(value) == bool:
            return value
        if value.lower() == "true":
            return True
        return False

    objectives = forms.CharField(
        label="Objectives",
        help_text=Experiment.OBJECTIVES_HELP_TEXT,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 20}),
    )

    analysis = forms.CharField(
        label="Analysis Plan",
        help_text=Experiment.ANALYSIS_HELP_TEXT,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 20}),
    )

    survey_required = forms.TypedChoiceField(
        label=Experiment.SURVEY_REQUIRED_LABEL,
        help_text=Experiment.SURVEY_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidgetCloser,
        coerce=coerce_truthy,
        empty_value=None,
    )
    survey_urls = forms.CharField(
        required=False,
        help_text=Experiment.SURVEY_HELP_TEXT,
        label="Survey URLs",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 1}),
    )
    survey_instructions = forms.CharField(
        required=False,
        label=Experiment.SURVEY_INSTRUCTIONS_LABEL,
        help_text=Experiment.SURVEY_LAUNCH_INSTRUCTIONS_HELP_TEXT,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 10}),
    )

    class Meta:
        model = Experiment
        fields = (
            "objectives",
            "analysis",
            "survey_required",
            "survey_urls",
            "survey_instructions",
        )


class ExperimentResultsForm(ChangeLogMixin, forms.ModelForm):
    results_url = forms.URLField(
        label="Primary Results URL",
        help_text=Experiment.RESULTS_URL_HELP_TEXT,
        required=False,
    )
    results_initial = forms.CharField(
        label="Initial Results",
        help_text=Experiment.RESULTS_INITIAL_HELP_TEXT,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 10}),
        required=False,
    )
    results_lessons_learned = forms.CharField(
        label="Lessons Learned",
        help_text=Experiment.RESULTS_LESSONS_HELP_TEXT,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 20}),
        required=False,
    )

    class Meta:
        model = Experiment
        fields = ("results_url", "results_initial", "results_lessons_learned")


class ExperimentRisksForm(ChangeLogMixin, forms.ModelForm):
    RADIO_OPTIONS = ((False, "No"), (True, "Yes"))

    def coerce_truthy(value):
        if type(value) == bool:
            return value
        if value.lower() == "true":
            return True
        elif value.lower() == "false":
            return False

    # Radio Buttons
    risk_internal_only = forms.TypedChoiceField(
        label=Experiment.RISK_INTERNAL_ONLY_LABEL,
        help_text=Experiment.RISK_INTERNAL_ONLY_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_partner_related = forms.TypedChoiceField(
        label=Experiment.RISK_PARTNER_RELATED_LABEL,
        help_text=Experiment.RISK_PARTNER_RELATED_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_brand = forms.TypedChoiceField(
        label=Experiment.RISK_BRAND_LABEL,
        help_text=Experiment.RISK_BRAND_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_fast_shipped = forms.TypedChoiceField(
        label=Experiment.RISK_FAST_SHIPPED_LABEL,
        help_text=Experiment.RISK_FAST_SHIPPED_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_confidential = forms.TypedChoiceField(
        label=Experiment.RISK_CONFIDENTIAL_LABEL,
        help_text=Experiment.RISK_CONFIDENTIAL_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_release_population = forms.TypedChoiceField(
        label=Experiment.RISK_RELEASE_POPULATION_LABEL,
        help_text=Experiment.RISK_RELEASE_POPULATION_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_revenue = forms.TypedChoiceField(
        label=Experiment.RISK_REVENUE_LABEL,
        help_text=Experiment.RISK_REVENUE_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_data_category = forms.TypedChoiceField(
        label=Experiment.RISK_DATA_CATEGORY_LABEL,
        help_text=Experiment.RISK_DATA_CATEGORY_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_external_team_impact = forms.TypedChoiceField(
        label=Experiment.RISK_EXTERNAL_TEAM_IMPACT_LABEL,
        help_text=Experiment.RISK_EXTERNAL_TEAM_IMPACT_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_telemetry_data = forms.TypedChoiceField(
        label=Experiment.RISK_TELEMETRY_DATA_LABEL,
        help_text=Experiment.RISK_TELEMETRY_DATA_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_ux = forms.TypedChoiceField(
        label=Experiment.RISK_UX_LABEL,
        help_text=Experiment.RISK_UX_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_security = forms.TypedChoiceField(
        label=Experiment.RISK_SECURITY_LABEL,
        help_text=Experiment.RISK_SECURITY_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_revision = forms.TypedChoiceField(
        label=Experiment.RISK_REVISION_LABEL,
        help_text=Experiment.RISK_REVISION_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )
    risk_technical = forms.TypedChoiceField(
        label=Experiment.RISK_TECHNICAL_LABEL,
        help_text=Experiment.RISK_TECHNICAL_HELP_TEXT,
        choices=RADIO_OPTIONS,
        widget=RadioWidget,
        coerce=coerce_truthy,
        empty_value=None,
    )

    # Optional Risk Descriptions
    risk_technical_description = forms.CharField(
        required=False,
        label="Technical Risks Description",
        help_text=Experiment.RISK_TECHNICAL_HELP_TEXT,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 10,
                "placeholder": Experiment.RISK_TECHNICAL_DEFAULT,
            }
        ),
    )
    risks = forms.CharField(
        required=False,
        label="Risks",
        help_text=Experiment.RISKS_HELP_TEXT,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 20,
                "placeholder": Experiment.RISKS_DEFAULT,
            }
        ),
    )

    # Testing
    testing = forms.CharField(
        required=False,
        label="Test Instructions",
        help_text=Experiment.TESTING_HELP_TEXT,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 10,
                "placeholder": Experiment.TESTING_DEFAULT,
            }
        ),
    )
    test_builds = forms.CharField(
        required=False,
        label="Test Builds",
        help_text=Experiment.TEST_BUILDS_HELP_TEXT,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": Experiment.TEST_BUILDS_DEFAULT,
            }
        ),
    )
    qa_status = forms.CharField(
        label="QA Status",
        help_text=Experiment.QA_STATUS_HELP_TEXT,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": Experiment.QA_STATUS_DEFAULT,
            }
        ),
    )

    class Meta:
        model = Experiment
        fields = (
            "risk_internal_only",
            "risk_partner_related",
            "risk_brand",
            "risk_fast_shipped",
            "risk_confidential",
            "risk_release_population",
            "risk_revenue",
            "risk_data_category",
            "risk_external_team_impact",
            "risk_telemetry_data",
            "risk_ux",
            "risk_security",
            "risk_revision",
            "risk_technical",
            "risk_technical_description",
            "risks",
            "testing",
            "test_builds",
            "qa_status",
        )

    def clean(self):
        cleaned_data = super().clean()
        if (
            "risk_technical" in cleaned_data
            and "risk_technical_description" in cleaned_data
        ):
            # Both checked, now we need to do an invariance check on these
            # two. This is to match what's done with jQuery in the form:
            # the 'risk_technical_description' needs to be required
            # if 'risk_technical' is ``True``.
            if cleaned_data.get("risk_technical"):
                if not cleaned_data["risk_technical_description"]:
                    msg = (
                        f"This is required if "
                        f"'{Experiment.RISK_TECHNICAL_LABEL}' is true."
                    )
                    raise forms.ValidationError({"risk_technical_description": msg})
        return cleaned_data


class ExperimentReviewForm(ExperimentConstants, ChangeLogMixin, forms.ModelForm):
    # Required
    review_science = forms.BooleanField(
        required=False,
        label="Data Science Peer Review",
        help_text=Experiment.REVIEW_SCIENCE_HELP_TEXT,
    )
    review_engineering = forms.BooleanField(
        required=False,
        label="Engineering Allocated",
        help_text=Experiment.REVIEW_ENGINEERING_HELP_TEXT,
    )
    review_qa_requested = forms.BooleanField(
        required=False,
        label=mark_safe(
            f"QA <a href={settings.JIRA_URL} target='_blank'>" "Jira</a> Request Sent"
        ),
        help_text=Experiment.REVIEW_QA_REQUESTED_HELP_TEXT,
    )
    review_intent_to_ship = forms.BooleanField(
        required=False,
        label="Intent to Ship Email Sent",
        help_text=Experiment.REVIEW_INTENT_TO_SHIP_HELP_TEXT,
    )
    review_bugzilla = forms.BooleanField(
        required=False,
        label="Bugzilla Updated",
        help_text=Experiment.REVIEW_BUGZILLA_HELP_TEXT,
    )
    review_qa = forms.BooleanField(
        required=False, label="QA Sign-Off", help_text=Experiment.REVIEW_QA_HELP_TEXT
    )
    review_relman = forms.BooleanField(
        required=False,
        label="Release Management Sign-Off",
        help_text=Experiment.REVIEW_RELMAN_HELP_TEXT,
    )

    # Optional
    review_advisory = forms.BooleanField(
        required=False,
        label="Lightning Advisory (Optional)",
        help_text=Experiment.REVIEW_LIGHTNING_ADVISING_HELP_TEXT,
    )
    review_legal = forms.BooleanField(
        required=False,
        label="Legal Review",
        help_text=Experiment.REVIEW_GENERAL_HELP_TEXT,
    )
    review_ux = forms.BooleanField(
        required=False, label="UX Review", help_text=Experiment.REVIEW_GENERAL_HELP_TEXT
    )
    review_security = forms.BooleanField(
        required=False,
        label="Security Review",
        help_text=Experiment.REVIEW_GENERAL_HELP_TEXT,
    )
    review_vp = forms.BooleanField(
        required=False, label="VP Sign Off", help_text=Experiment.REVIEW_GENERAL_HELP_TEXT
    )
    review_data_steward = forms.BooleanField(
        required=False,
        label="Data Steward Review",
        help_text=Experiment.REVIEW_GENERAL_HELP_TEXT,
    )
    review_comms = forms.BooleanField(
        required=False,
        label="Mozilla Press/Comms",
        help_text=Experiment.REVIEW_GENERAL_HELP_TEXT,
    )
    review_impacted_teams = forms.BooleanField(
        required=False,
        label="Impacted Team(s) Signed-Off",
        help_text=Experiment.REVIEW_GENERAL_HELP_TEXT,
    )

    class Meta:
        model = Experiment
        fields = (
            # Required
            "review_science",
            "review_engineering",
            "review_qa_requested",
            "review_intent_to_ship",
            "review_bugzilla",
            "review_qa",
            "review_relman",
            # Optional
            "review_advisory",
            "review_legal",
            "review_ux",
            "review_security",
            "review_vp",
            "review_data_steward",
            "review_comms",
            "review_impacted_teams",
        )

    @property
    def required_reviews(self):
        return [self[r] for r in self.instance.get_all_required_reviews()]

    @property
    def optional_reviews(self):
        return [
            self[r]
            for r in list(
                sorted(
                    set(self.Meta.fields) - set(self.instance.get_all_required_reviews())
                )
            )
        ]

    @property
    def added_reviews(self):
        return [
            strip_tags(self.fields[field_name].label)
            for field_name in self.changed_data
            if self.cleaned_data[field_name]
        ]

    @property
    def removed_reviews(self):
        return [
            strip_tags(self.fields[field_name].label)
            for field_name in self.changed_data
            if not self.cleaned_data[field_name]
        ]

    def get_changelog_message(self):
        message = ""

        if self.added_reviews:
            message += "Added sign-offs: {reviews} ".format(
                reviews=", ".join(self.added_reviews)
            )

        if self.removed_reviews:
            message += "Removed sign-offs: {reviews} ".format(
                reviews=", ".join(self.removed_reviews)
            )

        return message

    def save(self, *args, **kwargs):
        experiment = super().save(*args, **kwargs)

        if self.changed_data:
            Notification.objects.create(
                user=self.request.user, message=self.get_changelog_message()
            )

        return experiment

    def clean(self):

        super(ExperimentReviewForm, self).clean()

        permissions = [
            (
                "review_qa",
                "experiments.can_check_QA_signoff",
                "You don't have permission to edit QA signoff fields",
            ),
            (
                "review_relman",
                "experiments.can_check_relman_signoff",
                "You don't have permission to edit Release " "Management signoff fields",
            ),
        ]

        # user cannot check or uncheck QA and relman
        # sign off boxes without permission
        for field_name, permission_name, error_message in permissions:
            if field_name in self.changed_data and not self.request.user.has_perm(
                permission_name
            ):
                self.changed_data.remove(field_name)
                self.cleaned_data.pop(field_name)
                messages.warning(self.request, error_message)

        return self.cleaned_data


class ExperimentStatusForm(ExperimentConstants, ChangeLogMixin, forms.ModelForm):

    attention = forms.CharField(required=False)

    class Meta:
        model = Experiment
        fields = ("status", "attention")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.old_status = self.instance.status

    @property
    def new_status(self):
        return self.cleaned_data["status"]

    def clean_status(self):
        expected_status = self.new_status in self.STATUS_TRANSITIONS[self.old_status]

        if self.old_status != self.new_status and not expected_status:
            raise forms.ValidationError(
                (
                    "You can not change an Experiment's status "
                    "from {old_status} to {new_status}"
                ).format(old_status=self.old_status, new_status=self.new_status)
            )

        return self.new_status

    def save(self, *args, **kwargs):
        experiment = super().save(*args, **kwargs)

        if (
            self.old_status == Experiment.STATUS_DRAFT
            and self.new_status == Experiment.STATUS_REVIEW
            and not experiment.bugzilla_id
        ):

            tasks.create_experiment_bug_task.delay(self.request.user.id, experiment.id)
            tasks.update_exp_id_to_ds_bug_task.delay(self.request.user.id, experiment.id)

        if (
            self.old_status == Experiment.STATUS_REVIEW
            and self.new_status == Experiment.STATUS_SHIP
            and experiment.bugzilla_id
            and experiment.should_use_normandy
        ):
            experiment.normandy_slug = experiment.generate_normandy_slug()
            experiment.save()

            tasks.update_experiment_bug_task.delay(self.request.user.id, experiment.id)

            tasks.update_ds_bug_task.delay(experiment.id)

        return experiment


class ExperimentArchiveForm(ExperimentConstants, ChangeLogMixin, forms.ModelForm):

    archived = forms.BooleanField(required=False)

    class Meta:
        model = Experiment
        fields = ("archived",)

    def clean_archived(self):
        return not self.instance.archived

    def get_changelog_message(self):
        message = "Archived Experiment"
        if not self.instance.archived:
            message = "Unarchived Experiment"
        return message

    def save(self, *args, **kwargs):
        experiment = Experiment.objects.get(id=self.instance.id)

        if not experiment.is_archivable:
            notification_msg = "This experiment cannot be archived in its current state!"
            Notification.objects.create(user=self.request.user, message=notification_msg)
            return experiment

        experiment = super().save(*args, **kwargs)
        tasks.update_bug_resolution_task.delay(self.request.user.id, experiment.id)
        return experiment


class ExperimentSubscribedForm(ExperimentConstants, forms.ModelForm):

    subscribed = forms.BooleanField(required=False)

    class Meta:
        model = Experiment
        fields = ()

    def __init__(self, request, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.initial["subscribed"] = self.instance.subscribers.filter(
            id=self.request.user.id
        ).exists()

    def clean_subscribed(self):
        return self.instance.subscribers.filter(id=self.request.user.id).exists()

    def save(self, *args, **kwargs):
        experiment = super().save(*args, **kwargs)

        if self.cleaned_data["subscribed"]:
            experiment.subscribers.remove(self.request.user)
        else:
            experiment.subscribers.add(self.request.user)

        return experiment


class ExperimentCommentForm(forms.ModelForm):
    created_by = forms.CharField(required=False)
    text = forms.CharField(required=True)
    section = forms.ChoiceField(required=True, choices=Experiment.SECTION_CHOICES)

    class Meta:
        model = ExperimentComment
        fields = ("experiment", "section", "created_by", "text")

    def __init__(self, request, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request

    def clean_created_by(self):
        return self.request.user


class NormandyIdForm(ChangeLogMixin, forms.ModelForm):
    normandy_id = forms.IntegerField(
        label="Primary Recipe ID",
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Primary Recipe ID"}
        ),
    )

    other_normandy_ids = forms.CharField(
        label="Other Recipe IDs",
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Other Recipe IDs (Optional)"}
        ),
        required=False,
    )

    def clean(self):
        cleaned_data = super().clean()

        if cleaned_data.get("normandy_id") in cleaned_data.get("other_normandy_ids", []):
            raise forms.ValidationError(
                {"other_normandy_ids": "Duplicate IDs are not accepted."}
            )

        return cleaned_data

    def clean_other_normandy_ids(self):
        if not self.cleaned_data["other_normandy_ids"].strip():
            return []

        try:
            return [
                int(i.strip()) for i in self.cleaned_data["other_normandy_ids"].split(",")
            ]
        except ValueError:
            raise forms.ValidationError(f"IDs must be numbers separated by commas.")

    class Meta:
        model = Experiment
        fields = ("normandy_id", "other_normandy_ids")
