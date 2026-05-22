from bot.models.user import User, UserRole
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole, EnrollmentStatus, DeliveryMode
from bot.models.lesson import Lesson, LessonStatus
from bot.models.attendance import AttendanceRecord, AttendanceStatus, MarkedBy, DiscordVerificationStatus, VoiceSession
from bot.models.homework import HomeworkTask, HomeworkSubmission, SubmissionStatus
from bot.models.reminder_log import ReminderLog, ReminderChannel
# Portal models
from bot.models.academic import Subject, Program, ItemStatus
from bot.models.portal_auth import ParentProfile, PortalCredential, StudentParentLink, StudentProfile, ParentRelationship
from bot.models.payment import StudentPayment, StudentPaymentLog, TutorPayment, TutorPaymentItem, PaymentStatus, TutorPaymentStatus, PaymentMethod
from bot.models.operations import (
    ProgressReport, ReportStatus,
    TutorAvailability, AvailabilityStatus,
    MakeupLessonRequest, MakeupStatus,
    Announcement, AnnouncementStatus,
    Resource, ResourceType,
    SystemAlert, AlertSeverity, AlertStatus,
    EmailTemplate, EmailTemplateType,
)

__all__ = [
    # Core
    "User", "UserRole",
    "ClassGroup", "ClassGroupMember", "MemberRole", "EnrollmentStatus", "DeliveryMode",
    "Lesson", "LessonStatus",
    "AttendanceRecord", "AttendanceStatus", "MarkedBy", "DiscordVerificationStatus", "VoiceSession",
    "HomeworkTask", "HomeworkSubmission", "SubmissionStatus",
    "ReminderLog", "ReminderChannel",
    # Academic structure
    "Subject", "Program", "ItemStatus",
    # Portal auth
    "ParentProfile", "PortalCredential", "StudentParentLink", "StudentProfile", "ParentRelationship",
    # Payments
    "StudentPayment", "StudentPaymentLog", "TutorPayment", "TutorPaymentItem",
    "PaymentStatus", "TutorPaymentStatus", "PaymentMethod",
    # Operations
    "ProgressReport", "ReportStatus",
    "TutorAvailability", "AvailabilityStatus",
    "MakeupLessonRequest", "MakeupStatus",
    "Announcement", "AnnouncementStatus",
    "Resource", "ResourceType",
    "SystemAlert", "AlertSeverity", "AlertStatus",
    "EmailTemplate", "EmailTemplateType",
]
