# leboncoin-alertes

**Surveillez des recherches Leboncoin et recevez sur Telegram les nouvelles annonces qui correspondent à vos critères.** Le programme est écrit pour Python 3.11 ou supérieur et conserve l’historique localement dans SQLite.

## Comment ça marche

```text
config.yml + .env
       │
       ▼
Recherche HTML Leboncoin (/recherche?text=…)
       │  curl_cffi, empreinte TLS Chrome
       ▼
HTML rendu côté serveur ── extraction du JSON __NEXT_DATA__
       │
       ▼
Filtres locaux ── note vendeur si nécessaire ── déduplication SQLite
       │
       ▼
Notification Telegram des nouvelles annonces
```

L’API JSON interne de recherche (`/api/backui/search`, `POST api.leboncoin.fr/finder/search`) est protégée par DataDome et renvoie HTTP 403. Le programme utilise donc la page HTML normale `https://www.leboncoin.fr/recherche?text=…&category=…&price=MIN-MAX&page=N&limit=35`, puis extrait le JSON `__NEXT_DATA__` qu’elle contient. Le transport est `curl_cffi` avec `impersonate: chrome` ; `requests` et `urllib` simples sont également susceptibles d’être bloqués.

**À exécuter depuis une connexion résidentielle**, par exemple le PC de la maison. Leboncoin renvoie des 403 DataDome depuis les IP de datacenter : un VPS, un serveur cloud ou GitHub Actions ne convient pas.

Le paramètre de recherche est `text`, pas `keyword` (ignoré silencieusement). `category` et `price=MIN-MAX` sont pris en compte. Le tri `sort_by` est ignoré : les résultats restent ordonnés par pertinence et la première page peut contenir de vieilles annonces. Prévoyez donc une fenêtre d’âge généreuse et plusieurs pages. Les catégories `83` (« Accessoires informatique », notamment GPU et RAM) et `15` (« Ordinateurs ») sont utiles pour l’informatique.

## Installation

Dans PowerShell, depuis le répertoire du projet :

```powershell
cd C:\Users\cneuv\projects\leboncoin-alertes
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item config.example.yml config.yml
Copy-Item .env.example .env
```

Le paquet déclare le point d’entrée `lbc-alertes = lbc_alertes.cli:main`. Une fois installé, la commande `lbc-alertes` est disponible dans l’environnement virtuel. Tant que le paquet n’est pas installé, utilisez l’une de ces options :

```powershell
.\.venv\Scripts\python.exe -m lbc_alertes.cli once
```

ou définissez le chemin des sources avant d’invoquer le module :

```powershell
$env:PYTHONPATH = "C:\Users\cneuv\projects\leboncoin-alertes\src"
.\.venv\Scripts\python.exe -m lbc_alertes.cli once
```

La configuration est recherchée dans cet ordre : chemin donné avec `-c`, puis `config.yml`, `config.yaml` (et `config.json`, accepté comme YAML) dans le répertoire courant, puis les mêmes noms à côté du paquet installé. Un chemin relatif de `db_path` est résolu par rapport au fichier de configuration.

## Configuration

`config.example.yml` est un exemple complet ; `config.yml` est la configuration effectivement chargée. Les valeurs ci-dessous correspondent aux clés utilisées par le code.

| Clé | Portée | Rôle et défaut |
|---|---|---|
| `poll_interval_min` | globale | Minutes entre les cycles ; défaut `60` dans le code, `90` dans l’exemple. Gardez au moins `60` minutes. |
| `poll_jitter_min` | globale | Variation aléatoire ajoutée ou retranchée (minutes), défaut `15`. |
| `db_path` | globale | Fichier SQLite, défaut `data/state.db`, relatif au fichier de configuration. |
| `scraper.request_delay_min`, `request_delay_max` | scraper | Pause aléatoire entre requêtes, en secondes ; défauts `4.0` et `9.0`. |
| `scraper.max_retries` | scraper | Nombre maximal de tentatives de collecte, défaut `3`. |
| `scraper.backoff_base_min` | scraper | Base de temporisation en minutes après blocage/erreur, défaut `45`. |
| `scraper.impersonate` | scraper | Empreinte TLS simulée par `curl_cffi`, défaut `chrome`. |
| `scraper.playwright_fallback` | scraper | Autorise le repli navigateur, défaut `true`. Il nécessite l’extra Playwright et Chromium. |
| `scraper.seller_lookup` | scraper | Complète la fiche des vendeurs sans note publiée, défaut `true`. |
| `notify.parse_mode` | notifications | Mode Telegram, défaut `HTML`. |
| `notify.notify_on_first_run` | notifications | Notifier aussi les annonces déjà présentes au premier cycle de chaque recherche ; défaut `false`. |
| `searches` | liste | Recherches à surveiller. Chaque entrée doit avoir un `name` et au moins `text` ou `category`. |
| `searches[].text` | recherche | Mots-clés envoyés à Leboncoin via `text`. |
| `searches[].category` | recherche | Identifiant de catégorie, par exemple `83` ou `15`. |
| `searches[].price_min`, `price_max` | recherche | Bornes de prix envoyées au site. |
| `searches[].max_age_hours` | recherche | Âge maximal de publication en heures ; défaut `72`. Dates interprétées en heure de Paris. |
| `searches[].require`, `exclude` | recherche | Mots ou expressions requis/exclus, comparés sans tenir compte des accents. |
| `searches[].seller_types` | recherche | Types autorisés : `private` et/ou `pro` ; liste vide = les deux. |
| `searches[].min_rating` | recherche | Note vendeur minimale sur 5 ; une note inconnue n’est pas rejetée. |
| `searches[].pages` | recherche | Nombre maximal de pages analysées, défaut `1` ; chaque page contient jusqu’à 35 résultats. |
| `searches[].enums`, `locations` | recherche | Filtres additionnels transmis à la recherche Leboncoin. |

