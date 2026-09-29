"""Assistant de support : une question en clair, un diagnostic tire des mesures.

Le technicien au telephone avec un abonne ecrit ce qu'il entend ("coupures le
soir", "Netflix saccade") ; l'assistant recoit la question ET l'etat mesure du
reseau (abonne, latence, bufferbloat, radio, saturation, cycles de mesure) et
rend un diagnostic : cause probable, preuves chiffrees, action a mener.

OPTIONNEL ET EXPLICITE. Rien ne part vers le modele tant qu'aucune cle API
n'est configuree (``ANTHROPIC_API_KEY``). Seul ce qui est affiche dans
l'interface lui est envoye : identifiant, plan, debits, latences, etat radio --
jamais de mot de passe ni de configuration de routeur.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

#: Modeles qui acceptent la reprise automatique par un autre modele quand le
#: premier decline (``fallbacks: "default"``).
_AVEC_REPRISE = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
BETA_REPRISE = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You are the support assistant of freeQoS, an out-of-band QoS and monitoring \
controller used by a wireless and fibre internet service provider (MikroTik \
routers, PPPoE and static-IP subscribers, Ubiquiti airMAX radios).

A support technician asks you a question, usually while a subscriber is on the \
phone. With the question you receive a JSON snapshot of what freeQoS measured: \
the subscriber record and last samples, latency (RTT median, jitter, loss), \
bufferbloat grade (latency added under load, A+ to F), QoE score, whether the \
subscriber hits their plan cap, churn/upgrade signals, radio health of the \
access points and CPEs (signal, SNR, CCQ, airtime), site capacity and \
headroom, and the state of the measurement cycles.

How to answer:
- Answer in the language of the question.
- Start with the most likely cause in one sentence, then the evidence: quote \
the measured numbers you relied on.
- Separate what the data shows from what you infer. If the data needed to \
decide is missing (probe off, no sample, cycle failing), say so plainly and \
say what to check or enable in freeQoS to get it.
- End with concrete next steps for the technician, most useful first \
(e.g. re-aim the CPE, move the subscriber to another sector, raise the plan, \
investigate the uplink, enable the RTT probe).
- Direction convention: "down" is towards the subscriber, "up" is from the \
subscriber.
- Be concise: a technician reads this during a call. Plain text with short \
bullet points; no tables.
"""


def enabled(settings: Any) -> bool:
    cle = getattr(settings, "anthropic_api_key", None)
    return bool(cle and cle.get_secret_value().strip())


def make_client(settings: Any) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value().strip(),
        timeout=180.0,
        max_retries=2,
    )


def build_prompt(question: str, context: dict[str, Any]) -> str:
    """La question, puis les mesures. ``sort_keys`` : un meme etat donne le
    meme texte, et le prefixe reste reutilisable d'un appel a l'autre."""
    donnees = json.dumps(context, default=str, sort_keys=True, ensure_ascii=False)
    return (
        f"<question>\n{question.strip()}\n</question>\n\n"
        f"<freeqos_measurements>\n{donnees}\n</freeqos_measurements>"
    )


async def ask(
    client: Any,
    *,
    model: str,
    question: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Un appel, une reponse. Les erreurs de l'API remontent telles quelles :
    l'appelant les traduit en statut HTTP."""
    parametres: dict[str, Any] = {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "output_config": {"effort": "medium"},
        "messages": [{"role": "user", "content": build_prompt(question, context)}],
    }
    if model in _AVEC_REPRISE:
        reponse = await client.beta.messages.create(
            betas=[BETA_REPRISE], fallbacks="default", **parametres
        )
    else:
        reponse = await client.messages.create(**parametres)

    texte = "\n\n".join(
        b.text for b in reponse.content if getattr(b, "type", None) == "text" and b.text
    ).strip()
    if reponse.stop_reason == "refusal":
        texte = texte or "The assistant declined to answer this question."
    elif reponse.stop_reason == "max_tokens":
        texte += "\n\n[answer cut short]"
    usage = getattr(reponse, "usage", None)
    return {
        "answer": texte,
        "model": getattr(reponse, "model", model),
        "stop_reason": reponse.stop_reason,
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
