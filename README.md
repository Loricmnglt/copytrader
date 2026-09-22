# copytrader

Outil de **copy-trading paper-first** pour suivre 4 wallets EVM et copier leurs
achats/ventes de tokens — avec gestion du risque, filtrage anti-rug, et
journalisation complète. **Par défaut il ne dépense aucun argent réel** : il
simule les trades contre les prix et la liquidité réels du marché pour
*mesurer* si suivre ces wallets a un edge, avant tout capital.

> ⚠️ **Lis d'abord `EDGE.md`** (verdict honnête sur la faisabilité).

---

## ⭐ Cas d'usage réel : Hyperliquid (tes 4 wallets)

**Découverte importante :** ces 4 wallets ne tradent pas des memecoins spot —
ce sont des **traders de perpétuels sur Hyperliquid** (comptes $290k–870k,
historiquement rentables). Le copieur *spot* ci-dessous ne peut pas les voir.
Utilise donc le copieur **Hyperliquid**, qui suit leurs positions perp en temps
réel via l'API et les copie en mirroring mis à l'échelle de ton budget.

```bash
python -m copytrader hl-doctor     # positions live des wallets + connexion API
python -m copytrader hl-analyze    # leur P&L / win-rate réel -> poids de copie
python -m copytrader hl-paper      # SIMULATION (aucun argent) — laisse tourner
python -m copytrader hl-report     # bilan + export CSV (hl_reports\)
# hl-live  = réel, multi-verrous (voir plus bas)
```

Comment ça copie : pour chaque coin, on somme la conviction directionnelle des
wallets (taille de leur position / valeur de leur compte, × leur poids), on
met à l'échelle de ton budget, on plafonne le levier brut (défaut 2x) et par
coin, puis on rééquilibre notre livre vers cette cible (ouvre/ajuste/ferme). Si
un wallet ferme, la cible tombe → on sort. Frais taker + slippage + funding
sont simulés ; positions/état sauvegardés (reprise après redémarrage).

Réglages dans `[hyperliquid]` de `config.toml`. Détails et caveats : `EDGE.md`.

> Le reste de ce document décrit le copieur **spot** d'origine (memecoins EVM),
> conservé mais **non adapté à ces wallets-ci**.

---

## 1. Architecture

```
                 ┌─────────────┐   trades bruts   ┌──────────────┐
  RPC publics →  │   monitor   │ ───────────────► │   signals    │
  (multi-chain)  │ (getLogs)   │                  │ (scoring +   │
                 └─────────────┘                  │  filtres)    │
                        ▲                          └──────┬───────┘
                        │ prix/liquidité                  │ décisions
                 ┌──────┴──────┐                   ┌──────▼───────┐
                 │  prices     │ ◄──────────────── │    risk      │
                 │ DexScreener │   sizing/plafonds  │  (capital)   │
                 │ honeypot.is │                   └──────┬───────┘
                 └─────────────┘                          │ ordres
                                                   ┌───────▼────────┐
                                          paper ── │   executor     │ ── live
                                        (simulé)   │ fills+slippage │  (gaté)
                                                   └───────┬────────┘
                                                           │
                                                   ┌───────▼────────┐
                                                   │   ledger        │
                                                   │ SQLite + CSV    │
                                                   └────────────────┘
```

Chaque module est isolé et testable :

| Module | Rôle |
|---|---|
| `monitor.py` | Détecte les swaps des wallets via les events `Transfer` ERC-20 (multi-chaînes). |
| `prices.py` | Prix USD + liquidité (DexScreener), sécurité token (honeypot.is), métadonnées ERC-20. |
| `analyzer.py` | Reconstruit l'historique des wallets, calcule P&L/win-rate → **poids** de copie. |
| `signals.py` | Score chaque signal (poids wallet, confluence, fraîcheur, marché), filtre. |
| `risk.py` | Sizing dynamique, plafonds, TP/SL/trailing/timeout, halte journalière. |
| `executor_paper.py` | Simule les fills avec slippage/frais/latence **réels**. |
| `executor_live.py` | Exécution réelle — **désactivée par défaut**, isolée, multi-verrous. |
| `ledger.py` | Journalise signaux/fills/trades/équité en SQLite + export CSV. |
| `engine.py` | Boucle qui relie le tout. |

