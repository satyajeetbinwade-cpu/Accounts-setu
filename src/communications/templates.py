"""Message templates for follow-up communications.

Each template supports {{variable}} placeholders that get substituted
at send-time with values from the FollowUpRequest.context dict.
"""

from typing import Optional

# ---------------------------------------------------------------------------
# Built-in templates (also seeded into the DB on init)
# ---------------------------------------------------------------------------

BUILTIN_TEMPLATES = [
    {
        "name": "missing_invoice",
        "channel": "both",
        "subject_template": "Missing Invoice – {{period}} Reconciliation ({{recon_type}})",
        "body_template": (
            "Dear {{contact_name}},\n\n"
            "We are currently reconciling your {{recon_type}} records for the period "
            "{{period}} and noticed that the following invoice(s) are missing from "
            "the books:\n\n"
            "{{missing_details}}\n\n"
            "Total missing: {{missing_count}} invoice(s)\n\n"
            "Kindly provide the missing invoice(s) at your earliest convenience so "
            "we can complete the reconciliation accurately.\n\n"
            "If you have any questions, please reach out.\n\n"
            "Best regards,\n"
            "Accounts Team"
        ),
        "variables": '["contact_name", "period", "recon_type", "missing_count", "missing_details"]',
    },
    {
        "name": "missing_entries",
        "channel": "both",
        "subject_template": "Missing Entries – {{period}} Reconciliation ({{recon_type}})",
        "body_template": (
            "Dear {{contact_name}},\n\n"
            "During the reconciliation of your {{recon_type}} records for "
            "{{period}}, we identified some entries that need clarification "
            "or are missing from the portal/books:\n\n"
            "{{missing_details}}\n\n"
            "Please review and provide the necessary context or missing "
            "entries so we can proceed with the reconciliation.\n\n"
            "Best regards,\n"
            "Accounts Team"
        ),
        "variables": '["contact_name", "period", "recon_type", "missing_details"]',
    },
    {
        "name": "custom",
        "channel": "both",
        "subject_template": "{{subject}}",
        "body_template": "{{custom_message}}",
        "variables": '["subject", "custom_message"]',
    },
    {
        "name": "followup_reminder",
        "channel": "both",
        "subject_template": "Reminder: Outstanding Items – {{period}} ({{recon_type}})",
        "body_template": (
            "Dear {{contact_name}},\n\n"
            "This is a reminder regarding our previous communication about "
            "outstanding items for the {{period}} {{recon_type}} reconciliation.\n\n"
            "{{missing_details}}\n\n"
            "We would appreciate your prompt attention to this matter.\n\n"
            "Best regards,\n"
            "Accounts Team"
        ),
        "variables": '["contact_name", "period", "recon_type", "missing_details"]',
    },
]


def render_template(body_template: str, context: dict) -> str:
    """Render a template string by substituting {{variable}} placeholders.

    Unknown variables are left as-is (not silently dropped) so callers
    can detect typos.
    """
    result = body_template
    for key, value in context.items():
        placeholder = "{{" + key + "}}"
        if placeholder in result:
            result = result.replace(placeholder, str(value))
    return result


def render_subject(subject_template: Optional[str], context: dict) -> Optional[str]:
    """Render a subject template the same way."""
    if not subject_template:
        return None
    return render_template(subject_template, context)
