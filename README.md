# Snow — bots de trading (actions US et crypto)

Des bots qui tradent sur **Alpaca** (actions US et crypto, même API). Les décisions viennent de **règles
quantitatives testables** ; un **LLM (TradingAgents)** peut, en option, mettre un veto sur les nouvelles
entrées. Tout démarre en **paper trading** (argent fictif).

```
            ┌──────────────┐   barres   ┌────────────┐  cible 0..1  ┌──────────────┐  ordres  ┌─────────┐
 Alpaca ───▶│ data.py      │──────────▶│ stratégie  │─────────────▶│ risk.py      │────────▶│ broker  │
 Yahoo  ───▶│ (live/backt.)│            │ (règles)   │              │ taille,      │         │ Alpaca  │
            └──────────────┘            └────────────┘              │ limites,     │         │ ou Sim  │
                                                         veto LLM ─▶│ cash         │         └─────────┘
                                                      (entrées)     └──────────────┘
```

Le backtest et le bot live passent par **le même code** (stratégie → `plan_orders` → ordres) :
ce que tu testes est ce qui tournera.

## Installation

Python 3.11 ou plus récent.

```bash
git clone https://github.com/longhithibaut-lgtm/Snow.git
cd Snow
python -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml
cp .env.example .env
```

### Compte Alpaca (paper)

