# ZayaRun 🏃‍♂️▲

App de **assessoria de corrida de rua / trail** que conecta na sua **Strava**
(e, em breve, **Garmin**) e mostra seus dados do jeito do treinador:
distância, pace, volume semanal, evolução e **projeção de provas** — de forma
simples e acessível.

> MVP focado no **lado do corredor**: você conecta a Strava e acompanha sua
> evolução. O lado da assessoria (treinador montando planos para alunos) é a
> próxima fase.

## O que já faz

- 🔗 **Integração Strava** via OAuth (sincroniza suas atividades).
- 📊 **Painel do corredor:** distância total, pace médio, nº de corridas, elevação.
- 📈 **Evolução do pace** (gráfico por corrida) e comparação das últimas corridas.
- 🗓️ **Volume semanal** (km por semana, últimas 8 semanas).
- 🩹 **Carga de treino (ACWR)** — indicador de risco de lesão (zona ideal 0.8–1.3).
- 🏁 **Projeção de provas** (5k/10k/21k/42k) pela fórmula de Riegel.
- ⏱️ **Detalhe da atividade** com parciais por km (splits).
- 📱 Layout **mobile-first** — abra no navegador do celular depois do treino.

> **Garmin:** estruturado em `runner/services/garmin.py`, mas desativado até a
> aprovação da Garmin Health API (não há OAuth público instantâneo como na Strava).

## Stack

- **Django 5** + **Django REST-ready** · **PostgreSQL** (SQLite no local)
- **WhiteNoise** para estáticos · **Chart.js** (CDN) para gráficos
- Pronto pra **Heroku** (`Procfile`, `runtime.txt`)

## Rodando localmente

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # preencha as chaves da Strava
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Acesse http://127.0.0.1:8000/ , faça login e clique em **Conectar com a Strava**.

### Configurando a Strava (acesso imediato)

1. Crie um app em https://www.strava.com/settings/api
2. Em **Authorization Callback Domain**, use `127.0.0.1` (local) ou seu domínio Heroku.
3. Copie **Client ID** e **Client Secret** para o `.env`:
   ```
   STRAVA_CLIENT_ID=xxxxx
   STRAVA_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
   STRAVA_REDIRECT_URI=http://127.0.0.1:8000/strava/callback/
   ```

> Limites da Strava: 100 req/15min e 1000 req/dia por app. Escopo usado:
> `activity:read_all`.

## Deploy no Heroku

```bash
heroku create
heroku addons:create heroku-postgresql:essential-0
heroku config:set SECRET_KEY=... DEBUG=False \
  STRAVA_CLIENT_ID=... STRAVA_CLIENT_SECRET=... \
  STRAVA_REDIRECT_URI=https://SEU-APP.herokuapp.com/strava/callback/
git push heroku main
heroku run python manage.py createsuperuser
```

(No painel da Strava, ajuste o callback domain para `SEU-APP.herokuapp.com`.)

## Testes

```bash
python manage.py test
```

## Estrutura

```
zayarun/            # config do projeto (settings, urls, wsgi)
runner/             # app do corredor
  models.py         # StravaToken, Activity
  views.py          # dashboard, fluxo OAuth, sync, detalhe
  services/
    strava.py       # cliente OAuth + API Strava
    garmin.py       # placeholder (aguardando aprovação)
    metrics.py      # pace, volume, evolução, ACWR, projeção (Riegel)
  templates/runner/ # base, login, dashboard, detalhe
  static/runner/    # css
```

## Próximos passos (roadmap)

1. Garmin Health API (quando aprovada).
2. Lado da **assessoria**: treinador cria planos e acompanha alunos.
3. Aderência ao plano (treino prescrito × realizado).
4. GPS próprio (tracking nativo) — fase mobile.