Exemple minimal raisonnable (complétez les identifiants Telegram dans `.env`) :

```yaml
poll_interval_min: 90
poll_jitter_min: 15
db_path: data/state.db

scraper:
  request_delay_min: 4.0
  request_delay_max: 9.0
  backoff_base_min: 45
  impersonate: chrome
  seller_lookup: true

notify:
  parse_mode: HTML
  notify_on_first_run: false

searches:
  - name: rtx 3090
    text: rtx 3090
    category: 83
    price_min: 200
    price_max: 800
    max_age_hours: 336
    require: [rtx, 3090]
    exclude: [3060, 3070, 4090, j'achète]
    seller_types: [private]
    min_rating: 3.5
    pages: 2
```

Les listes `require` et `exclude` sont recherchées sans distinction de casse ni d’accents dans le titre, la catégorie, le début du texte de l’annonce et les attributs pertinents. Tous les mots d’une expression doivent correspondre, dans n’importe quel ordre. Ajoutez `j'achète` à `exclude` pour écarter les annonces où quelqu’un cherche à acheter.

`-d/--data-dir` remplace entièrement `db_path` et utilise `<data-dir>\state.db`. Il ne conserve pas le sous-dossier `data\` du chemin configuré : cette option bascule donc vers un autre fichier de base.

## Telegram

1. Dans Telegram, créez un bot avec `@BotFather` et copiez son jeton.
2. Copiez `.env.example` en `.env`, puis renseignez `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID`. Ne publiez pas ces valeurs.
3. Pour obtenir un identifiant de chat privé, envoyez d’abord un message au bot, puis ouvrez `https://api.telegram.org/bot<TOKEN>/getUpdates` et relevez `message.chat.id`. Pour un groupe ou un canal, `@userinfobot` peut aider à trouver l’identifiant.
4. Lancez `lbc-alertes test` pour vérifier la configuration et envoyer le message de test.

Exemple de notification (rendu vérifié) :

```text
🆕 <b>Rtx 3090 fe</b>
💰 1 000 €
🕐 publiée il y a 10 j (27/10/2026 11:28)
👤 Nicolas · particulier
⭐ 5.0/5 (3 avis)
📍 Sucy-en-Brie 94370, Val-de-Marne
🔎 recherche : rtx 3090
<a href="https://www.leboncoin.fr/ad/accessoires_informatique/3274865343">Voir l'annonce</a>
```

Telegram limite les envois à environ 1 message par seconde par chat et 20 par minute dans un groupe. Le client traite les réponses HTTP 429 et leur paramètre `retry_after`.

## Exécution

Toutes les commandes peuvent être lancées avec l’exécutable installé dans `.venv\Scripts` ou avec le module Python :

```powershell
.\.venv\Scripts\python.exe -m lbc_alertes.cli once
.\.venv\Scripts\python.exe -m lbc_alertes.cli run
.\.venv\Scripts\python.exe -m lbc_alertes.cli test
.\.venv\Scripts\python.exe -m lbc_alertes.cli prune
```

| Commande | Fonction |
|---|---|
| `once` | Effectue un cycle de recherche puis quitte. |
| `run` | Lance le démon en continu ; c’est aussi la commande par défaut. `Ctrl+C` l’arrête proprement. |
| `test` | Affiche les recherches et la base SQLite, puis teste Telegram. |
| `prune` | Supprime de la base les annonces vues il y a plus de 45 jours. Le démon effectue aussi ce nettoyage après ses cycles. |

Options communes : `-c/--config CHEMIN`, `-d/--data-dir DOSSIER`, `-v/--verbose` et `--version`. Par exemple :

```powershell
.\.venv\Scripts\python.exe -m lbc_alertes.cli once -c C:\Users\cneuv\projects\leboncoin-alertes\config.yml -v
```

### Lancer au démarrage de Windows

Dans le **Planificateur de tâches**, créez une tâche déclenchée **À l’ouverture de session** (ou au démarrage, avec les droits nécessaires). Dans **Action > Démarrer un programme**, indiquez :

- **Programme/script** : `C:\Users\cneuv\projects\leboncoin-alertes\.venv\Scripts\python.exe`
- **Ajouter des arguments** : `-m lbc_alertes.cli run -c "C:\Users\cneuv\projects\leboncoin-alertes\config.yml"`
- **Démarrer dans** : `C:\Users\cneuv\projects\leboncoin-alertes`

