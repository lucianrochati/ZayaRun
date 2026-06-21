"""
Diagnóstico da IA (Gemini/Claude) sem expor segredos.

Uso (no Railway):  railway run python manage.py check_ai
                   railway run python manage.py check_ai "Em 1 frase, o que é pace?"
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from runner.services import ai


class Command(BaseCommand):
    help = "Mostra como a IA está resolvida e testa uma chamada real (sem vazar a chave)."

    def add_arguments(self, parser):
        parser.add_argument("prompt", nargs="?", default="Responda apenas: pong.")

    def handle(self, *args, **opts):
        gk = ai.gemini_key()
        ak = ai.anthropic_key()
        w = self.stdout.write
        w(f"AI_PROVIDER (settings) : {getattr(settings, 'AI_PROVIDER', 'auto')}")
        w(f"GEMINI_MODEL           : {getattr(settings, 'GEMINI_MODEL', '-')}")
        w(f"provider() resolvido   : {ai.provider()}")
        if gk:
            w(f"GEMINI_API_KEY         : presente — prefixo '{gk[:4]}…', {len(gk)} chars"
              + ("  ✅ formato de API key" if gk.startswith("AIza")
                 else "  ⚠️ NÃO começa com 'AIza' (parece token OAuth, não API key)"))
        else:
            w("GEMINI_API_KEY         : AUSENTE")
        w(f"ANTHROPIC_API_KEY      : {'presente' if ak else 'ausente'}")
        w("-" * 60)

        if ai.provider() == "gemini":
            text, debug = ai.gemini_request("Você é um teste.", opts["prompt"], max_tokens=60)
            w(f"Gemini diag            : {debug}")
            if text:
                w(f"Resposta               : {text}")
        out = ai.complete("Você é um teste de corrida.", opts["prompt"], max_tokens=60)
        w("-" * 60)
        if out:
            w(self.style.SUCCESS(f"complete() OK → {out}"))
        else:
            w(self.style.ERROR("complete() retornou None — a IA NÃO respondeu (veja o diag acima)."))
