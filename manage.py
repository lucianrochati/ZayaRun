#!/usr/bin/env python
"""Utilitario de linha de comando do Django para o ZayaRun."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zayarun.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Nao foi possivel importar o Django. Ele esta instalado e "
            "disponivel na sua PYTHONPATH? Voce esqueceu de ativar o "
            "ambiente virtual?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
