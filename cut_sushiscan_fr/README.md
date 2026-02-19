# cut_sushiscan_fr

Script Python pour reconstruire des pages manga depuis des images decoupees (JPG/JPEG/PNG/WEBP), avec workflow adapte au format SushiScan FR.

## Fonctionnalites

- Trim pub:
- haut de la 1re image (`786px` par defaut)
- bas de la derniere image (`786px` par defaut)
- Concatenation verticale de toutes les images source.
- Decoupage en pages de hauteur fixe (`2132px` par defaut).
- Nettoyage auto des micro "pixels parasites" en bas de page (1 a 6 px, configurable).
- Modes de sortie:
- `images` (JPG uniquement)
- `cbz` (CBZ, avec option suppression des JPG apres creation)
- `both` (JPG + CBZ)
- Mode interactif au lancement (selection source, destination, hauteur, options CBZ, verbose, suppressions).

## Prerequis

- Python 3.10+
- Dependances du `requirements.txt`

## Installation

```bash
cd cut_sushiscan_fr
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Utilisation

### 1) Mode interactif (recommande)

Si tu lances sans argument, le script passe en interactif:

```bash
python cut.py
```

Tu pourras choisir:
- dossier source
- dossier destination
- hauteur de page
- mode (`images`, `cbz`, `both`)
- verbose
- suppression des pages coupees apres creation du CBZ
- suppression des images source apres creation du CBZ

### 2) Mode ligne de commande

```bash
python cut.py "C:\chemin\vers\images" --output-folder "C:\chemin\vers\sortie"
```

Commande type SushiScan FR:

```bash
python cut.py "C:\chemin\vers\images" ^
  --trim-first-top 786 ^
  --trim-last-bottom 786 ^
  --page-height 2132 ^
  --mode both ^
  --verbose
```

## Destination par defaut

Si `--output-folder` n'est pas fourni, la sortie est:

```text
<source>\<nom_du_dossier_source>_cut
```

Exemple:

```text
...\Volume 1\Volume 1_cut
```

## Options principales

- `input_folder`
- dossier source des images

- `--interactive`
- force le mode interactif

- `--output-folder`
- dossier destination (sinon dossier `<source>_cut` dans la source)

- `--trim-first-top` (defaut `786`)
- `--trim-last-bottom` (defaut `786`)

- `--page-height` (defaut `2132`)
- `0` active l'auto-detection

- `--page-bottom-trim`
- retire N px en bas de chaque page generee (defaut `6`)

- `--mode {images,cbz,both}`
- mode de sortie

- `--cbz`
- raccourci retrocompatible pour `mode=both`

- `--cbz-name`
- nom du CBZ

- `--delete-pages-after-cbz`
- supprime les pages JPG apres creation du CBZ

- `--keep-pages-after-cbz`
- conserve les pages JPG apres creation du CBZ

- `--delete-source-after-cbz`
- supprime les images source apres creation reussie du CBZ

- `--fix-bottom-overlap` / `--no-fix-bottom-overlap`
- active/desactive le nettoyage auto des micro chevauchements aux frontieres

- `--max-overlap-fix-px` (defaut `6`)
- max de pixels retire par frontiere

- `--overlap-fix-threshold` (defaut `0.8`)
- seuil de similarite pour detecter un parasite

- `--overlap-fix-min-std` (defaut `0.0`)
- filtre texture minimal (0 desactive)

- `--save-strip`
- sauve la grande image concatenee en `_strip.jpg`

- `--skip-mostly-white-pages`
- ignore les pages majoritairement blanches

- `--verbose`
- logs detailles

## Exemples

CBZ uniquement (et suppression automatique des pages coupees):

```bash
python cut.py "C:\images" --mode cbz
```

CBZ + suppression sources apres creation:

```bash
python cut.py "C:\images" --mode cbz --delete-source-after-cbz --verbose
```

Conserver JPG + CBZ:

```bash
python cut.py "C:\images" --mode both --keep-pages-after-cbz
```

Diminuer legerement les micro parasites:

```bash
python cut.py "C:\images" --max-overlap-fix-px 6 --overlap-fix-threshold 1.2
```

## Sorties

- `page_001.jpg`, `page_002.jpg`, ...
- (optionnel) `_strip.jpg`
- (optionnel) `<nom>.cbz`

## Notes

- Sur le jeu SushiScan FR analyse, `2132` corrige la derive cumulative de pagination.
- Le nettoyage des pixels parasites est local (frontiere par frontiere), sans modifier le reste du flux.
- Le script ne retire pas les watermarks/logos inclus dans les images source.
