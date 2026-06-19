"""
Integracao Garmin (placeholder estruturado).

Por que ainda nao esta ativa:
- A Garmin Health API / Activity API exige aprovacao de parceria
  (https://developer.garmin.com/gc-developer-program/). Nao ha OAuth
  publico instantaneo como na Strava.
- Assim que as credenciais forem aprovadas, implemente aqui o mesmo
  contrato usado em services/strava.py:
    - build_authorize_url(state)
    - exchange_code_for_token(user, code)
    - sync_activities(user)
  e adicione um GarminToken em models.py (espelhando StravaToken).

Manter este modulo com a mesma assinatura facilita plugar a Garmin
sem mexer nas views.
"""


def is_configured():
    """Garmin ainda nao habilitado (aguardando aprovacao da API)."""
    return False


def sync_activities(user, **kwargs):
    raise NotImplementedError(
        "Integracao Garmin pendente de aprovacao da API. "
        "Use a Strava por enquanto."
    )
