"""portal_schema

Add all portal tables and extend existing tables with new columns.
This migration is purely additive — no existing columns are removed or renamed.

Revision ID: a1b2c3d4e5f6
Revises: 3216c592b58f
Create Date: 2026-05-16 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "3216c592b58f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # NEW ENUM TYPES (PostgreSQL only — SQLite uses VARCHAR natively)      #
    # ------------------------------------------------------------------ #
    is_pg = op.get_bind().dialect.name == "postgresql"
    if is_pg:
        # Extend existing type (attendancestatus was created in initial_schema)
        op.execute("ALTER TYPE attendancestatus ADD VALUE IF NOT EXISTS 'excused'")

        def _create_type(name: str, values: str) -> None:
            """Create a PG enum type idempotently (skip if already exists)."""
            op.execute(
                f"DO $$ BEGIN "
                f"CREATE TYPE {name} AS ENUM ({values}); "
                f"EXCEPTION WHEN duplicate_object THEN null; "
                f"END $$"
            )

        # Only create types that are referenced in ALTER TABLE (add_column) below.
        # Types used exclusively in CREATE TABLE are created automatically by SQLAlchemy.
        _create_type("discordverificationstatus", "'verified', 'mismatch', 'no_data', 'pending'")
        _create_type("deliverymode",              "'online', 'in_person', 'hybrid'")
        _create_type("enrollmentstatus",          "'active', 'trial', 'paused', 'withdrawn'")

    # ------------------------------------------------------------------ #
    # NEW BASE TABLES (must exist before FK references in ALTER TABLE)   #
    # ------------------------------------------------------------------ #

    # subjects
    op.create_table(
        "subjects",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="itemstatus"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # programs (referenced by class_groups.program_id FK below)
    op.create_table(
        "programs",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("subject_id", sa.Integer(), sa.ForeignKey("subjects.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("year_level", sa.String(50), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="itemstatus"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_programs_subject_id", "programs", ["subject_id"])

    # ------------------------------------------------------------------ #
    # MODIFY EXISTING TABLES (additive only)                              #
    # ------------------------------------------------------------------ #

    # attendance_records — add verification fields
    op.add_column("attendance_records", sa.Column(
        "tutor_marked_status",
        # attendancestatus already exists from initial_schema — do NOT re-create it
        sa.Enum("present", "late", "left_early", "absent", "excused", "unknown",
                name="attendancestatus", create_type=False),
        nullable=True,
    ))
    op.add_column("attendance_records", sa.Column(
        "tutor_marked_at", sa.DateTime(timezone=True), nullable=True
    ))
    op.add_column("attendance_records", sa.Column(
        "discord_verification_status",
        # Created above with DO block — do NOT let SQLAlchemy re-create it
        sa.Enum("verified", "mismatch", "no_data", "pending",
                name="discordverificationstatus", create_type=False),
        nullable=False,
        server_default="pending",
    ))
    op.add_column("attendance_records", sa.Column(
        "discord_verified_minutes", sa.Integer(), nullable=True
    ))

    # class_groups — add program link + portal fields
    op.add_column("class_groups", sa.Column(
        "program_id", sa.Integer(), sa.ForeignKey("programs.id"), nullable=True
    ))
    op.create_index("ix_class_groups_program_id", "class_groups", ["program_id"])
    op.add_column("class_groups", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("class_groups", sa.Column(
        "delivery_mode",
        # Created above with DO block — do NOT let SQLAlchemy re-create it
        sa.Enum("online", "in_person", "hybrid", name="deliverymode", create_type=False),
        nullable=False,
        server_default="online",
    ))
    op.add_column("class_groups", sa.Column(
        "default_duration_minutes", sa.Integer(), nullable=True
    ))

    # class_group_members — add enrollment status
    op.add_column("class_group_members", sa.Column(
        "enrollment_status",
        # Created above with DO block — do NOT let SQLAlchemy re-create it
        sa.Enum("active", "trial", "paused", "withdrawn",
                name="enrollmentstatus", create_type=False),
        nullable=False,
        server_default="active",
    ))
    op.add_column("class_group_members", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("class_group_members", sa.Column("end_date", sa.Date(), nullable=True))

    # ------------------------------------------------------------------ #
    # NEW TABLES                                                          #
    # ------------------------------------------------------------------ #

    # parent_profiles
    op.create_table(
        "parent_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_parent_profiles_email", "parent_profiles", ["email"], unique=True)

    # portal_credentials
    op.create_table(
        "portal_credentials",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "parent_profile_id",
            sa.Integer(),
            sa.ForeignKey("parent_profiles.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("invite_token", sa.String(255), nullable=True, unique=True),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invite_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_portal_credentials_invite_token", "portal_credentials", ["invite_token"])

    # student_parent_links
    op.create_table(
        "student_parent_links",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("student_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "parent_profile_id",
            sa.Integer(),
            sa.ForeignKey("parent_profiles.id"),
            nullable=False,
        ),
        sa.Column(
            "relationship",
            sa.Enum("mother", "father", "guardian", "other", name="parentrelationship"),
            nullable=False,
            server_default="guardian",
        ),
        sa.Column("is_primary_contact", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_student_parent_links_student", "student_parent_links", ["student_user_id"])
    op.create_index("ix_student_parent_links_parent", "student_parent_links", ["parent_profile_id"])

    # student_profiles
    op.create_table(
        "student_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True
        ),
        sa.Column("preferred_name", sa.String(100), nullable=True),
        sa.Column("school", sa.String(255), nullable=True),
        sa.Column("year_level", sa.String(20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_student_profiles_user_id", "student_profiles", ["user_id"], unique=True)

    # student_payments
    op.create_table(
        "student_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("student_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("parent_profile_id", sa.Integer(), sa.ForeignKey("parent_profiles.id"), nullable=True),
        sa.Column("term", sa.String(100), nullable=False),
        sa.Column("amount_due", sa.Numeric(10, 2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("paid", "pending", "overdue", "waived", name="paymentstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "payment_method",
            sa.Enum("bank_transfer", "cash", "other", name="paymentmethod"),
            nullable=True,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_student_payments_student_user_id", "student_payments", ["student_user_id"])
    op.create_index("ix_student_payments_parent_profile_id", "student_payments", ["parent_profile_id"])

    # student_payment_logs
    op.create_table(
        "student_payment_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("student_payments.id"), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("old_status", sa.String(50), nullable=True),
        sa.Column("new_status", sa.String(50), nullable=True),
        sa.Column("performed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_student_payment_logs_payment_id", "student_payment_logs", ["payment_id"])

    # tutor_payments
    op.create_table(
        "tutor_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("tutor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("pay_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pay_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount_due", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("amount_paid", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.Enum("unpaid", "pending", "paid", name="tutorpaymentstatus"),
            nullable=False,
            server_default="unpaid",
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tutor_payments_tutor_user_id", "tutor_payments", ["tutor_user_id"])

    # tutor_payment_items
    op.create_table(
        "tutor_payment_items",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("tutor_payment_id", sa.Integer(), sa.ForeignKey("tutor_payments.id"), nullable=False),
        sa.Column("lesson_id", sa.Integer(), sa.ForeignKey("lessons.id"), nullable=False),
        sa.Column("hours", sa.Numeric(5, 2), nullable=False),
        sa.Column("rate", sa.Numeric(10, 2), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tutor_payment_items_payment_id", "tutor_payment_items", ["tutor_payment_id"])

    # progress_reports
    op.create_table(
        "progress_reports",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("student_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("lesson_id", sa.Integer(), sa.ForeignKey("lessons.id"), nullable=False),
        sa.Column("tutor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("class_group_id", sa.Integer(), sa.ForeignKey("class_groups.id"), nullable=False),
        sa.Column("tally_submission_id", sa.String(255), nullable=True),
        sa.Column("report_url", sa.String(1024), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("strengths", sa.Text(), nullable=True),
        sa.Column("areas_for_improvement", sa.Text(), nullable=True),
        sa.Column("next_steps", sa.Text(), nullable=True),
        sa.Column("homework_set", sa.Text(), nullable=True),
        sa.Column("visible_to_parent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("visible_to_student", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "status",
            sa.Enum("draft", "submitted", "approved", "sent_to_parent", name="reportstatus"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_progress_reports_student_user_id", "progress_reports", ["student_user_id"])
    op.create_index("ix_progress_reports_lesson_id", "progress_reports", ["lesson_id"])
    op.create_index("ix_progress_reports_tutor_user_id", "progress_reports", ["tutor_user_id"])
    op.create_index("ix_progress_reports_class_group_id", "progress_reports", ["class_group_id"])

    # tutor_availability
    op.create_table(
        "tutor_availability",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("tutor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("unavailable_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unavailable_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", name="availabilitystatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tutor_availability_tutor_user_id", "tutor_availability", ["tutor_user_id"])

    # makeup_lesson_requests
    op.create_table(
        "makeup_lesson_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("original_lesson_id", sa.Integer(), sa.ForeignKey("lessons.id"), nullable=False),
        sa.Column("student_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("rescheduled_lesson_id", sa.Integer(), sa.ForeignKey("lessons.id"), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", "completed", name="makeupstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_makeup_lesson_requests_lesson_id", "makeup_lesson_requests", ["original_lesson_id"])
    op.create_index("ix_makeup_lesson_requests_student_user_id", "makeup_lesson_requests", ["student_user_id"])

    # announcements
    op.create_table(
        "announcements",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("target_role", sa.String(50), nullable=True),
        sa.Column("target_class_group_id", sa.Integer(), sa.ForeignKey("class_groups.id"), nullable=True),
        sa.Column("target_program_id", sa.Integer(), sa.ForeignKey("programs.id"), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("draft", "published", "archived", name="announcementstatus"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # resources
    op.create_table(
        "resources",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("resource_url", sa.String(1024), nullable=False),
        sa.Column(
            "resource_type",
            sa.Enum("worksheet", "recording", "link", "document", "other", name="resourcetype"),
            nullable=False,
            server_default="link",
        ),
        sa.Column("class_group_id", sa.Integer(), sa.ForeignKey("class_groups.id"), nullable=True),
        sa.Column("program_id", sa.Integer(), sa.ForeignKey("programs.id"), nullable=True),
        sa.Column("student_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # system_alerts
    op.create_table(
        "system_alerts",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("alert_type", sa.String(100), nullable=False, index=True),
        sa.Column(
            "severity",
            sa.Enum("low", "medium", "high", "critical", name="alertseverity"),
            nullable=False,
            server_default="medium",
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("related_entity_type", sa.String(100), nullable=True),
        sa.Column("related_entity_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("open", "resolved", "dismissed", name="alertstatus"),
            nullable=False,
            server_default="open",
        ),
        sa.Column("assigned_to", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )

    # email_templates
    op.create_table(
        "email_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "template_type",
            sa.Enum(
                "payment_reminder", "progress_report_reminder", "homework_reminder",
                "parent_invite", "general",
                name="emailtemplatetype",
            ),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------ #
    # SEED DATA                                                           #
    # ------------------------------------------------------------------ #
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc).isoformat()

    # Default subject: Mathematics
    op.execute(f"""
        INSERT INTO subjects (name, status, created_at)
        VALUES ('Mathematics', 'active', '{now}')
        ON CONFLICT (name) DO NOTHING
    """)

    # NSW programs
    op.execute(f"""
        INSERT INTO programs (subject_id, name, year_level, description, status, created_at)
        SELECT s.id, p.name, p.year_level, p.description, 'active', '{now}'
        FROM subjects s,
        (VALUES
            ('HSC Mathematics', 'Year 11-12', 'HSC Maths Advanced and Extension'),
            ('Year 10 Mathematics', 'Year 10', 'Year 10 Maths preparation'),
            ('Accelerated Mathematics', 'Year 7-10', 'Accelerated Maths program'),
            ('Selective & OC Preparation', 'Year 4-6', 'Selective School and OC test prep'),
            ('Primary Mathematics', 'Year 3-6', 'Primary school Maths foundations'),
            ('Junior Foundations', 'Year 1-2', 'Early numeracy foundations')
        ) AS p(name, year_level, description)
        WHERE s.name = 'Mathematics'
        ON CONFLICT DO NOTHING
    """)

    # Default email templates
    op.execute(f"""
        INSERT INTO email_templates (name, subject, body, template_type, active, created_at, updated_at)
        VALUES
        ('Payment Reminder',
         'Payment reminder — {{student_name}} — {{class_name}}',
         'Dear {{parent_name}},\n\nThis is a friendly reminder that a payment of ${{amount_due}} for {{student_name}}''s {{class_name}} is due on {{due_date}}.\n\nPlease transfer to our account or contact us if you have any questions.\n\nKind regards,\nRyze Education',
         'payment_reminder', true, '{now}', '{now}'),

        ('Progress Report Reminder',
         'Action required — progress report for {{student_name}}',
         'Hi {{tutor_name}},\n\nA progress report for {{student_name}} ({{class_name}}, lesson on {{lesson_date}}) is still outstanding.\n\nPlease submit via the portal or Tally form at your earliest convenience.\n\nThank you,\nRyze Education Admin',
         'progress_report_reminder', true, '{now}', '{now}'),

        ('Parent Portal Invite',
         'Welcome to the Ryze Education Parent Portal',
         'Dear {{parent_name}},\n\nYou have been invited to the Ryze Education parent portal, where you can track {{student_name}}''s upcoming lessons, attendance, homework and progress reports.\n\nClick the link below to set your password and get started:\n{{portal_link}}\n\nThis link expires in 48 hours.\n\nWelcome aboard,\nRyze Education',
         'parent_invite', true, '{now}', '{now}')
        ON CONFLICT (name) DO NOTHING
    """)


def downgrade() -> None:
    # Drop new tables in reverse dependency order
    op.drop_table("email_templates")
    op.drop_table("system_alerts")
    op.drop_table("resources")
    op.drop_table("announcements")
    op.drop_table("makeup_lesson_requests")
    op.drop_table("tutor_availability")
    op.drop_table("progress_reports")
    op.drop_table("tutor_payment_items")
    op.drop_table("tutor_payments")
    op.drop_table("student_payment_logs")
    op.drop_table("student_payments")
    op.drop_table("student_profiles")
    op.drop_table("student_parent_links")
    op.drop_table("portal_credentials")
    op.drop_table("parent_profiles")
    op.drop_table("programs")
    op.drop_table("subjects")

    # Remove added columns from existing tables
    op.drop_column("class_group_members", "end_date")
    op.drop_column("class_group_members", "start_date")
    op.drop_column("class_group_members", "enrollment_status")
    op.drop_index("ix_class_groups_program_id", "class_groups")
    op.drop_column("class_groups", "default_duration_minutes")
    op.drop_column("class_groups", "delivery_mode")
    op.drop_column("class_groups", "description")
    op.drop_column("class_groups", "program_id")
    op.drop_column("attendance_records", "discord_verified_minutes")
    op.drop_column("attendance_records", "discord_verification_status")
    op.drop_column("attendance_records", "tutor_marked_at")
    op.drop_column("attendance_records", "tutor_marked_status")
