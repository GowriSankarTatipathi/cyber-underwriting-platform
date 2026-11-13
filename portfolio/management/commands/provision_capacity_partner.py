from getpass import getpass
from allauth.account.models import EmailAddress
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from workspaces.models import User


class Command(BaseCommand):
    help = "Provision a named partner account after verifying its email out of band. Grants no case access or commercial relationship."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--email-verified", required=True, action="store_true")

    @transaction.atomic
    def handle(self, email, **options):
        email = email.strip().lower()
        try:
            validate_email(email)
        except ValidationError as error:
            raise CommandError("Use a valid email address.") from error
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError("An account with that email already exists. Its password and access were not changed.")
        user = User(email=email)
        password = getpass("New partner password: ")
        if password != getpass("Confirm password: "):
            raise CommandError("Passwords do not match.")
        try:
            validate_password(password, user)
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from error
        user.set_password(password)
        user.save()
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
        self.stdout.write("Partner account created with no case access. Sign-in uses the deployment's MFA policy. Record the commercial relationship and approve a submission separately. No email was sent.")
