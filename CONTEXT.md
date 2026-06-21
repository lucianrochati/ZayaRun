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
- **CRUD do plano:** tela `plan_detail` (macrociclo semana a semana) + editar/excluir treino + adicionar treino + excluir plano. Criar plano cai direto nessa visão. Permissão atleta-ou-coach.
- **Anamnese + agendamento clínico (NOVO):** modelo `Anamnese` (dias disponíveis, sessões, longão, lesão, dor atual, terreno, objetivo) **pré-preenchida pela leitura da Strava**. O gerador usa os dias reais e **espaça os treinos-chave** (nunca dois pesados em dias seguidos — `plans.weekly_schedule`). **Blindagem clínica**: dor/lesão/ACWR/pouco histórico/prazo curto geram avisos (`meta.safety_warnings`, salvos em `plan.notes`) e versão conservadora; recomenda avaliação profissional.
- **Copiloto conversacional (H3):** pergunta em linguagem natural respondida sobre os próprios dados (fallback honesto sem chave).
- **Prontidão (H3):** índice heurístico **transparente** (carga+percepção+check-in); HRV/sono e ML de lesão deixados como ponto de extensão honesto (sem número falso) em `services/wellness.py` e `services/garmin.py`.
- **Login com Strava:** botão "Entrar com a Strava" na tela de login cria/loga a conta pelo athlete_id (sem cadastro manual). ⚠️ limitado pela **cota de atletas conectados** da Strava (apps não aprovados ≈ só o dono) — pedir aumento na Strava p/ distribuir.
- **Compliance Strava:** ao desconectar, purga atividades + perfil (exigência dos termos).
- **Navegação:** bottom nav (Painel / Plano / Copiloto / Treinador).
- **Deploy:** no ar no **Railway** (Postgres). `railway.json` roda migrate/collectstatic/createsu/gunicorn; `runtime.txt` em 3.12.10 (fix mise). `seed_demo` popula treinador/atleta demo.
- **Testes:** 62 passando (`python manage.py test`).
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
- **No ar no Railway** (serviço `web` + Postgres). `railway.json` roda migrate/collectstatic/createsu/gunicorn;
  `settings.py` libera `.up.railway.app`/`.railway.app`; `runtime.txt` em **3.12.10** (corrige bug de attestation do mise).
- `DATABASE_URL` no serviço web referencia `${{Postgres.DATABASE_URL}}` (host interno só funciona dentro do Railway).
- `seed_demo` popula treinador/atleta demo (rodar via URL pública do Postgres com `railway run`).
- Login com Strava existe, mas **bloqueado pela cota de atletas conectados** da Strava (apps não aprovados ≈ só o dono);
  pedir aumento na Strava para distribuir a testers.
- Alternativas documentadas: Render (`render.yaml`) e Heroku (`app.json`).

## 8. PENDÊNCIAS / próximos passos (priorizado)

### 🐛 BUGS A CORRIGIR — PRIORIDADE (reportados 2026-06-20)
1. **Plano começa no futuro, não hoje.** Criado hoje, o plano inicia ~2 meses depois (ex.: 17/08).
   **Esperado:** começar JÁ, no próximo dia disponível da agenda (se hoje é sábado, domingo já é treino).
   **Causa:** `plans.generate_plan` limita `n` a `MAX_WEEKS=20` e faz `first_monday = race_monday - (n-1) semanas`;
   com a prova a >20 semanas, o início é empurrado pra frente. **Corrigir:** ancorar o início na semana ATUAL
   (a partir do próximo dia disponível) e, se a prova é distante, fazer fase de base começando agora — nunca adiar o início.
2. **KM não batem (título × descrição × alvo).** Ex.: "Rodagem + educativos" alvo 5,6 km mas a descrição diz
   "6 km soltos"; "Longão" título "9 km" mas alvo 8,9 km. **Causa:** formatação `:.0f` nos títulos/descrições
   arredonda, enquanto `target_distance_km` mostra 1 casa; e os 6×100m (600 m) dos educativos não entram no alvo.
   Locais: `plans._quality_session` (base) e `plans.build_week` (longão). **Corrigir:** um único número coerente
   entre título, descrição e `target_distance_m` (somar os educativos ao alvo ou arredondar igual em todos).

### Próximos passos
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
Branch `claude/zayarun-app-design-4rsvdl`. Rodar testes (`.\.venv\Scripts\python.exe manage.py test`) para confirmar
baseline (**62 OK**). **Próximo passo recomendado: os 2 BUGS no topo da seção 8** (início do plano no futuro + KM que não batem).
