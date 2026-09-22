# Verdict de faisabilité — à lire avant de risquer de l'argent

## Révélation majeure : ce sont des traders de perps Hyperliquid

Le diagnostic initial (« wallets memecoin spot ») était **faux**, et l'outil de
copy spot n'a jamais rien vu (0 signal en 20 h) parce que l'activité est
ailleurs. L'API Hyperliquid le confirme : ces 4 wallets tradent des
**perpétuels à effet de levier** sur Hyperliquid, avec des comptes à 6 chiffres.
Leurs wallets EVM paraissaient vides parce que le capital est *dans* Hyperliquid.

Analyse de leur historique récent (commande `hl-analyze`) :

| Wallet | Compte | P&L net (récent) | Win-rate | Poids attribué |
|---|---:|---:|---:|---:|
| wallet-1 `0xf97a` | ~$297k | **+$70'000** | 77% | 1.45 |
| wallet-4 `0x95da` | ~$872k | **+$177'000** | 87% | 1.40 |
| wallet-2 `0xac14` | ~$286k | **+$50'000** | 70% | 1.12 |
| wallet-3 `0xd135` | $0 (retiré) | +$288'000 | 65% | 0.40 |

Ce sont donc de **vrais traders rentables** — une bien meilleure base de
copy-trading que des memecoins.

## Pourquoi Hyperliquid est bien plus favorable que le memecoin spot

- Positions et fills lisibles **en temps réel** via l'API (pas de reconstruction).
- **Pas de honeypot, pas de rug, liquidité profonde, spreads serrés** → les
  pires pièges du memecoin disparaissent.
- Exécution par ordres signés via la même API ; latence basse possible.

## Les caveats honnêtes (le copy de perps n'est PAS gratuit)

1. **Tu restes en retard** : tu copies après qu'ils ont pris leur position, donc
   à un prix un peu moins bon. Sur des mouvements rapides, ça compte.
2. **Funding** : tenir un perp coûte (ou rapporte) du funding en continu — c'est
   modélisé dans le paper.
3. **Mise à l'échelle extrême** : leurs comptes font $300k–870k, ton budget
   100 CHF. On copie leur *direction/conviction* mise à l'échelle. Conséquence :
   les signaux faibles tombent parfois **sous le minimum d'ordre Hyperliquid
   (~$10-12)** et sont ignorés — normal à ce budget.
4. **Tu ne vois pas tout** : couvertures ailleurs, taille réelle du risque,
   raison d'une position — inconnu. Le passé ne garantit pas le futur.
5. **Le levier** : eux tradent jusqu'à 20x. On plafonne NOTRE levier brut
   indépendamment (défaut 2x) pour ne pas exploser le budget.

## Recommandation

1. `hl-analyze` (déjà fait) → confirme qu'ils valent la peine. ✅
2. `hl-paper` en continu plusieurs jours → mesure l'edge NET de frais/funding/
   retard sur *ton* budget. C'est ça qui décide.
3. Ne passe au réel (`hl-live`, gaté) que si le paper est nettement positif net.
   Commence sous 100 CHF.

Contrairement au memecoin, ici il y a une vraie chance d'edge — mais elle doit
se prouver en paper avant tout argent réel.
