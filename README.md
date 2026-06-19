# ZayaRun 🏃‍♂️▲

App de **assessoria de corrida de rua / trail** que conecta na sua **Strava**
(e, em breve, **Garmin**) e mostra seus dados do jeito do treinador:
distância, pace, volume semanal, evolução e **projeção de provas** — de forma
simples e acessível.

> MVP focado no **lado do corredor**: você conecta a Strava e acompanha sua
> evolução. O lado da assessoria (treinador montando planos para alunos) é a
> próxima fase.

## 🆓 Testar de graça (Render) — recomendado

O [Render](https://render.com) tem **web + Postgres gratuitos** e lê o
`render.yaml` deste repositório. Tudo pelo celular, sem cartão:

1. Crie conta em https://render.com (pode usar "Sign in with GitHub").
2. **New +** → **Blueprint** → conecte o repositório `lucianrochati/ZayaRun`
   (branch `claude/zayarun-app-design-4rsvdl`).
3. O Render lê o `render.yaml` e pede só dois valores:
   - **STRAVA_CLIENT_SECRET** → o Client Secret da Strava.
   - **ADMIN_PASSWORD** → uma senha sua (usuário já vem como `lucian`).
4. **Apply** e aguarde o build. Sua URL será algo como
   `https://zayarun.onrender.com`.
5. Na Strava (https://www.strava.com/settings/api), em **Authorization
   Callback Domain**, coloque o host do seu app (ex.: `zayarun.onrender.com`).
6. Abra a URL → **Entrar** (`lucian` + sua senha) → **Conectar com a Strava**.

> O plano free do Render "dorme" após ~15 min sem uso (a primeira abertura
> depois disso leva ~30–60s). O Postgres free é ótimo para testar; para uso
> contínuo, um Postgres gratuito sem expiração como o [Neon](https://neon.tech)
> pode ser plugado via `DATABASE_URL`.

## 🚀 Alternativa: Heroku (pago, ~US$5–10/mês)

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/lucianrochati/ZayaRun/tree/claude/zayarun-app-design-4rsvdl)

1. Toque no botão acima e faça login no Heroku.
2. Em **App name**, escolha um nome (ex.: `zayarun-lucian`) — guarde o domínio
   `zayarun-lucian.herokuapp.com`.
3. Preencha:
   - **STRAVA_CLIENT_SECRET** → cole o Client Secret da Strava.
   - **ADMIN_PASSWORD** → escolha uma senha de acesso (usuário já vem como `lucian`).
   - (Client ID `253803` já vem preenchido.)
4. Toque em **Deploy app** e aguarde.
5. Na Strava (https://www.strava.com/settings/api), em **Authorization Callback
   Domain**, coloque o domínio do seu app (ex.: `zayarun-lucian.herokuapp.com`).
6. Abra o app → **Entrar** (`lucian` + sua senha) → **Conectar com a Strava**.
   Na tela da Strava, **autorize ver suas atividades**.

> 🔒 **Segurança:** o Client Secret é digitado direto no Heroku e **nunca fica no
> código**. Se ele já foi exposto (ex.: em um print), use **"Gerar novo segredo do
> cliente"** na Strava depois de testar.

> ℹ️ O escopo `read` que aparece no painel da Strava é só do token de exemplo. O
> ZayaRun pede `activity:read_all` na hora de conectar — por isso é importante
> autorizar quando a Strava perguntar.

## Insight do dia (treinador de bolso)

O ZayaRun lê seus dados e **recomenda o que fazer** — não só mostra números.
Dois modos, controlados por `INSIGHT_PROVIDER`:

- `auto` (padrão): usa a **API da Claude** se `ANTHROPIC_API_KEY` estiver
  definida; senão, cai para o motor de **regras** (sempre funciona, sem custo).
- `rules`: sempre regras. `claude`: sempre Claude (fallback para regras em erro).

Para ativar a narrativa por IA, defina no ambiente (ex.: Config Vars do Render):

```
ANTHROPIC_API_KEY=sk-ant-...
INSIGHT_PROVIDER=auto        # ou "claude"
INSIGHT_MODEL=claude-opus-4-8  # opcional
```

> O insight é cacheado por atleta (~1h) para não pesar no carregamento.

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
  STRAVA_CLIENT_ID=253803 STRAVA_CLIENT_SECRET=... \
  ADMIN_USERNAME=lucian ADMIN_PASSWORD=...
git push heroku main
```

O usuário de login é criado automaticamente no deploy (comando `createsu`,
a partir de `ADMIN_USERNAME`/`ADMIN_PASSWORD`). A URL de callback é deduzida
do próprio domínio — só ajuste o **callback domain** na Strava para
`SEU-APP.herokuapp.com`.

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
