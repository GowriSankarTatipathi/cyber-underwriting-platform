from allauth.account.models import EmailAddress
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from workspaces.models import Membership, Organization, User


class Command(BaseCommand):
    help = "Give an existing operator-created user an insurer administrator membership after verifying their email out of band."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--insurer", required=True)
        parser.add_argument("--email-verified", action="store_true", required=True)

    @transaction.atomic
    def handle(self, email, insurer, **options):
        user = User.objects.filter(email=email.strip().lower(), is_active=True).first()
        if not user:
            raise CommandError("Create this administrator with createsuperuser first. Passwords are entered interactively, never as command arguments.")
        organization, _ = Organization.objects.get_or_create(name=insurer, kind="insurer")
        Membership.objects.update_or_create(user=user, organization=organization, defaults={"role": "admin", "is_active": True})
        EmailAddress.objects.update_or_create(user=user, email=user.email, defaults={"verified": True, "primary": True})
        self.stdout.write("Insurer administrator prepared. Sign-in still requires MFA enrollment. Publish an insurer checklist before creating cases.")