Laissez la tâche s’exécuter tant qu’elle tourne. Le mode `run` gère lui-même l’intervalle et le jitter ; ne le déclenchez pas toutes les quelques minutes.

## Filtre de la note vendeur

Le score et le nombre d’avis figurent souvent gratuitement dans les attributs des résultats de recherche (`rating_score`, fraction de 0 à 1, et `rating_count`). Le programme convertit le score sur 5 : par exemple `0.99` devient `4.95/5`. Beaucoup de vendeurs particuliers n’ont toutefois aucun score publié. Une note absente reste **inconnue**, jamais égale à zéro, et `min_rating` ne rejette pas ces annonces.

Avec `scraper.seller_lookup: true`, les nouvelles annonces sans note peuvent être complétées via `GET https://api.leboncoin.fr/api/user-card/v2/<user_id>/infos`. La fiche peut également fournir taux de réponse, badges, date d’inscription et nombre d’annonces. Elle est mise en cache dans SQLite pendant 7 jours. Si aucune note n’est disponible, le message indique `⭐ note vendeur : non publiée`.

## Anti-blocage

Les réglages privilégient une cadence modérée : intervalle d’au moins **60 minutes** (défaut 90) avec jitter aléatoire, pause aléatoire de **4 à 9 secondes entre requêtes**, temporisation exponentielle à partir de **45 minutes** après détection d’un blocage ou d’un captcha, et **2 pages par recherche** dans l’exemple. Le validateur de configuration tolère techniquement des valeurs dès 30 minutes, mais il est fortement recommandé de ne jamais descendre sous 60 minutes. La pagination augmente le nombre de requêtes ; ne l’élargissez que selon vos besoins.

SQLite (`data/state.db`) mémorise les annonces dans `seen_ads`, avec la clé `(search, list_id)` : une annonce n’est notifiée qu’une seule fois par recherche, y compris après redémarrage. Au premier cycle d’une recherche, les annonces correspondantes sont enregistrées sans notification pour éviter un flot initial. `notify.notify_on_first_run: true` désactive cette précaution.

## Dépannage

| Symptôme | Explication / piste |
|---|---|
| HTTP 403 ou captcha | DataDome a bloqué la collecte. Vérifiez que vous êtes sur une IP résidentielle, conservez `impersonate: chrome`, réduisez la cadence ou les pages et laissez passer le délai exponentiel. N’essayez pas de relancer en boucle. |
| Recherche hors sujet ou filtre absent | Le paramètre à utiliser est `text`, jamais `keyword`. Le code vérifie que le site a bien appliqué texte, catégorie et prix. |
| Aucune annonce récente | Le tri par date n’est pas appliqué. Augmentez `max_age_hours` (par exemple `168` à `336`) et `pages` (plus de `1`) : la première page est classée par pertinence. |
| Fonctionne à la maison, pas sur VPS | Les IP de datacenter sont bloquées par DataDome. Utilisez un PC relié à une connexion résidentielle, pas un VPS, un cloud ou GitHub Actions. |
| Erreur de fuseau `Europe/Paris` sous Windows | `tzdata` est déclarée comme dépendance Windows. Vérifiez que le paquet est installé dans le même environnement virtuel que celui utilisé pour lancer l’application. |
| Première exécution sans message | Comportement attendu : les annonces existantes sont enregistrées sans notification. Pour l’autoriser, configurez `notify_on_first_run: true`. |

Les dates de publication sont interprétées avec `Europe/Paris` via `zoneinfo`. La dépendance `tzdata` est déclarée pour Windows, qui ne fournit pas toujours la base de fuseaux IANA.

## FAQ

**Pourquoi ne pas interroger directement l’API ?** Son endpoint interne de recherche répond 403 sous DataDome ; les résultats sont lus dans le JSON inclus dans la page HTML rendue par Leboncoin.

**Une annonce sans note est-elle exclue par `min_rating` ?** Non. Une note inconnue est conservée ; le filtre ne rejette que les notes connues inférieures au seuil.

**Comment éviter les annonces de recherche/achat ?** Ajoutez `j'achète` dans `exclude` ; la comparaison ignore les accents.

**Puis-je démarrer plusieurs instances ?** Évitez-le : elles peuvent multiplier les requêtes. En outre, le fichier SQLite est destiné à conserver l’état d’une seule instance.

## Crédits

- [`etienne-hd/lbc`](https://github.com/etienne-hd/lbc) — PyPI `lbc`, licence MIT ; a permis de valider le transport `curl_cffi`.
- [`kuaterpillar/lbc`](https://github.com/kuaterpillar/lbc) — correspondance des champs de note vendeur de la fiche utilisateur.
- [`AzurCover/pokemon-tracker`](https://github.com/AzurCover/pokemon-tracker) — documentation du problème de blocage des IP de datacenter.

Respectez les conditions d’utilisation de Leboncoin et restez raisonnable dans la fréquence des requêtes. Cet outil est destiné à une veille personnelle, sans contournement des protections ni sollicitation intensive du service.
