"""C1 UI — Rules Workspace (hub) + Taxonomy Editor + Regulatory Rules Table.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt + C1's
design table §4):
- Rules Workspace is the hub: five rule categories (TDS section-rates,
  GST tolerance, suspense-ledger conventions, aging thresholds, plus the
  new Statutory Due Dates), inside the standard page frame.
- Every rule row has a scope switcher (segmented control) defaulting to
  "Firm-wide" with a "Client override" option; the selected scope uses the
  informational-accent token. The firm-wide view shows an "Overridden for
  N clients" badge.
- Rule edit: single-step, not propose-then-approve. A Manager+ editing and
  saving a rule IS both propose and approve. An edit creates a NEW
  effective-dated RuleVersion (forward-only).
- Impact confirmation: shown ONLY for high-impact/wide-blast-radius rule
  edits — an inline warning-amber banner requiring an explicit "Confirm
  and save" action. Not a modal; not shown for ordinary edits.
- Taxonomy Editor: rename is a safe label-only inline edit (no confirm);
  delete reuses Delete vs Deactivate (3.2) — Delete side only — with
  Disabled-with-inline-reason (3.4) when blocked.
- Regulatory Rules Table: ONE status chip per row (data-freshness signal),
  consolidating seed-warning + staleness.
- Statutory Due Dates: same table/list styling as the other categories.
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.clients import service as clients
from src.rules import service as rules
from src.ui.theme import (
    render_accent_pill,
    render_freshness_chip,
    render_inline_reason,
    render_view_history,
    render_warning_banner,
)

# Streamlit radio label for the three hub sections.
_HUB_SECTIONS = [
    "Rules Workspace",
    "Taxonomy Editor",
    "Regulatory Rules Table",
]

# The five rule categories (key -> label) rendered as a left-nav radio.
_CATEGORY_LABELS = {
    "tds_rates": "TDS Section Rates",
    "gst_tolerance": "GST Tolerance",
    "suspense": "Suspense Ledger Conventions",
    "aging": "Aging Thresholds",
    "statutory_due_dates": "Statutory Due Dates",
}


def render_rules_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "rules.view"):
        st.warning("You don't have access to Rules & Regulatory Configuration.")
        return

    can_manage = auth.has_permission(current_user, "rules.manage")

    st.subheader("Rules, Taxonomy & Regulatory Configuration")
    st.caption("The platform's editable rulebook.")

    section = st.radio(
        "Rules section", _HUB_SECTIONS, horizontal=True, label_visibility="collapsed",
    )

    if section == "Rules Workspace":
        _render_rules_workspace(current_user, can_manage)
    elif section == "Taxonomy Editor":
        _render_taxonomy_editor(current_user, can_manage)
    else:
        _render_regulatory_table(current_user, can_manage)


# ---------------------------------------------------------------------------
# Rules Workspace
# ---------------------------------------------------------------------------


def _render_rules_workspace(current_user: dict[str, Any], can_manage: bool) -> None:
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)

    category_key = st.radio(
        "Rule category", list(_CATEGORY_LABELS.keys()), format_func=_CATEGORY_LABELS.get,
        horizontal=True, label_visibility="collapsed",
    )

    if category_key == "statutory_due_dates":
        _render_statutory_due_dates(current_user, can_manage)
    else:
        _render_rule_category(current_user, can_manage, category_key)

    st.markdown('</div>', unsafe_allow_html=True)


def _render_rule_category(current_user: dict[str, Any], can_manage: bool, category_key: str) -> None:
    category_label = _CATEGORY_LABELS[category_key]
    st.markdown(f'<span class="setu-card-title">{category_label}</span>', unsafe_allow_html=True)
    st.caption("Each rule is editable firm-wide, or overridden per client. Edits are forward-only.")

    all_rules = rules.list_rules(category_key=category_key)
    if not all_rules:
        st.info("No rules defined for this category yet.")
        return

    for r in all_rules:
        _render_rule_row(current_user, can_manage, r)


def _render_rule_row(current_user: dict[str, Any], can_manage: bool, rule: dict[str, Any]) -> None:
    rule_key = rule["key"]
    st.markdown('<hr style="margin:10px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    # Row header: label + override badge (firm-wide view only).
    header_l, header_r = st.columns([3, 2])
    with header_l:
        st.markdown(f"**{rule['label']}**")
        if rule.get("description"):
            st.caption(rule["description"])
        # F4 — the single reusable "View History" trigger, inline next to the
        # rule header. Read-only; shows the rule's own value-change history.
        render_view_history(
            "rule", f"{rule['rule_id']}:firm", current_user=current_user,
            label="View History",
        )
    with header_r:
        if rule["override_count"] > 0:
            render_accent_pill(f"Overridden for {rule['override_count']} client(s)")

    # Scope switcher — segmented control, informational-accent for selected.
    scope = st.segmented_control(
        f"Scope for {rule_key}", ["firm", "client"],
        format_func=lambda s: "Firm-wide" if s == "firm" else "Client override",
        default="firm", key=f"scope_{rule_key}",
        disabled=not can_manage,
    )
    if scope is None:
        scope = "firm"

    client_id: Optional[int] = None
    selected_client_name: Optional[str] = None

    if scope == "client":
        client_options = clients.list_clients(include_inactive=False)
        if not client_options:
            st.info("No active clients to override.")
            return
        client_name = st.selectbox(
            "Client", [c["legal_name"] for c in client_options], key=f"clientsel_{rule_key}",
        )
        selected = next((c for c in client_options if c["legal_name"] == client_name), None)
        client_id = selected["client_id"] if selected else None
        selected_client_name = client_name

    # Current effective value for the selected scope.
    if scope == "firm":
        current_value = rule.get("firm_value")
        effective_from = rule.get("firm_effective_from")
    else:
        versions = rules.rule_versions(rule_key, scope="client", client_id=client_id)
        current_value = versions[0]["value"] if versions else None
        effective_from = versions[0]["effective_from"] if versions else None

    value_type = rule["value_type"]
    unit = rule.get("unit") or ""

    edit_col, save_col = st.columns([4, 1])

    with edit_col:
        new_value = _render_value_input(rule, current_value, value_type, unit, can_manage)

    with save_col:
        st.write("")
        if effective_from:
            st.caption(f"Eff. {effective_from}")

    # Impact confirmation — shown ONLY for high-impact rule edits.
    high_impact = bool(rule.get("high_impact"))

    if not can_manage:
        return

    # F4 retrofit — a rule VALUE is a high-sensitivity field ("rule
    # thresholds" per the PRD), so a reason is mandatory at BOTH layers.
    # Reuses the Reason-capture-before-save component (3.3) exactly.
    reason = st.text_input(
        "Reason for this rule change (required before saving)",
        key=f"rule_reason_{rule_key}_{scope}_{client_id}",
    )
    reason_ok = bool(reason.strip())
    if not reason_ok:
        render_inline_reason("A reason is required before this rule change can be saved.")

    if high_impact and scope == "firm":
        _render_high_impact_save(rule, scope, client_id, new_value, current_user, current_value, reason, reason_ok)
    else:
        if st.button(
            "Save", key=f"save_{rule_key}_{scope}_{client_id}",
            disabled=not reason_ok, type="primary",
        ):
            _commit_rule_edit(rule, scope, client_id, new_value, current_user, current_value, reason)


def _render_value_input(
    rule: dict[str, Any], current_value: Optional[str], value_type: str, unit: str, can_manage: bool,
) -> str:
    key = f"val_{rule['key']}"
    if value_type == "boolean":
        return "true" if st.checkbox(
            "Enabled", value=(current_value == "true"), key=key, disabled=not can_manage,
        ) else "false"
    if value_type == "number":
        return st.text_input(
            f"Value ({unit})" if unit else "Value",
            value=current_value or "", key=key, disabled=not can_manage,
        )
    if value_type == "date":
        return st.text_input(
            "Value (YYYY-MM-DD)", value=current_value or "", key=key, disabled=not can_manage,
        )
    return st.text_input("Value", value=current_value or "", key=key, disabled=not can_manage)


def _render_high_impact_save(
    rule: dict[str, Any], scope: str, client_id: Optional[int], new_value: str,
    current_user: dict[str, Any], current_value: Optional[str],
    reason: str = "", reason_ok: bool = True,
) -> None:
    """Impact confirmation: an inline warning-amber banner requiring an
    explicit "Confirm and save" — shown ONLY for high-impact rule edits,
    never for routine ones."""
    render_warning_banner(
        "<strong>Firm-wide impact</strong> \u2014 this change affects every client "
        "that has not overridden this rule. Review before saving."
    )
    confirm = st.button(
        "Confirm and save", key=f"confirm_{rule['key']}_{scope}",
        type="primary", disabled=not reason_ok,
    )
    if confirm:
        _commit_rule_edit(rule, scope, client_id, new_value, current_user, current_value, reason)


def _commit_rule_edit(
    rule: dict[str, Any], scope: str, client_id: Optional[int], new_value: str,
    current_user: dict[str, Any], current_value: Optional[str], reason: str = "",
) -> None:
    if new_value == (current_value or ""):
        st.info("No change \u2014 value is unchanged.")
        return
    try:
        rules.edit_rule_value(
            rule["key"], new_value, scope=scope, client_id=client_id,
            actor=current_user["username"], reason=reason,
        )
        st.success(f"Rule '{rule['label']}' updated \u2014 live immediately.")
        st.rerun()
    except rules.RulesError as exc:
        st.error(str(exc))


# ---------------------------------------------------------------------------
# Statutory Due Dates (new category for C2)
# ---------------------------------------------------------------------------


def _render_statutory_due_dates(current_user: dict[str, Any], can_manage: bool) -> None:
    st.markdown('<span class="setu-card-title">Statutory Due Dates</span>', unsafe_allow_html=True)
    st.caption("Filing due dates \u2014 read by C2's filing calendar (see the Filing tab).")

    due_dates = rules.list_statutory_due_dates()
    if not due_dates:
        st.info("No statutory due dates defined yet.")
        return

    header = st.columns([3, 2, 2, 2])
    for col, label in zip(header, ["Obligation", "Period", "Due", "Grace (days)"]):
        col.markdown(
            f'<span style="color:var(--setu-text-secondary);font-size:12px;font-weight:600;">{label}</span>',
            unsafe_allow_html=True,
        )
    st.markdown('<hr style="margin:4px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    for d in due_dates:
        row = st.columns([3, 2, 2, 2])
        with row[0]:
            st.write(d["obligation"])
        with row[1]:
            st.write(d["period"])
        with row[2]:
            due = ""
            if d.get("due_day"):
                due = f"Day {d['due_day']}"
            elif d.get("due_month"):
                due = f"Month {d['due_month']}"
            st.write(due or "\u2014")
        with row[3]:
            st.write(str(d["grace_days"]))
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Taxonomy Editor
# ---------------------------------------------------------------------------


def _render_taxonomy_editor(current_user: dict[str, Any], can_manage: bool) -> None:
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Taxonomy Editor</span>', unsafe_allow_html=True)
    st.caption(
        "Pre-seeded with the Flagged Item vocabulary from the Information "
        "Architecture (Step 3). Entries rename or delete only \u2014 no deactivate."
    )

    categories = rules.taxonomy_categories()
    if not categories:
        st.info("No taxonomy entries.")
        return

    category = st.radio("Category", categories, horizontal=True, label_visibility="collapsed")

    entries = rules.list_taxonomy(category=category)
    for e in entries:
        st.markdown('<hr style="margin:10px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
        label_col, rename_col, del_col = st.columns([3, 4, 2])
        with label_col:
            st.markdown(f"**{e['label']}**")
            st.caption(e["code"])
        with rename_col:
            new_label = st.text_input(
                "Rename label", value=e["label"], key=f"taxlabel_{e['entry_id']}",
                label_visibility="collapsed", disabled=not can_manage,
            )
            if can_manage and new_label != e["label"]:
                if st.button("Rename", key=f"taxrename_{e['entry_id']}"):
                    try:
                        rules.rename_taxonomy_entry(e["entry_id"], new_label, actor=current_user["username"])
                        st.success("Label renamed.")
                        st.rerun()
                    except rules.RulesError as exc:
                        st.error(str(exc))
        with del_col:
            allowed, reason = rules.can_delete_taxonomy_entry(e["entry_id"])
            if st.button("Delete", key=f"taxdel_{e['entry_id']}", disabled=not can_manage or not allowed):
                try:
                    rules.delete_taxonomy_entry(e["entry_id"], actor=current_user["username"])
                    st.success("Entry deleted.")
                    st.rerun()
                except rules.RulesError as exc:
                    st.error(str(exc))
            if not allowed and reason:
                render_inline_reason(reason)

    if can_manage:
        st.markdown('<hr style="margin:12px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
        with st.expander("+ Add taxonomy entry"):
            with st.form("tax_add_form", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                with c1:
                    new_code = st.text_input("Code *")
                with c2:
                    new_label = st.text_input("Label *")
                with c3:
                    new_category = st.selectbox("Category", categories)
                submitted = st.form_submit_button("Add entry", type="primary")
            if submitted:
                try:
                    rules.add_taxonomy_entry(new_category, new_code, new_label, actor=current_user["username"])
                    st.success("Entry added.")
                    st.rerun()
                except rules.RulesError as exc:
                    st.error(str(exc))

    st.markdown('</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Regulatory Rules Table
# ---------------------------------------------------------------------------


def _render_regulatory_table(current_user: dict[str, Any], can_manage: bool) -> None:
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Regulatory Rules Table</span>', unsafe_allow_html=True)
    st.caption(
        "Pre-seeded with known current rates as of a stated date. "
        "Verify before relying on for filings."
    )

    regs = rules.list_regulatory_rules()
    if not regs:
        st.info("No regulatory rules.")
        return

    header = st.columns([2, 1, 2, 2, 2])
    for col, label in zip(header, ["Domain", "Section", "Name", "Rate / rule", "Status"]):
        col.markdown(
            f'<span style="color:var(--setu-text-secondary);font-size:12px;font-weight:600;">{label}</span>',
            unsafe_allow_html=True,
        )
    st.markdown('<hr style="margin:4px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    for reg in regs:
        row = st.columns([2, 1, 2, 2, 2])
        with row[0]:
            st.write(reg["domain"])
        with row[1]:
            st.write(reg["section"] or "\u2014")
        with row[2]:
            st.write(reg["name"])
        with row[3]:
            st.write(reg["rate_or_rule"])
        with row[4]:
            _render_regulatory_status(reg)

        if can_manage:
            with st.expander("Edit / review", expanded=False):
                c1, c2 = st.columns(2)
                with c1:
                    new_rate = st.text_input(
                        "Rate / rule", value=reg["rate_or_rule"], key=f"regrate_{reg['reg_rule_id']}",
                    )
                    new_eff = st.text_input(
                        "Effective from (YYYY-MM-DD)", value=reg["effective_from"], key=f"regeff_{reg['reg_rule_id']}",
                    )
                with c2:
                    st.caption(f"Seed data as of {reg['seed_as_of']}")
                    if st.button("Mark reviewed", key=f"regreviewed_{reg['reg_rule_id']}"):
                        rules.mark_regulatory_rule_reviewed(reg["reg_rule_id"], actor=current_user["username"])
                        st.success("Marked reviewed.")
                        st.rerun()
                if st.button("Save changes", key=f"regsave_{reg['reg_rule_id']}"):
                    try:
                        rules.update_regulatory_rule(
                            reg["reg_rule_id"], rate_or_rule=new_rate, effective_from=new_eff,
                            actor=current_user["username"],
                        )
                        st.success("Regulatory rule updated.")
                        st.rerun()
                    except rules.RulesError as exc:
                        st.error(str(exc))

        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)


def _render_regulatory_status(reg: dict[str, Any]) -> None:
    status = reg["status"]
    if status == "seed":
        render_freshness_chip("seed", label="Seed data \u2014 unverified")
    elif status == "reviewed":
        reviewed = reg["last_reviewed_at"].split("T")[0] if reg.get("last_reviewed_at") else ""
        render_freshness_chip("reviewed", label=f"Reviewed {reviewed}")
    else:
        render_freshness_chip("stale", label=f"Stale \u2014 not reviewed in {rules.STALE_AFTER_MONTHS} months")