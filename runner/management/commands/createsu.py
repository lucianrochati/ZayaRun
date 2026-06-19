"""
Cria/atualiza um superusuario a partir de variaveis de ambiente.

Permite logar no ZayaRun sem precisar de terminal (ideal para deploy
via botao do Heroku). Idempotente: roda em todo deploy sem quebrar.

Variaveis usadas:
  ADMIN_USERNAME  (obrigatoria)
  ADMIN_PASSWORD  (obrigatoria)
  ADMIN_EMAIL     (opcional)
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Cria ou atualiza um superusuario a partir de variaveis de ambiente."

    def handle(self, *args, **options):
        username = os.environ.get("ADMIN_USERNAME")
        password = os.environ.get("ADMIN_PASSWORD")
        email = os.environ.get("ADMIN_EMAIL", "")

        if not username or not password:
            self.stdout.write(
                "ADMIN_USERNAME/ADMIN_PASSWORD nao definidos; pulando criacao."
            )
            return

        User = get_user_model()
        user, created = User.objects.get_or_create(username=username)
        user.email = email or user.email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        status = "criado" if created else "atualizado"
        self.stdout.write(self.style.SUCCESS(f"Superusuario '{username}' {status}."))
