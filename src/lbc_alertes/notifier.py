"""Notifications Telegram (API Bot, sans dépendance externe).

Le bot et le chat se configurent avec TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
(voir .env.example).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .models import Ad

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


def esc(text: str) -> str:
    """Échappe le texte pour le mode HTML de Telegram."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def human_age(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "date inconnue"
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:  # sécurité : date sans fuseau
        dt = dt.replace(tzinfo=timezone.utc)
    minutes = max(0, int((now - dt).total_seconds() // 60))
    if minutes < 60:
        return f"il y a {minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"il y a {hours} h"
    return f"il y a {hours // 24} j"


def format_price(ad: Ad) -> str:
    if ad.price is None:
        return "prix non précisé"
    if ad.price == 0:
        return "gratuit"
    return f"{ad.price:,}".replace(",", " ") + " €"


def build_message(search_name: str, ad: Ad, *, now: datetime | None = None) -> str:
    """Construit le message Telegram. ``now`` permet des tests déterministes."""
    if ad.publication_date:
        date_line = (
            f"🕐 publiée {esc(human_age(ad.publication_date, now))} "
            f"({ad.publication_date:%d/%m/%Y %H:%M})"
        )
    else:
        date_line = "🕐 date de publication inconnue"
    lines = [
        f"🆕 <b>{esc(ad.subject)}</b>",
        f"💰 {esc(format_price(ad))}",
        date_line,
        f"👤 {esc(ad.owner.label)}",
    ]
    if ad.owner.rating is not None:
        note = f"{ad.owner.rating:.1f}/5"
        if ad.owner.rating_count:
            note += f" ({ad.owner.rating_count} avis)"
        if ad.owner.pro_rates_link:
            note += f' · <a href="{esc(ad.owner.pro_rates_link)}">avis pro</a>'
        lines.append(f"⭐ {note}")
    elif ad.owner.pro_rates_link:
        lines.append(f'⭐ <a href="{esc(ad.owner.pro_rates_link)}">avis du vendeur pro</a>')
    else:
        # Beaucoup de vendeurs particuliers n'ont aucune note publiée : on le dit
        # explicitement plutôt que de laisser le champ vide.
        lines.append("⭐ note vendeur : non publiée")
    if ad.location:
        lines.append(f"📍 {esc(ad.location)}")
    lines.append(f"🔎 recherche : {esc(search_name)}")
    lines.append(f'<a href="{esc(ad.url)}">Voir l\'annonce</a>')
    return "\n".join(line for line in lines if line)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, parse_mode: str = "HTML",
                 send_photo: bool = True, timeout: float = 20.0):
        self.token = token
        self.chat_id = str(chat_id)
        self.parse_mode = parse_mode
        self.send_photo = send_photo
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, payload: dict) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "lbc-alertes/0.1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code == 429:
                try:
                    return {"retry_after": int(json.loads(body)["parameters"]["retry_after"])}
                except Exception:
                    return {"retry_after": 5}
            raise RuntimeError(f"Telegram {method} a échoué ({exc.code}) : {body[:300]}") from exc

    def send(self, text: str, photo_url: str | None = None) -> bool:
        if not self.configured:
            log.warning("Telegram non configuré : message non envoyé.")
            return False
        payload = {"chat_id": self.chat_id, "parse_mode": self.parse_mode,
                   "disable_web_page_preview": False}
        if self.send_photo and photo_url:
            attempt = {"photo": photo_url, "caption": text, **payload}
            res = self._call("sendPhoto", attempt)
            if res.get("ok"):
                return True
            if res.get("retry_after"):
                time.sleep(res["retry_after"])
                return self.send(text, None)
            log.info("sendPhoto refusé, envoi sans image.")
        res = self._call("sendMessage", {"text": text, **payload})
        if res.get("retry_after"):
            time.sleep(res["retry_after"])
            return self._call("sendMessage", {"text": text, **payload}).get("ok", False)
        return bool(res.get("ok"))

    def notify(self, search_name: str, ads: list[Ad], *,
               now: datetime | None = None) -> int:
        sent = 0
        for ad in ads:
            if self.send(build_message(search_name, ad, now=now), ad.thumbnail):
                sent += 1
            time.sleep(0.15)  # reste sous les limites de débit de Telegram
        return sent

    def test(self) -> bool:
        return self.send("✅ lbc-alertes : test d'envoi, la notification fonctionne.")
