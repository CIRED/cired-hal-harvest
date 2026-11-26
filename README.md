# Moissonneur HAL des publications du CIRED

Ce script constitue, et tient à jour, une copie locale des publications du
[CIRED](https://www.centre-cired.fr/) déposées en texte intégral dans
[HAL](https://hal.science/CIRED) : les PDF, leurs notices au format RIS et leur
texte brut. Il vient du projet CIRED.digital (assistant documentaire « Cirdi »,
2025-2026, arrêté le 17 novembre 2026), dont il moissonnait le corpus.

Le dépôt contient aussi les métadonnées de la moisson du 26 novembre 2026
(`data/`). Les PDF n'y figurent pas : chacun garde la licence choisie par son
déposant dans HAL. Le script les télécharge.

Contact : Minh Ha-Duong (CNRS, CIRED).

## Faire sa copie

Prérequis : [uv](https://docs.astral.sh/uv/), ou bien `python3` (3.11 ou plus)
avec le module `requests` ; `pdftotext` (paquet `poppler-utils`) pour le texte
brut.

```bash
git clone https://github.com/CIRED/cired-hal-harvest.git
cd cired-hal-harvest
uv run hal_harvest.py update      # ou : python3 hal_harvest.py update
```

Une moisson complète prend environ 40 minutes (26 novembre 2026 : 1 388
fichiers, 4,4 Go). Les mises à jour suivantes ne téléchargent que les nouveautés.

Résultat, dans `data/` :

```
documents/           les textes intégraux, nommés par identifiant HAL (hal-01234567.pdf)
cired_hal.ris        les notices, chacune liée à son fichier dans documents/
text/                le texte brut de chaque PDF (hal-01234567.txt)
catalog/             catalogues dédoublonnés datés ; rapports d'échecs
raw/                 réponses brutes de l'API HAL, une par moisson
harvests.tsv         journal des moissons : date, notices, fichiers, échecs
```

**Périmètre.** Toutes les notices HAL portant l'acronyme de laboratoire CIRED
(`labStructAcronym_s:CIRED`, qui couvre les deux structId successifs du labo)
et dont le texte intégral est déposé dans HAL (`submitType_s:file`). Sont
dédoublonnés : les working papers dont une version publiée existe sous le même
titre, et les dépôts multiples d'un même DOI (sauf ouvrage + chapitres). Les
dépôts d'un même travail sous des types différents (article, communication,
poster) restent distincts. Le périmètre se change en tête du script
(`HAL_QUERY`, `HAL_FILTER`) : il sert pour n'importe quelle collection HAL.

**Hors périmètre.** Les publications sans texte intégral dans HAL, les liens
vers des versions ouvertes hébergées ailleurs, les documents sous embargo.

## Utiliser la collection

- **Recherche plein texte**, sans LLM :

  ```bash
  rg -il "taxe carbone" data/text/ | head        # quels documents en parlent
  rg -i -C2 "double dividende" data/text/          # avec le contexte
  ```

  Le nom de chaque fichier est l'identifiant HAL : `https://hal.science/hal-01234567`
  ouvre la notice.
- **Gestionnaire de références** : `data/cired_hal.ris` s'importe dans Zotero
  (Fichier → Importer…), EndNote ou JabRef. Chaque notice indique le chemin
  relatif de son PDF (champ `L1`) : garder le fichier RIS à côté de
  `data/documents/`. Pour compléter plus tard une bibliothèque déjà importée
  sans créer de doublons, `uv run hal_harvest.py ris --new-since
  data/catalog/catalog_<date de l'import>.json` écrit `data/cired_hal_new.ris`
  avec les seules notices nouvelles.
- **Assistant ou RAG** : `data/text/` et `data/catalog/` (métadonnées en JSON)
  fournissent un corpus prêt à indexer.

## Étapes

`update` enchaîne les étapes suivantes, lançables séparément
(`uv run hal_harvest.py <étape>`) :

1. **query** interroge l'API HAL. Le script compare le nombre de notices reçues
   au total annoncé par HAL et **n'enregistre rien si la moisson est incomplète**
   (coupure réseau, erreur serveur) : relancer plus tard.
2. **prepare** dédoublonne la dernière moisson en un catalogue daté.
3. **download** télécharge les fichiers absents, à une seconde d'intervalle par
   politesse envers HAL.
4. **ris** écrit `data/cired_hal.ris`.
5. **extract** convertit les nouveaux PDF en texte avec `pdftotext` ; sans lui,
   l'étape est sautée avec un avertissement.

`status` liste les fichiers manquants (échecs, souvent des embargos : voir
`data/catalog/download_failures_*.json`) et les fichiers qui ne sont plus au
catalogue (notice retirée ou fusionnée dans HAL). Ces derniers ne sont jamais
supprimés automatiquement. L'option `--data DIR` place les données ailleurs.

Tests, sans réseau : `uv run --group dev pytest`.

## Licences

Code : [CeCILL 2.1](LICENSE.html). Métadonnées HAL (`data/`) : CC0, comme
HAL les diffuse. Textes intégraux : la licence de chaque dépôt, indiquée sur sa
page HAL.
