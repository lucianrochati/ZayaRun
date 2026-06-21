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

### Lado-treinador / assessoria (NOVO — H1→H3)
- **Papéis e vínculo:** `Profile` (atleta/treinador), `CoachAthlete` com convite por código + aceite.
- **Painel do coach (H1):** roster com **triagem por IA/regras** (semáforo de risco, abandono, conquista) e resumo do dia.
- **Prescrição (H1+H2):** treino manual, **semana gerada automaticamente** (regras + ajuste por ACWR, nota opcional da Claude) e **planejado × realizado** com matching automático no sync da Strava + score de aderência.
- **Feedback do atleta (H1):** PSE (1–10) + sensação, e **check-in diário** (sono/dor/estresse) que alimentam o contexto da IA.
- **Perfil de aptidão (NOVO):** `FitnessProfile` derivado de TODO o histórico (services/fitness.py) — volume típico, tendência, frequência, maior longão, pico, zonas de pace reais (fácil/limiar/esforço), ACWR, consistência, nível e confiança. Recalculado a cada sync.
- **Plano de prova dirigido pelo histórico (H3):** macrociclo periodizado (base→construção→pico→polimento) cujos números (base, pico, frequência, teto do longão, paces) vêm do `FitnessProfile` real — não de offsets genéricos. Recriar = re-planeja pela forma atual. Backfill profundo na 1ª conexão.
- **CRUD do plano (NOVO):** tela `plan_detail` (macrociclo semana a semana) + editar/excluir treino + adicionar treino + excluir plano. Criar plano cai direto nessa visão. Permissão atleta-ou-coach.
- **Copiloto conversacional (H3):** pergunta em linguagem natural respondida sobre os próprios dados (fallback honesto sem chave).
- **Prontidão (H3):** índice heurístico **transparente** (carga+percepção+check-in); HRV/sono e ML de lesão deixados como ponto de extensão honesto (sem número falso) em `services/wellness.py` e `services/garmin.py`.
- **Login com Strava:** botão "Entrar com a Strava" na tela de login cria/loga a conta pelo athlete_id (sem cadastro manual). ⚠️ limitado pela **cota de atletas conectados** da Strava (apps não aprovados ≈ só o dono) — pedir aumento na Strava p/ distribuir.
- **Compliance Strava:** ao desconectar, purga atividades + perfil (exigência dos termos).
- **Navegação:** bottom nav (Painel / Plano / Copiloto / Treinador).
- **Deploy:** no ar no **Railway** (Postgres). `railway.json` roda migrate/collectstatic/createsu/gunicorn; `runtime.txt` em 3.12.10 (fix mise). `seed_demo` popula treinador/atleta demo.
- **Testes:** 56 passando (`python manage.py test`).
- Deploy preparado para **Heroku** (`app.json`, `Procfile`) e **Render** (`render.yaml`, `build.sh`).

## 4. Arquitetura / arquivos importantes
```
zayarun/settings.py          # config (env vars; ALLOWED_HOSTS p/ heroku+render; INSIGHT_*; STRAVA_*)
runner/models.py             # StravaToken, Activity (distance, pace, cadence, splits...)
runner/views.py              # dashboard (corredor + semana prescrita + feedback), OAuth, activity_detail
runner/views_coach.py        # roster/triagem, prescrição, plano, feedback/PSE, check-in, copiloto
runner/services/strava.py    # cliente OAuth + API Strava (sync agora reconcilia planejado×realizado)
runner/services/garmin.py    # placeholder (aguardando aprovação; inclui contrato sync_wellness)
runner/services/metrics.py   # pace, volume, evolução, ACWR, Riegel, PRs, adherence, split_fade
runner/services/matching.py  # casa Activity ↔ PlannedWorkout (planejado × realizado)
runner/services/fitness.py   # perfil de aptidão (lê todo o histórico → FitnessProfile)
runner/services/plans.py     # gerador de macrociclo dirigido pelo FitnessProfile
runner/services/ai.py        # wrapper fino sobre a Claude (compartilhado), fallback honesto
runner/services/coaching.py  # auto_prescribe, triage_roster, analyze_workout, copiloto
runner/services/wellness.py  # prontidão (heurística transparente) + ponto de extensão p/ ML
runner/services/insights.py  # "Insight do dia": build_context (+wellness) + regras + Claude
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
3. ~~**Lado da assessoria**~~ ✅ **FEITO** (H1→H3): roster+triagem, prescrição manual/IA, planejado×realizado,
   plano de prova adaptativo, copiloto, PSE/check-in, prontidão. Próximos refinos abaixo.
4. **Garmin** (quando a Health API for aprovada) — ativar `sync_activities` + `sync_wellness` (HRV/sono → prontidão/ML).
5. **ML de lesão real:** trocar a heurística de `wellness.predict_injury_risk` por modelo treinado (exige HRV/sono + histórico rotulado).
6. **Notificações** ao coach (atleta em risco/sumido) e ao atleta (treino do dia).
7. Ideias de UX: PRs com confete, gráficos interativos (tocar→treino), média móvel, card compartilhável.

### Setup de ambiente (NOVO)
- Não havia Python na máquina; instalado **Python 3.12** via `winget` + criado **`.venv`** local com `requirements.txt`.
- Rodar testes/servidor: `.\.venv\Scripts\python.exe manage.py test` / `runserver`.

## 9. Pontos de atenção
- **Termos da Strava** para app publicado: "Powered by Strava", link "Ver no Strava", não treinar modelos com os dados.
- **Política de privacidade** obrigatória na Play (e ao usar dados da Strava).
- **Custo:** Render free "dorme" após ~15 min; Railway/Heroku são pagos (poucos dólares/mês).
- **Segurança:** repo é público — nenhum segredo deve ser commitado (secret/admin/API key só em Config Vars).

## 10. Como retomar
Branch `claude/zayarun-app-design-4rsvdl`. Rodar testes (`python manage.py test`) para confirmar baseline (45 OK),
escolher o item 1 ou 2 da seção 8 e seguir.
