import uuid

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=email.strip().lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True)
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="user_email_case_insensitive")]

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)


class Organization(models.Model):
    class Kind(models.TextChoices):
        INSURER = "insurer", "Insurer"
        BUSINESS = "business", "Business"
        PROVIDER = "provider", "IT provider"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=16, choices=Kind.choices)

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "Insurer administrator"
        UNDERWRITER = "underwriter", "Underwriter"
        REVIEWER = "reviewer", "Technical reviewer"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    role = models.CharField(max_length=16, choices=Role.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "organization"], name="unique_membership")]


class ChecklistVersion(models.Model):
    insurer = models.ForeignKey(Organization, on_delete=models.PROTECT)
    product = models.CharField(max_length=100, default="Commercial cyber")
    version = models.CharField(max_length=40)
    requirements = models.JSONField(default=list)
    recovery_targets = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["insurer", "product", "version"], name="unique_checklist_version")]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Published checklists are immutable; create a new version.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.insurer} · {self.product} · {self.version}"


class Case(models.Model):
    class Kind(models.TextChoices):
        APPLICATION = "application", "Application"
        RENEWAL = "renewal", "Renewal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    renewal_of = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True, related_name="renewal_case")
    reference = models.CharField(max_length=40, unique=True)
    insurer = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="insurer_cases")
    applicant = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="applicant_cases")
    checklist = models.ForeignKey(ChecklistVersion, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.RENEWAL)
    service = models.CharField(max_length=160)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="assigned_cases")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_cases")
    created_at = models.DateTimeField(auto_now_add=True)
    due_date = models.DateField(null=True, blank=True)
    internal_notes = models.TextField(blank=True)
    consent_granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="authorized_cases")
    consent_expires_at = models.DateTimeField(null=True, blank=True)
    consent_revoked_at = models.DateTimeField(null=True, blank=True)

    @property
    def consent_active(self):
        return bool(self.consent_granted_by_id and not self.consent_revoked_at and self.consent_expires_at and self.consent_expires_at > timezone.now())

    @property
    def status_label(self):
        return "Awaiting evidence" if self.consent_active else "Awaiting authorization"

    def __str__(self):
        return self.reference


class SharingGrant(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Business owner"
        TECHNICIAN = "technician", "IT provider technician"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    role = models.CharField(max_length=16, choices=Role.choices)
    organization_name = models.CharField(max_length=160, blank=True)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="issued_grants")
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def active(self):
        return not self.revoked_at and self.expires_at > timezone.now() and self.user.is_active

    class Meta:
        constraints = [models.UniqueConstraint(fields=["case", "user"], name="one_grant_per_case_user")]


class Invitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=16, choices=SharingGrant.Role.choices)
    organization_name = models.CharField(max_length=160, blank=True)
    token_digest = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    expires_at = models.DateTimeField()
    access_expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def usable(self):
        return not self.accepted_at and not self.revoked_at and self.expires_at > timezone.now() and self.access_expires_at > timezone.now()


class EvidenceRequest(models.Model):
    kind = models.CharField(max_length=16, choices=[("control", "Security control"), ("backup", "Backup coverage"), ("restore", "Supervised restore")], default="control")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="requests")
    title = models.CharField(max_length=160)
    description = models.TextField()
    freshness_days = models.PositiveIntegerField()
    due_date = models.DateField(null=True, blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)

    def __str__(self):
        return self.title


class CaseFile(models.Model):
    """Private sample files; versioned upload ingestion belongs to Phase 3."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(EvidenceRequest, on_delete=models.CASCADE, related_name="files")
    name = models.CharField(max_length=160)
    storage_key = models.CharField(max_length=200)


class CaseJob(models.Model):
    """Authorization boundary for subsequent background processors."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="jobs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    grant = models.ForeignKey(SharingGrant, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=20, default="queued", choices=[("queued", "Queued"), ("completed", "Completed"), ("blocked", "Access revoked")])
    created_at = models.DateTimeField(auto_now_add=True)


class AuditEvent(models.Model):
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    detail = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
