#!/usr/bin/env bash
# Script de build para o Render (plano gratuito).
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate
python manage.py createsu
