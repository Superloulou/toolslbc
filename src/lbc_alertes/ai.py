"""Analyse des annonces par un serveur IA compatible OpenAI.

Le serveur est local (llama.cpp, Ollama, LM Studio...) : il suffit d'une URL de
base et, éventuellement, d'une clé d'API.  L'analyse demande un verdict binaire
(« retenu / écarté ») accompagné d'une raison, en JSON strict.

Variables d'environnement :
    LBC_AI_BASE_URL   exemple http://192.168.1.172:8081/v1
    LBC_AI_API_KEY    clé Bearer (optionnelle si le serveur est ouvert)
    LBC_AI_MODEL      identifiant du modèle (sinon le premier du serveur)
    LBC_AI_ENABLED    0/false/n pour désactiver complètement
    LBC_AI_AUTO       0/false/n pour désactiver l'analyse automatique
    LBC_AI_TIMEOUT    secondes, 60 par défaut
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("lbc_alertes.ai")

_TRUE = {"1", "true", "yes", "oui", "on", "vrai"}
_FALSE = {"0", "false", "no", "non", "off", "faux"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return default


@dataclass(slots=True)
class AIConfig:
    """Configuration du serveur IA."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = True
    auto: bool = True
    timeout: float = 60.0
    max_tokens: int = 900
    temperature: float = 0.0

    @classmethod
    def from_env(cls) -> "AIConfig":
        base = os.environ.get("LBC_AI_BASE_URL", "").strip().rstrip("/")
        return cls(
            base_url=base,
            api_key=os.environ.get("LBC_AI_API_KEY", "").strip(),
            model=os.environ.get("LBC_AI_MODEL", "").strip(),
            enabled=_env_bool("LBC_AI_ENABLED", bool(base)),
            auto=_env_bool("LBC_AI_AUTO", True),
            timeout=float(os.environ.get("LBC_AI_TIMEOUT", "60") or 60),
            max_tokens=int(os.environ.get("LBC_AI_MAX_TOKENS", "900") or 900),
        )

    @property
    def usable(self) -> bool:
        """Vrai si l'IA peut réellement être interrogée."""
        return self.enabled and bool(self.base_url)

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


@dataclass(slots=True)
class Verdict:
    """Réponse de l'IA pour une annonce."""

    retained: bool | None
    reason: str
    raw: str = ""
    error: str = ""

    @property
    def verdict(self) -> str | None:
        if self.retained is None:
            return None
        return "retenu" if self.retained else "rejete"


SYSTEME = (
    "Tu analyses des annonces Leboncoin pour un particulier qui cherche à acheter "
    "d'occasion. Tu dois décider si l'annonce mérite d'être retenue (montrée) ou "
    "écartée. Sois strict et logique : une annonce hors sujet, trompeuse, "
    "incomplète, trop chère par rapport aux consignes, ou d'un vendeur "
    "suspect doit être écartée. Tu réponds UNIQUEMENT par un objet JSON, sans "
    "texte autour, de la forme : {\"retenu\": true, \"raison\": \"phrase courte\"}."
)


def _format_prix(price: Any) -> str:
    try:
        cents = int(price)
    except (TypeError, ValueError):
        return "prix inconnu"
    if cents <= 0:
        return "prix non précisé"
    euros = cents / 100
    if euros >= 1000:
        return f"{euros:,.0f} €".replace(",", " ")
    return f"{euros:.0f} €"


