import getpass
from allauth.account.models import EmailAddress
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from workspaces.models import Membership, Organization, User


class Command(BaseCommand):
    help = "Provision an insurer reviewer after verifying their identity out of band."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("insurer_id")
        parser.add_argument("--role", choices=["admin", "underwriter", "reviewer"], default="underwriter")

    @transaction.atomic
    def handle(self, email, insurer_id, role, **options):
        try:
            insurer = Organization.objects.get(pk=insurer_id, kind="insurer")
        except (Organization.DoesNotExist, ValidationError):
            raise CommandError("Use an existing insurer organization UUID.")
        user = User.objects.filter(email=email.lower()).first()
        if user is None:
            password = getpass.getpass("New account password: ")
            if password != getpass.getpass("Confirm password: "):
                raise CommandError("Passwords did not match.")
            user = User(email=email.lower())
            try:
                validate_password(password, user)
            except ValidationError as error:
                raise CommandError(" ".join(error.messages))
            user.set_password(password)
            user.save()
        Membership.objects.update_or_create(user=user, organization=insurer, defaults={"role": role, "is_active": True})
        EmailAddress.objects.update_or_create(user=user, email=user.email, defaults={"verified": True, "primary": True})
        self.stdout.write("Reviewer provisioned. MFA enrollment is required at first sign-in.")
