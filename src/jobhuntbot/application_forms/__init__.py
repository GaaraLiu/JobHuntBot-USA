"""Read-only application form detection and mapping APIs."""

from .base import ApplicationFormAdapter, FormContext
from .mapping import ApplicationFormMapper, FieldConceptMapper
from .models import (
    ApplicationField,
    ApplicationForm,
    ApplicationMappingPlan,
    FieldMappingPlan,
    FormSection,
)
from .registry import (
    SUPPORTED_APPLICATION_ATS,
    create_form_adapter,
    detect_application_ats,
    registered_application_ats,
)

__all__ = [
    "ApplicationField",
    "ApplicationForm",
    "ApplicationFormAdapter",
    "ApplicationFormMapper",
    "ApplicationMappingPlan",
    "FieldConceptMapper",
    "FieldMappingPlan",
    "FormContext",
    "FormSection",
    "SUPPORTED_APPLICATION_ATS",
    "create_form_adapter",
    "detect_application_ats",
    "registered_application_ats",
]