def build_prompt(ad: dict[str, Any], directives: dict[str, str]) -> str:
    """Construit la question envoyée au modèle pour une annonce."""
    consignes = []
    global_rule = directives.get("global", "").strip()
    if global_rule:
        consignes.append(f"Consignes générales : {global_rule}")
    own = directives.get(ad.get("search") or "", "").strip()
    if own:
        consignes.append(f"Consignes pour la recherche « {ad.get('search')} » : {own}")
    if not consignes:
        consignes.append(
            "Aucune consigne précise : juge selon le bon sens (pertinence par "
            "rapport à la recherche, prix cohérent, annonce crédible)."
        )

    body = (ad.get("body") or "").strip()
    if len(body) > 1200:
        body = body[:1200] + "…"

    note = ad.get("rating")
    note_txt = "non renseignée"
    if note is not None:
        note_txt = f"{note}/5 ({ad.get('rating_count') or 0} avis)"

    lignes = [
        f"Recherche : {ad.get('search')}",
        f"Titre : {ad.get('subject') or '(sans titre)'}",
        f"Prix : {_format_prix(ad.get('price'))}",
        f"Description : {body or '(absente)'}",
        f"Vendeur : {ad.get('owner') or 'inconnu'}",
        f"Note du vendeur : {note_txt}",
        f"Localisation : {ad.get('location') or 'inconnue'}",
        f"Publiée le : {ad.get('published') or ad.get('first_seen') or 'inconnue'}",
    ]
    return (
        "\n".join(consignes)
        + "\n\nAnnonce à analyser :\n"
        + "\n".join(lignes)
        + "\n\nRéponds par le JSON demandé."
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse(text: str) -> tuple[bool | None, str]:
    """Extrait (retenu, raison) d'une réponse du modèle, même mal formatée."""
    if not text:
        return None, ""
    match = _JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            raw = data.get("retenu", data.get("retient", data.get("keep")))
            if isinstance(raw, str):
                raw = raw.strip().lower() in _TRUE
            reason = str(data.get("raison") or data.get("reason") or "").strip()
            if raw is None:
                return None, reason
            return bool(raw), reason
    # Repli : on cherche un mot-clé dans le texte.
    low = text.lower()
    if "retenu" in low or "\"keep\": true" in low:
        return True, text.strip()[:200]
    if "écart" in low or "rejete" in low or "retenu\": false" in low:
        return False, text.strip()[:200]
    return None, text.strip()[:200]


class Analyzer:
    """Client minimal pour un serveur compatible OpenAI (/v1/chat/completions)."""

    def __init__(self, config: AIConfig | None = None) -> None:
        self.config = config or AIConfig.from_env()

    # --- transport ---------------------------------------------------------
    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        req = urllib.request.Request(self.config.url(path), data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        if not raw.strip():
            return {}
        return json.loads(raw)

    # --- informations ------------------------------------------------------
    def models(self) -> list[str]:
        """Identifiants des modèles servis (vide si le serveur ne répond pas)."""
        try:
            payload = self._request("/models")
        except Exception as exc:  # noqa: BLE001
            log.warning("IA : /models indisponible : %s", exc)
            return []
        out: list[str] = []
        for item in payload.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                out.append(str(item["id"]))
        return out

    def resolve_model(self) -> str:
        """Modèle à utiliser : celui configuré, sinon le premier disponible."""
        if self.config.model:
            return self.config.model
        found = self.models()
        if found:
            self.config.model = found[0]
            log.info("IA : modèle détecté : %s", self.config.model)
        return self.config.model

    def reachable(self) -> bool:
        return bool(self.models())

    def ping(self) -> str:
        """Vérifie que le serveur répond à une vraie conversation."""
        reply = self._chat(
            "Réponds uniquement par le mot pong.",
            # Les modèles à raisonnement consomment des jetons avant de répondre.
            max_tokens=400,
        )
        return (reply or "").strip()

    # --- analyse -----------------------------------------------------------
    def _chat(
        self,
        prompt: str,
        *,
        system: str = SYSTEME,
        max_tokens: int | None = None,
    ) -> str:
        model = self.resolve_model()
        payload = {
            "model": model or "default",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            # Les modèles à raisonnement ont besoin d'une grande fenêtre : avec
            # 60 jetons la réponse est tronquée avant la fin du JSON.
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": False,
        }
        data = self._request("/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        text = message.get("content")
        if not text:
            # Certains serveurs mettent la réponse dans reasoning_content.
            text = message.get("reasoning_content") or ""
        return str(text)

    def analyze(self, ad: dict[str, Any], directives: dict[str, str]) -> Verdict:
        """Demande un verdict sur une annonce (dict issu de Store.get_ad)."""
        if not self.config.usable:
            return Verdict(None, "", error="IA non configurée")
        prompt = build_prompt(ad, directives)
        try:
            raw = self._chat(prompt)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200] if exc.fp else ""
            log.error("IA : erreur HTTP %s : %s", exc.code, detail)
            return Verdict(None, "", error=f"HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            log.error("IA : interrogation impossible : %s", exc)
            return Verdict(None, "", error=str(exc))

        retained, reason = _parse(raw)
        if retained is None:
            log.warning("IA : réponse inexploitable : %s", raw[:200])
            return Verdict(None, reason, raw, error="réponse inexploitable")
        return Verdict(retained, reason or ("annonce retenue" if retained else "annonce écartée"), raw)


def screen_ads(
    store: Any,
    analyzer: Analyzer,
    search: str | None = None,
    *,
    limit: int = 50,
) -> tuple[int, int]:
    """Analyse les annonces sans verdict. Retourne (analysées, retenues)."""
    directives = store.get_directives()
    pending = store.pending(search, limit=limit)
    analyzed = 0
    retained = 0
    for item in pending:
        ad = store.get_ad(item["search"], item["list_id"])
        if ad is None:
            continue
        verdict = analyzer.analyze(ad, directives)
        if verdict.verdict is None:
            log.warning(
                "IA : pas de verdict pour %s (%s)",
                item["list_id"],
                verdict.error or verdict.reason,
            )
            continue
        store.set_verdict(
            item["search"], item["list_id"], verdict.verdict, verdict.reason, "ia"
        )
        analyzed += 1
        if verdict.retained:
            retained += 1
    return analyzed, retained
