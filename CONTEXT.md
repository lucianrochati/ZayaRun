# ZayaRun — Contexto do projeto (handoff)

> Documento para retomar o desenvolvimento de onde paramos. Última atualização: 2026-06-19.

## 1. Visão do produto
App de **assessoria de corrida de rua / trail** que conecta na **Strava** (e, futuramente, **Garmin**)
e entrega o que esses apps fazem mal: a camada de **treinador** — evolução real, prevenção de lesão e
orientação prescritiva, de forma simples e acessível, em português.

**Posicionamento:** "app de treinador, não de rede social". MVP focado no **lado do corredor**.

## 2. Decisões-chave já tomadas
- **Stack:** Django 5 + Python 3.12 (web app responsivo / PWA). Banco Postgres (SQLite no local).
- **Por que web e não nativo agora:** mais rápido de testar e validar; nativo (Flutter) fica para depois.
- **Integração:** Strava primeiro (acesso imediato). Garmin estruturado mas desativado (exige aprovação da API).
- **GPS próprio:** fora do MVP (fase futura, depende de nativo).
- **Insight com IA:** opcional via API da Claude (SDK `anthropic`, modelo `claude-opus-4-8`), com fallback por regras.
- **Repositório:** `lucianrochati/ZayaRun` (PÚBLICO). Branch de trabalho: `claude/zayarun-app-design-4rsvdl`.

## 3. O que já está PRONTO ✅
- OAuth Strava + sincronização de atividades (refresh de token automático; redirect deduzido do host).
- Painel do corredor: distância, pace médio, nº de corridas, elevação, média por corrida, cadência média.
- **Seletor de período** (Semana/Mês/Ano/Tudo) filtrando todas as métricas.
- **Gráficos** (Chart.js): volume adaptável (dia/semana/mês) + evolução de pace, distância e cadência.
- **Carga de treino (ACWR)** com status e barra (indicador de risco de lesão).
- **Projeção de provas** 5k/10k/21k/42k (fórmula de Riegel).
- **Recordes pessoais** automáticos por distância, com selo "Novo!".
- **Insight do dia** (motor de regras + opção via Claude; cache por atleta ~1h).
- **Detalhe da atividade** com parciais por km (splits).
- **Redesign enterprise**: design system (tokens, Inter), logo SVG + ícones PNG, **PWA instalável** (manifest + meta tags).
- **Folder de divulgação**: `pitch/zayarun-folder.png` (vertical) e `pitch/zayarun-folder.html`.
- **Testes:** 24 passando (`python manage.py test`).
- Deploy preparado para **Heroku** (`app.json`, `Procfile`) e **Render** (`render.yaml`, `build.sh`).

## 4. Arquitetura / arquivos importantes
```
zayarun/settings.py          # config (env vars; ALLOWED_HOSTS p/ heroku+render; INSIGHT_*; STRAVA_*)
runner/models.py             # StravaToken, Activity (distance, pace, cadence, splits...)
runner/views.py              # dashboard, OAuth (connect/callback/sync/disconnect), activity_detail
runner/services/strava.py    # cliente OAuth + API Strava
runner/services/garmin.py    # placeholder (aguardando aprovação)
runner/services/metrics.py   # pace, volume, evolução, ACWR, Riegel, personal_records
runner/services/insights.py  # "Insight do dia": build_context + gerador regras + gerador Claude
runner/management/commands/createsu.py  # cria superusuário via env (deploy sem terminal)
runner/templates/runner/     # base, login, dashboard, activity_detail, _logo_symbol
runner/static/runner/        # css/style.css (design system), img/ (logo+ícones), manifest.webmanifest
pitch/                       # folder de divulgação (png + html)
```

## 5. Como rodar localmente
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # preencher chaves da Strava
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## 6. Variáveis de ambiente
| Var | Para quê |
|---|---|
| `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` | Django |
| `DATABASE_URL` | Postgres (auto no Heroku; setar no Render/Railway) |
| `STRAVA_CLIENT_ID` | **253803** (público) |
| `STRAVA_CLIENT_SECRET` | **NUNCA no repo** — só nas Config Vars do host. ⚠️ foi exposto em chat: **gerar novo** na Strava. |
| `STRAVA_REDIRECT_URI` | opcional (deduzido do host) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | cria login no deploy (`createsu`) |
| `ANTHROPIC_API_KEY` | (opcional) liga o Insight via Claude |
| `INSIGHT_PROVIDER` | `auto` (padrão) / `rules` / `claude` |
| `INSIGHT_MODEL` | `claude-opus-4-8` (padrão) |

## 7. Estado do deploy
- Repo tornado **público** para o botão de deploy funcionar.
- Já existiu tentativa no **Heroku** (esbarrou em cartão/cobrança).
- Caminho gratuito documentado: **Render** (web free + Postgres free) via `render.yaml`.
- Usuário tem conta **Railway** (intenção de usar). Pendência: ajustar `ALLOWED_HOSTS`/CSRF para `.up.railway.app`
  e definir start command (`gunicorn zayarun.wsgi`) + migração no deploy.

## 8. PENDÊNCIAS / próximos passos (priorizado)
1. **Definir host de produção** (Railway ou Render) e deixar no ar com HTTPS.
2. **Publicação na Google Play (TWA)** — app é web, entra como PWA empacotada:
   - falta **service worker** (offline) no PWA;
   - rota `/.well-known/assetlinks.json` (verificação de posse);
   - conta Play Console (US$ 25), empacotar no **PWABuilder**, política de privacidade.
3. **Lado da assessoria** (treinador → aluno): plano prescrito × realizado, painel do coach. (Diferencial / receita.)
4. **Garmin** (quando a Health API for aprovada).
5. Ideias de UX: PRs com confete, gráficos interativos (tocar→treino), média móvel, bottom nav, card compartilhável.

## 9. Pontos de atenção
- **Termos da Strava** para app publicado: "Powered by Strava", link "Ver no Strava", não treinar modelos com os dados.
- **Política de privacidade** obrigatória na Play (e ao usar dados da Strava).
- **Custo:** Render free "dorme" após ~15 min; Railway/Heroku são pagos (poucos dólares/mês).
- **Segurança:** repo é público — nenhum segredo deve ser commitado (secret/admin/API key só em Config Vars).

## 10. Como retomar
Branch `claude/zayarun-app-design-4rsvdl`. Rodar testes (`python manage.py test`) para confirmar baseline (24 OK),
escolher o item 1 ou 2 da seção 8 e seguir.