## 2. Technologies

- **Python 3.11+**. Le cœur (`doctor`/`analyze`/`paper`/`report`) est
  **100 % stdlib** — aucun `pip install` requis (`urllib`, `json`, `sqlite3`,
  `tomllib`).
- Données : **JSON-RPC** sur RPC publics (failover), **DexScreener** (prix/
  liquidité, gratuit), **honeypot.is** (taxes/honeypot, gratuit),
  **Etherscan V2** (analyse d'historique, clé gratuite, optionnelle).
- Seul le mode **live** importe `web3` + `eth-account` (chargés à la volée).

## 3. Comment les transactions sont détectées

Pas besoin d'ABI de router. Pour chaque wallet suivi, on récupère les events
`Transfer` ERC-20 où le wallet est **émetteur ou destinataire**, on les groupe
par transaction, et on en déduit le swap :

- wallet **reçoit** le token X **+ envoie** un actif de base (WETH/USDC/ETH) → **ACHAT** de X
- wallet **envoie** le token X **+ reçoit** un actif de base → **VENTE** de X

La jambe en ETH natif (sans event `Transfer`) est lue depuis `value` de la tx ;
le gas depuis le reçu. Ça capte les swaps sur **n'importe quel** DEX/aggregator.

## 4. Comment les trades sont exécutés

- **Paper (défaut)** : fill au prix DexScreener *courant* (donc déjà plus tard
  que l'entrée du wallet = la vraie pénalité « on arrive en retard »), avec un
  modèle de **slippage** (impact = fonction de la taille vs liquidité on-chain,
  facteur calibrable), les **frais de pool** et le **gas en conditions réelles**
  (`eth_gasPrice` live × unités de swap × prix natif, rafraîchi en continu ;
  repli sur une estimation statique si le RPC ne répond pas). La latence est
  enregistrée.
- **Live (gaté)** : quote via l'aggregator **Odos**, signature locale, envoi via
  ton propre RPC. Voir §7 et les verrous.

## 5. Logique de gestion du risque

- **Réserve** de poudre sèche (jamais 100 % déployé).
- **Sizing par conviction** : `base_alloc_pct × budget × (0.5 + conviction)`.
- **Plafonds durs** : par position, par token, nombre de positions simultanées.
- **Sorties** : take-profit, stop-loss, trailing stop, timeout, + **miroir des
  ventes** (si un wallet suivi vend, on sort).
- **Cooldown** par token après une sortie (évite de racheter un dump).
- **Coupe-circuit journalier** : stop des nouvelles entrées si la journée est
  trop dans le rouge.
- **Anti-token-pourri** : liquidité/volume minimum, âge de la paire, check
  honeypot/taxe, drift de prix maximal (skip si trop tard).

## 6. Paramètres modifiables (`config.toml`)

Tout se règle dans `config.toml` (copie de `config.example.toml`) — aucun code
à toucher. Sections : `[risk]` (budget, %, TP/SL…), `[signal]` (filtres,
conviction, confluence), `[execution]` (latence, gas, slippage),
`[[wallets]]` (adresses, poids, on/off), `[live]` (verrous réels). Voir les
commentaires du fichier.

## 7. Lancer en test/simulation d'abord

```bash
# 1) préparer la config
cp config.example.toml config.toml     # (déjà fait : tes 4 wallets sont dedans)

# 2) vérifier la connectivité
python -m copytrader doctor

# 3) (optionnel mais recommandé) analyser les wallets -> poids
#    nécessite une clé Etherscan gratuite (couvre toutes les chaînes) :
setx ETHERSCAN_API_KEY "TA_CLE"        # Windows ; ouvre un nouveau terminal
python -m copytrader analyze

# 4) SIMULATION (aucun argent réel) — laisse tourner
python -m copytrader paper

# 5) à tout moment, le bilan
python -m copytrader report            # + export CSV dans reports/
```

**Tourner en continu (recommandé pour plusieurs jours / le test 24 h)** :
utilise le lanceur auto-restart au lieu de garder un terminal ouvert. Il
relance le bot s'il s'arrête/plante, et l'état (positions ouvertes, cash) est
sauvegardé dans `state_<mode>.json` et **rechargé automatiquement** à chaque
redémarrage — donc rien n'est perdu si le PC ou le bot redémarre.

```bash
# double-clique run.bat, OU depuis PowerShell :
.\run.ps1
```

Les logs sont ajoutés à `copytrader.out.log`. Pour arrêter : ferme la fenêtre.

Pour le **test réel de 100 CHF / 24 h** (seulement après un paper concluant) :

```bash
# 0) vérifier la plomberie live depuis TA machine (RPC + agrégateur),
#    sans clé ni fonds :
python -m copytrader livecheck
```

1. `pip install -r requirements.txt` (web3, eth-account).
2. Dans `config.toml` → `[live] enabled = true`, garde `dry_run_first = true`.
3. **Finance un wallet dédié** avec du **WETH** (le capital de trading, base des
   deux jambes) + un peu d'**ETH natif** pour le gas. Mets sa clé privée dans
   une variable d'environnement, jamais dans un fichier :
   `setx COPYTRADER_PK "0x..."` (rouvre un terminal).
4. `set COPYTRADER_ARM=1` puis `python -m copytrader live --yes`.
   Tant que `dry_run_first = true`, **rien n'est envoyé** (dry-run réaliste).
5. Une fois le dry-run observé, passe `dry_run_first = false` pour l'exécution
   réelle, en commençant par des plafonds minuscules.

## Sécurité de la clé privée

La clé n'est lue **qu'une seule fois**, dans `executor_live.py`, depuis la
variable d'environnement `COPYTRADER_PK`. Elle n'est **jamais** écrite dans la
config, la base, les CSV ni aucun log. Utilise un **wallet dédié** financé avec
seulement le budget du test.

## Limitations connues (honnêteté)

- **`executor_live` : comptabilité validée, envoi réel non éprouvé.** Le flux
  complet est implémenté — quote Odos, approvals ERC-20, envoi de tx signée,
  **parsing des reçus** (la quantité exacte reçue est lue depuis les events
  `Transfer` on-chain), comptabilité et journalisation. Le **parsing des reçus
  est testé sur de vrais swaps Arbitrum** (`parsed == on-chain`). En revanche
  l'**envoi de transaction bout-en-bout avec de vrais fonds n'a pas été exécuté
  par l'auteur**, et l'**API agrégateur n'a pas pu être jointe depuis
  l'environnement de build** (Cloudflare bloque les IP datacenter). D'où
  `python -m copytrader livecheck` : lance-le depuis ta machine pour confirmer
  que RPC + agrégateur répondent avant d'armer. Commence avec des montants
  minuscules.
- **Filtre anti-bruit** : dépôts/retraits de lending (aTokens), wrapping, LP
  ressemblent à des swaps. Ils sont maintenant filtrés dans le monitor (règle
  structurelle « ~1:1 vs WETH » + denylist de symboles configurable dans
  `[signal].ignore_token_symbol_patterns`), en plus du garde-fou DexScreener du
  moteur de signaux.
- **Reprise d'état** : l'état est repris depuis `state_<mode>.json`. Si tu
  changes le budget ou la config de façon importante entre deux runs, **supprime
  ce fichier** pour repartir propre (le cash repris refléterait l'ancien
  budget).
- **P&L de l'analyzer approximatif** : les jambes en WETH sont valorisées au
  prix natif *courant* (pas de FX historique). Bon pour classer les wallets,
  pas pour une compta exacte.
- **Latence** : polling (défaut ~3 s). C'est *modélisé et mesuré*, pas éliminé.
  Les chaînes sont désormais pollées **en parallèle** (threads) et tu peux
  brancher des **RPC privés via `${ENV}`** + baisser `poll_interval_s` pour
  réduire (sans supprimer) le retard structurel.
```

Voir `EDGE.md` pour le verdict de faisabilité.