1. Crée un compte sur [alpaca.markets](https://alpaca.markets) (le paper trading est gratuit).
2. Dans le tableau de bord, passe sur **Paper Trading** → **API Keys** → génère une clé.
3. Mets `ALPACA_API_KEY` et `ALPACA_SECRET_KEY` dans `.env`.

Les données crypto d'Alpaca sont publiques ; les actions demandent les clés (flux IEX gratuit).
Pour trader en réel depuis la France, vérifie que ton type de compte Alpaca donne accès aux actions
et/ou à la crypto (cela dépend du pays).

## Les commandes

```bash
snow backtest --start 2019-01-01 --split 2023-01-01   # tester les bots sur l'historique
snow run --dry-run                                    # calculer les ordres sans les envoyer
snow run                                              # une passe en paper trading
snow run --loop --every 60                            # tourner en continu, une passe par heure
snow status                                           # compte et positions par bot
snow stop / snow resume                               # coupe-circuit : plus aucun ordre / reprise
snow flatten --bot crypto-trend --yes                 # tout vendre pour un bot
```

`--bot NOM` limite une commande à un bot. `-c autre.yaml` change de fichier de config.

## Configurer les bots (`config.yaml`)

Chaque bot a ses symboles, sa stratégie, sa part du compte (`capital_pct`) et ses limites de risque.
Un symbole appartient à un seul bot, et la somme des `capital_pct` ne dépasse pas 1.

| Stratégie | Idée | Paramètres |
|---|---|---|
| `sma_cross` | Tendance : long tant que la moyenne courte est au-dessus de la longue | `fast`, `slow` |
| `rsi_reversion` | Achat des replis (RSI bas) au-dessus de la tendance, sortie au rebond | `period`, `entry`, `exit`, `trend` |
| `donchian_breakout` | Achat d'une cassure du plus haut sur N barres, sortie sous le plus bas sur M | `entry`, `exit` |

Ajouter une stratégie : une classe dans `snow/strategies.py` avec une méthode `signals(bars)` qui
renvoie une exposition cible entre 0 et 1 pour chaque barre, sans regarder les barres suivantes
(un test le vérifie pour chaque stratégie), puis l'ajouter à `STRATEGIES`.

### Garde-fous

| Réglage (`risk:`) | Effet |
|---|---|
| `max_position_pct` | Part maximale du budget du bot dans un symbole (défaut : 1 / nombre de symboles) |
| `max_gross_exposure` | Exposition totale maximale (≤ 1 : pas de levier) |
| `daily_loss_limit_pct` | Si le compte a perdu ce pourcentage depuis la clôture de la veille : plus aucun achat ce jour-là |
| `rebalance_threshold_pct`, `min_order_value` | Pas d'ordre pour un petit écart (évite de payer des frais pour rien) |
| `vol_target_annual` | Réduit la position quand la volatilité dépasse la cible |
| `cash_buffer_pct` | Cash gardé de côté pour les frais et les écarts de prix |

Par ailleurs :

- Les **sorties ne sont jamais bloquées** : ni par les seuils, ni par la limite de perte, ni par le LLM.
- Une **panne de données** sur un symbole le laisse tel quel : jamais de vente sur une donnée manquante.
- Un symbole qui a **un ordre déjà ouvert** n'en reçoit pas d'autre : relancer le bot ne double pas les ordres.
- La **barre en cours** n'est jamais utilisée : un signal ne change que sur une barre close.
- **`snow stop`** crée `state/STOP` : plus aucun ordre tant que `snow resume` n'est pas lancé.
- Le **réel** demande `--live`, des clés distinctes (`ALPACA_LIVE_*`) **et** `SNOW_LIVE_CONFIRM=yes` dans
  l'environnement. Sans les trois, c'est du paper.
- Chaque ordre est noté dans `logs/trades.csv`, le détail des passes dans `logs/snow.log`.
- Alertes optionnelles sur Telegram ou Discord (voir `.env.example`) à chaque ordre, erreur ou limite atteinte.

## Le backtest

```bash
snow backtest --start 2019-01-01 --split 2023-01-01
```

- Données Yahoo Finance (historique long et gratuit), barres ajustées des dividendes et splits.
- Un ordre décidé à la clôture d'une barre est exécuté **à l'ouverture de la barre suivante**, avec frais
  (`fee_pct_crypto`, `fee_pct_stock`) et glissement (`slippage_pct`).
- Comparaison à un **buy & hold** à poids égaux sur les mêmes symboles, frais compris.
- `--split` sépare les résultats avant et après une date : choisis tes paramètres sur la première
  période, puis juge-les **uniquement** sur la seconde.
- La courbe et les ordres sont enregistrés dans `logs/backtests/`.
- Le filtre LLM n'est **pas** utilisé en backtest : un modèle interrogé sur une date passée connaît
  peut-être déjà la suite, ce qui gonflerait les résultats.

Résultats de la config d'exemple (2019 → octobre 2026, avec frais) :

| Bot | Rendement annuel | Sharpe | Pire baisse | Buy & hold : rendement / Sharpe / pire baisse |
|---|---|---|---|---|
| `crypto-trend` (BTC, ETH) | ≈ +30 %/an | ≈ 1,0 | ≈ −41 % | +45 %/an / 0,9 / −77 % |
| `us-dips` (SPY, QQQ, IWM) | ≈ +3,5 %/an | ≈ 0,6 | ≈ −11 % | +17 %/an / 0,9 / −34 % |

En clair : ces stratégies **réduisent fortement les pertes maximales mais rapportent moins** que de
simplement détenir les actifs sur cette période. Ce sont des points de départ, pas des stratégies
gagnantes. Ne les optimise pas jusqu'à ce que le backtest soit beau : c'est la façon la plus sûre
d'obtenir une stratégie qui échoue en réel.

## Le faire tourner sur ton ordinateur

Les bots d'exemple travaillent sur des barres **journalières**. Il suffit donc de les lancer
régulièrement, et relancer le bot sans raison ne fait rien de plus. Deux options :

**1. Une fenêtre de terminal qui reste ouverte**

```bash
snow run --loop --every 60
```

**2. Une tâche planifiée** (le bot tourne même si tu n'y penses pas, tant que l'ordinateur est allumé)

- macOS / Linux : `crontab -e`, puis par exemple une passe par heure :
  ```
  5 * * * * cd /chemin/vers/Snow && .venv/bin/snow run >> logs/cron.log 2>&1
  ```
- Windows : Planificateur de tâches → Créer une tâche → déclencheur toutes les heures → action
  `C:\chemin\vers\Snow\.venv\Scripts\snow.exe` avec l'argument `run` et « Démarrer dans »
  `C:\chemin\vers\Snow`.

Les crypto changent de barre à 00:00 UTC (2 h ou 1 h du matin en France). Pour les actions, un ordre
passé après la clôture de New York (22 h heure de Paris) part à l'ouverture suivante, exactement comme
dans le backtest.

Si l'ordinateur est éteint, rien ne se passe : pas d'ordre, mais pas de sortie non plus. Pour des
positions crypto ouvertes le week-end, c'est un risque à connaître. Un petit serveur (VPS) règle ce
problème le jour où tu en as besoin.

## Le filtre LLM (TradingAgents)

```bash
pip install -e ".[llm]"
```

Puis, dans le bot, `llm_filter.enabled: true` avec le fournisseur et les modèles dans
`tradingagents_config` (et la clé API correspondante dans `.env`). Avant **chaque nouvelle entrée**,
TradingAgents analyse le symbole. Une note listée dans `veto_ratings` (par défaut `Sell` et
`Underweight`) annule l'achat. La réponse est gardée pour la journée.

Ce qu'il faut savoir :

- Une analyse coûte plusieurs dizaines d'appels LLM, donc de l'ordre de quelques dizaines de centimes à
  quelques dollars selon les modèles.
- Son effet ne peut **pas** être mesuré en backtest (voir plus haut). Pour savoir s'il aide, fais tourner
  deux bots en paper, l'un avec le filtre et l'autre sans, sur des symboles comparables, et compare
  après plusieurs mois.
- En cas d'erreur (clé manquante, panne), `on_error: block` saute l'entrée.

## Passer en réel : la checklist

1. Au moins **2 à 3 mois de paper trading** sans incident, avec des résultats proches du backtest.
2. Commencer avec un **montant que tu acceptes de perdre**.
3. Des clés **live** séparées (`ALPACA_LIVE_API_KEY`, `ALPACA_LIVE_SECRET_KEY`).
4. `SNOW_LIVE_CONFIRM=yes` dans `.env`, puis `snow run --live`.
5. Vérifier `snow status --live` et `logs/trades.csv` chaque jour au début.
6. En cas de doute : `snow stop`, puis `snow flatten --bot … --live --yes` si besoin.

## Tests

```bash
pytest
```

Les tests couvrent l'absence de regard vers le futur dans les stratégies, le dimensionnement et les
limites de risque, l'exécution du backtest à l'ouverture suivante, la non-duplication des ordres, le
coupe-circuit, le veto LLM et la protection du mode réel.

*Ce projet est un outil personnel. Ce n'est pas un conseil en investissement.*
