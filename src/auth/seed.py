"""First-run seed data for the F1 auth layer.

Per the F1 build prompt: roles are seeded data (Partner, Manager, Senior
Accountant, Article-Trainee, Admin, End-Client) — NOT a hardcoded enum —
and the permission list is a first-pass, "expand-as-built" set, not
final. This file only INSERTs when a table is empty, so re-running the
app never resets Admin-made changes (new roles, edited grants, etc.).

Permission naming: "<module>.<action>", action-level rather than
screen-level, per the prompt's data-model requirement.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.auth import security

SEED_ROLES: list[tuple[str, str]] = [
    ("Admin", "Manages users, roles, permissions, and platform configuration."),
    ("Partner", "Firm-wide oversight; approves high-impact actions."),
    ("Manager", "Reviews and approves reconciliation work; manages team assignments."),
    ("Senior Accountant", "Performs reconciliation review and execution."),
    ("Article-Trainee", "Prepares reconciliation runs under supervision; limited review rights."),
    ("End-Client", "Read-only access to their own reconciliation results (shared login)."),
]

# (code, module, description)
SEED_PERMISSIONS: list[tuple[str, str, str]] = [
    # --- F1 (auth) module actions --------------------------------------
    ("auth.users.manage", "auth", "Create, edit, deactivate users; assign roles."),
    ("auth.roles.manage", "auth", "Create, edit, deactivate roles; assign role permissions."),
    ("auth.permissions.view", "auth", "View the Permission Management (who can do X) screen."),
    ("auth.team.manage", "auth", "Assign staff to clients (per-client team assignment)."),
    ("auth.security_events.view", "auth", "View the security event log."),
    ("auth.overrides.manage", "auth", "Grant or revoke per-user permission overrides."),
    # --- Recon module actions (existing Review/Run/Compare/Config/Export tabs) ---
    ("recon.results.view", "recon", "View reconciliation results."),
    ("recon.results.review", "recon", "Mark or clear the review status of a result."),
    ("recon.results.bulk_review", "recon", "Bulk mark filtered results as reviewed."),
    ("recon.run.execute", "recon", "Execute a reconciliation run."),
    ("recon.config.view", "recon", "View the matching rules configuration."),
    ("recon.export.generate", "recon", "Generate and download an export workbook."),
    # --- F2 (clients) module actions -----------------------------------
    ("clients.profile.view", "clients", "View the Client Roster and client profiles."),
    ("clients.profile.manage", "clients", "Create/edit clients, contacts, chart of accounts, historical snapshots."),
    ("clients.branch.manage", "clients", "Add/edit GSTIN branches and their status; deactivate a branch."),
    ("clients.branch.delete", "clients", "Hard-delete a GSTIN branch (blocked while open recon work references it)."),
    ("clients.gstin_pan.edit", "clients", "Edit a client's GSTIN or PAN (Manager-level or above, reason required)."),
    # --- C3 (settings) module actions ----------------------------------
    ("settings.view", "settings", "View the System Settings hub."),
    ("settings.manage", "settings", "Edit firm profile, notifications, connections, onboarding defaults, retention."),
    ("settings.notifications.manage", "settings", "Set platform notification defaults and firm-mandatory flags."),
    ("settings.connections.manage", "settings", "Add/remove credential & API connections (status only; secrets go to C4)."),
    # --- C1 (rules) module actions ------------------------------------
    ("rules.view", "rules", "View the Rules Workspace, Taxonomy Editor, and Regulatory Rules Table."),
    ("rules.manage", "rules", "Edit rules, taxonomy entries, and regulatory rates (Manager-level or above)."),
    # --- C5 (AI instruction library) module actions -------------------
    ("c5.view", "c5", "View the AI Instruction & Knowledge Library and its approval queue."),
    ("c5.manage", "c5", "Create/edit/deactivate instructions and resolve proposed instructions."),
    ("c5.propose", "c5", "Submit a proposed instruction from an AI review screen (Manager-level or above)."),
    # --- C4 (security & credential vault) module actions --------------
    ("vault.view", "vault", "View the Security Overview screen (event summary, Backup/DR, open DPDP requests)."),
    ("vault.credentials.manage", "vault", "Add, rotate, and deactivate encrypted credentials. Never exposes raw secret values, even to Admin."),
    ("vault.dpdp.submit", "vault", "Submit a DPDP Act deletion request (Admin/Partner)."),
    ("vault.dpdp.approve", "vault", "Approve or reject a pending DPDP deletion request (Partner-only)."),
    # --- F3 (documents) module actions ---------------------------------
    ("documents.view", "documents", "View the scoped document vault, Unified Review Queue, and document detail."),
    ("documents.upload", "documents", "Upload new documents / new versions into the vault."),
    ("documents.reassign", "documents", "Reassign a misfiled document to the correct client (reason required)."),
    ("documents.review_queue.resolve", "documents", "Resolve Unified Review Queue entries (couldn't classify / couldn't map)."),
    ("documents.delete", "documents", "Soft-delete and restore documents (Partner/Manager only)."),
    # --- F3-AI (smart document ingestion) module actions ---------------
    ("ingestion_ai.upload", "ingestion_ai", "Upload a raw source file for AI-assisted column mapping."),
    ("ingestion_ai.review", "ingestion_ai", "See the full mapping-review screen (Senior Accountant and above)."),
    ("ingestion_ai.confirm", "ingestion_ai", "Confirm a column mapping and optionally trust it for reuse."),
    ("ingestion_ai.profiles.manage", "ingestion_ai", "View/revoke Mapping Profiles and copy a profile across clients (Admin)."),
    ("ingestion_ai.thresholds.manage", "ingestion_ai", "Edit the confidence-threshold defaults (Admin)."),
    ("ingestion_ai.llm.manage", "ingestion_ai", "Configure the AI ingestion model, provider endpoint, and API credential (Admin)."),
    # --- F5 (data integrity & validation) module actions ---------------
    ("f5.view", "f5", "View the Data Integrity Queue, sync health, and reconciliation-of-the-reconciliation tab."),
    ("f5.override.minor", "f5", "Override a minor/cosmetic structural validation block (Senior Accountant or above)."),
    ("f5.override.gstin_pan", "f5", "Override a GSTIN/PAN-level structural validation block (Manager or above)."),
    ("f5.control_total.enter", "f5", "Enter a manual control total for the completeness check."),
    # --- C2 (portal connect & filing) module actions -------------------
    ("filing.view", "filing", "View the filing calendar, filings, and connection/source toggles."),
    ("filing.prepare", "filing", "Prepare a filing package (manual upload) for a client."),
    ("filing.send", "filing", "Send a filing (Manager or above enforced at the service layer regardless of this grant)."),
    ("filing.acknowledge", "filing", "Capture a portal acknowledgement and mark a filing complete."),
    # --- Module 2 (reconciliation engine) module actions ----------------
    ("module2.view", "module2", "View Module 2's own screen and the exception detail screen."),
    ("module2.exceptions.resolve", "module2", "Accept / Reject / Escalate a reconciliation exception."),
    ("module2.ims.recommend", "module2", "View and act on the IMS Accept/Reject/Pending recommendation (2A)."),
    ("module2.eligible_credit.view", "module2", "View the live eligible-credit figure (2A)."),
    # --- Module 8 (action center) module actions -----------------------
    ("action_center.view", "action_center", "View the unified Action Center queue."),
    ("action_center.assign", "action_center", "Assign/reassign a queue item and set its due date."),
    ("action_center.batch", "action_center", "Batch-approve eligible (non-materiality-gated) queue items."),
    # --- F3-B (invoice extraction & digitalization) module actions -----
    ("invoice_extract.upload", "invoice_extract", "Upload an invoice for AI extraction (any of the four accepted formats)."),
    ("invoice_extract.review", "invoice_extract", "See the review screen and resolve sub-threshold / not-present fields."),
    ("invoice_extract.export", "invoice_extract", "Generate a Tally-ready export batch from Confirmed uploads."),
    ("invoice_extract.models.manage", "invoice_extract", "Edit the two invoice-extraction touchpoint model assignments (Admin)."),
    ("invoice_extract.threshold.manage", "invoice_extract", "Edit the invoice-extraction confidence threshold (Admin)."),
    # --- F6 (source file ingestion & format registry) module actions ---
    ("f6.view", "f6", "View the Format Registry, mapping-review screens, and ingestion run results."),
    ("f6.upload", "f6", "Upload a source file for identification and ingestion (Path A/B/C)."),
    ("f6.mapping.confirm", "f6", "Confirm a Path B mapping proposal and promote it (client-scoped by default)."),
    ("f6.format.promote_firmwide", "f6",
     "Promote a confirmed mapping firm-wide, changing parsing for every client at once "
     "(Partner or above — the highest-consequence action in this module)."),
    ("f6.format.quarantine", "f6", "Quarantine a format version (Manager or above)."),
]

# role name -> list of permission codes granted by default.
DEFAULT_ROLE_GRANTS: dict[str, list[str]] = {
    "Admin": [code for code, _, _ in SEED_PERMISSIONS],  # everything
    "Partner": [
        "auth.permissions.view",
        "auth.security_events.view",
        "auth.team.manage",
        "recon.results.view",
        "recon.results.review",
        "recon.results.bulk_review",
        "recon.run.execute",
        "recon.config.view",
        "recon.export.generate",
        "clients.profile.view",
        "clients.profile.manage",
        "clients.branch.manage",
        "clients.branch.delete",
        "clients.gstin_pan.edit",
        "settings.view",
        "settings.manage",
        "settings.notifications.manage",
        "settings.connections.manage",
        "rules.view",
        "rules.manage",
        "c5.view",
        "c5.manage",
        "c5.propose",
        "vault.view",
        "vault.credentials.manage",
        "vault.dpdp.submit",
        "vault.dpdp.approve",
        "documents.view",
        "documents.upload",
        "documents.reassign",
        "documents.review_queue.resolve",
        "documents.delete",
        "ingestion_ai.upload",
        "ingestion_ai.review",
        "ingestion_ai.confirm",
        "ingestion_ai.profiles.manage",
        "ingestion_ai.thresholds.manage",
        "ingestion_ai.llm.manage",
        "f5.view",
        "f5.override.minor",
        "f5.override.gstin_pan",
        "f5.control_total.enter",
        "filing.view",
        "filing.prepare",
        "filing.send",
        "filing.acknowledge",
        "module2.view",
        "module2.exceptions.resolve",
        "module2.ims.recommend",
        "module2.eligible_credit.view",
        "action_center.view",
        "action_center.assign",
        "action_center.batch",
        "invoice_extract.upload",
        "invoice_extract.review",
        "invoice_extract.export",
        "invoice_extract.models.manage",
        "invoice_extract.threshold.manage",
        "f6.view",
        "f6.upload",
        "f6.mapping.confirm",
        "f6.format.promote_firmwide",
        "f6.format.quarantine",
    ],
    "Manager": [
        "auth.permissions.view",
        "auth.team.manage",
        "recon.results.view",
        "recon.results.review",
        "recon.results.bulk_review",
        "recon.run.execute",
        "recon.config.view",
        "recon.export.generate",
        "clients.profile.view",
        "clients.profile.manage",
        "clients.branch.manage",
        "clients.branch.delete",
        "clients.gstin_pan.edit",
        "settings.view",
        "settings.manage",
        "rules.view",
        "rules.manage",
        "c5.view",
        "c5.manage",
        "c5.propose",
        "documents.view",
        "documents.upload",
        "documents.reassign",
        "documents.review_queue.resolve",
        "documents.delete",
        "ingestion_ai.upload",
        "ingestion_ai.review",
        "ingestion_ai.confirm",
        "f5.view",
        "f5.override.minor",
        "f5.override.gstin_pan",
        "f5.control_total.enter",
        "filing.view",
        "filing.prepare",
        "filing.send",
        "filing.acknowledge",
        "module2.view",
        "module2.exceptions.resolve",
        "module2.ims.recommend",
        "module2.eligible_credit.view",
        "action_center.view",
        "action_center.assign",
        "action_center.batch",
        "invoice_extract.upload",
        "invoice_extract.review",
        "invoice_extract.export",
        "f6.view",
        "f6.upload",
        "f6.mapping.confirm",
        "f6.format.quarantine",
    ],
    "Senior Accountant": [
        "recon.results.view",
        "recon.results.review",
        "recon.run.execute",
        "recon.config.view",
        "recon.export.generate",
        "clients.profile.view",
        "documents.view",
        "documents.upload",
        "ingestion_ai.upload",
        "ingestion_ai.review",
        "ingestion_ai.confirm",
        "f5.view",
        "f5.override.minor",
        "f5.control_total.enter",
        "filing.view",
        "filing.prepare",
        "module2.view",
        "module2.eligible_credit.view",
        "action_center.view",
        "action_center.assign",
        "invoice_extract.upload",
        "invoice_extract.review",
        "invoice_extract.export",
        "f6.view",
        "f6.upload",
    ],
    "Article-Trainee": [
        "recon.results.view",
        "recon.run.execute",
        "recon.config.view",
        "clients.profile.view",
        "documents.view",
        "documents.upload",
        "ingestion_ai.upload",
        "f5.view",
        "filing.view",
        "filing.prepare",
        "module2.view",
        "module2.eligible_credit.view",
        "action_center.view",
        "invoice_extract.upload",
        "f6.view",
        "f6.upload",
    ],
    "End-Client": [
        "recon.results.view",
        "documents.view",
        "documents.upload",
    ],
}

# Seed the very first Admin user so there's always a way in. Change this
# password immediately after first login in a real deployment.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "ChangeMe123"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_seed(conn: sqlite3.Connection) -> None:
    """Populate roles/permissions/grants/first-admin only if tables are empty."""
    _seed_roles(conn)
    _seed_permissions(conn)
    _seed_role_permissions(conn)
    _seed_first_admin(conn)
    _sync_new_permissions(conn)
    conn.commit()


def _sync_new_permissions(conn: sqlite3.Connection) -> None:
    """Add any SEED_PERMISSIONS codes introduced by a later module (e.g.
    F2's clients.* actions) to an already-seeded DB, plus their default
    role grants \u2014 without touching any Admin-made changes to existing
    permissions/grants. Unlike _seed_permissions/_seed_role_permissions
    (which only run on a completely empty table), this always runs and is
    additive-only."""
    now = _now()
    existing_codes = {row[0] for row in conn.execute("SELECT code FROM permissions")}
    for code, module, description in SEED_PERMISSIONS:
        if code not in existing_codes:
            conn.execute(
                "INSERT INTO permissions (code, module, description, created_at) VALUES (?, ?, ?, ?)",
                (code, module, description, now),
            )

    roles = {row[0]: row[1] for row in conn.execute("SELECT name, role_id FROM roles")}
    perms = {row[0]: row[1] for row in conn.execute("SELECT code, permission_id FROM permissions")}
    for role_name, codes in DEFAULT_ROLE_GRANTS.items():
        role_id = roles.get(role_name)
        if role_id is None:
            continue
        for code in codes:
            if code in existing_codes:
                continue  # only auto-grant brand-new permissions, not existing ones
            perm_id = perms.get(code)
            if perm_id is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?, ?)",
                (role_id, perm_id),
            )


def _seed_roles(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    if count:
        return
    now = _now()
    for name, description in SEED_ROLES:
        conn.execute(
            "INSERT INTO roles (name, description, is_system, is_active, created_at) "
            "VALUES (?, ?, 1, 1, ?)",
            (name, description, now),
        )


def _seed_permissions(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
    if count:
        return
    now = _now()
    for code, module, description in SEED_PERMISSIONS:
        conn.execute(
            "INSERT INTO permissions (code, module, description, created_at) VALUES (?, ?, ?, ?)",
            (code, module, description, now),
        )


def _seed_role_permissions(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM role_permissions").fetchone()[0]
    if count:
        return
    roles = {row[0]: row[1] for row in conn.execute("SELECT name, role_id FROM roles")}
    perms = {row[0]: row[1] for row in conn.execute("SELECT code, permission_id FROM permissions")}
    for role_name, codes in DEFAULT_ROLE_GRANTS.items():
        role_id = roles.get(role_name)
        if role_id is None:
            continue
        for code in codes:
            perm_id = perms.get(code)
            if perm_id is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?, ?)",
                (role_id, perm_id),
            )


def _seed_first_admin(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count:
        return
    admin_role = conn.execute("SELECT role_id FROM roles WHERE name = 'Admin'").fetchone()
    if admin_role is None:
        return
    salt, pw_hash = security.hash_password(DEFAULT_ADMIN_PASSWORD)
    conn.execute(
        """
        INSERT INTO users
            (username, display_name, email, password_salt, password_hash,
             role_id, is_active, is_end_client, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?)
        """,
        (
            DEFAULT_ADMIN_USERNAME, "Default Admin", None, salt, pw_hash,
            admin_role[0], _now(),
        ),
    )
