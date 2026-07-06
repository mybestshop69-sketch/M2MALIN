# M² Malin Social Manager

Application web en français pour préparer, approuver, programmer et publier le contenu de **M² Malin** sur Facebook, Instagram et TikTok.

## Fonctionnalités incluses

- Tableau de bord protégé par identifiant et mot de passe.
- Calendrier éditorial avec programmation en heure de Paris.
- Statuts : brouillon, approuvé, publié, échec.
- Publication manuelle immédiate ou automatique toutes les minutes lorsque l’heure est atteinte.
- Connexion OAuth à Meta et TikTok.
- Publication Facebook : texte, image ou vidéo.
- Publication Instagram professionnel : image ou Reel.
- Publication TikTok : photo ou vidéo via une URL publique.
- Chiffrement des jetons d’accès avant stockage.
- Base SQLite en local ou PostgreSQL en production.
- Docker et configuration Render.

## Important avant utilisation

Le dépôt est public. **Ne mettez jamais les clés, mots de passe ou jetons dans GitHub.** Utilisez les variables d’environnement de l’hébergeur.

Si une clé Meta ou TikTok a déjà été copiée dans un message, un fichier ou une capture d’écran, régénérez-la depuis le portail développeur avant la mise en production.

## Démarrage local

1. Installez Python 3.12.
2. Copiez `.env.example` vers `.env`.
3. Générez une clé de chiffrement :

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

4. Renseignez au minimum :

```env
FLASK_SECRET_KEY=une-valeur-longue-et-aleatoire
TOKEN_ENCRYPTION_KEY=la-cle-generee
ADMIN_USERNAME=admin
ADMIN_PASSWORD=un-mot-de-passe-fort
```

5. Lancez l’application :

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

Ouvrez ensuite `http://localhost:5000`.

## Configuration Meta

Créez une application Meta, ajoutez Facebook Login et configurez l’URI de redirection exacte :

```text
https://VOTRE-DOMAINE/oauth/meta/callback
```

Variables :

```env
META_APP_ID=
META_APP_SECRET=
META_GRAPH_VERSION=v23.0
META_REDIRECT_URI=https://VOTRE-DOMAINE/oauth/meta/callback
```

Autorisations demandées par l’application :

- `pages_show_list`
- `pages_read_engagement`
- `pages_manage_posts`
- `instagram_basic`
- `instagram_content_publish`
- `business_management`

La Page Facebook doit être administrée par le compte connecté. Le compte Instagram doit être professionnel et relié à cette Page.

## Configuration TikTok

Dans TikTok for Developers :

1. Créez une application.
2. Ajoutez **Login Kit** et **Content Posting API**.
3. Configurez l’URI exacte :
   `https://VOTRE-DOMAINE/oauth/tiktok/callback`
4. Demandez les autorisations `video.publish` et `video.upload`.
5. Faites vérifier le domaine qui héberge vos images et vidéos.
6. Soumettez l’application à l’audit TikTok pour permettre des publications publiques.

Variables :

```env
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_REDIRECT_URI=https://VOTRE-DOMAINE/oauth/tiktok/callback
TIKTOK_SCOPES=user.info.basic,video.publish,video.upload
```

Sans audit, TikTok peut limiter les publications de test au mode privé.

## Déploiement Render

1. Créez un nouveau service Render depuis ce dépôt.
2. Render détectera `render.yaml`.
3. Ajoutez les secrets dans **Environment**.
4. Définissez :
   - `APP_BASE_URL=https://votre-service.onrender.com`
   - les deux URI OAuth avec ce même domaine ;
   - un `ADMIN_PASSWORD` fort.
5. Redéployez.

Le fichier Docker utilise un seul worker afin d’éviter que plusieurs planificateurs publient la même publication.

## Mode automatique

Par défaut, chaque nouvelle publication est enregistrée en brouillon.

```env
HUMAN_APPROVAL_REQUIRED=true
AUTO_MODE_ENABLED=false
```

Pour approuver automatiquement les nouvelles publications :

```env
HUMAN_APPROVAL_REQUIRED=false
AUTO_MODE_ENABLED=true
```

L’automatisation ne remplace pas les validations exigées par Meta ou TikTok. Elle ne peut publier que lorsque les comptes, autorisations et jetons sont valides.

## Limites actuelles de cette première version

- Le premier compte/Page Facebook renvoyé par Meta est sélectionné automatiquement.
- Les vidéos et images doivent déjà être hébergées sur une URL HTTPS publique.
- Le renouvellement automatique des jetons TikTok et les jetons Meta longue durée devront être ajoutés avant une exploitation continue.
- La génération IA des textes et des visuels n’est pas encore branchée à un fournisseur d’IA.
- Pour une montée en charge, déplacez le planificateur vers un worker séparé avec verrou distribué.

## Sécurité

- Ne commitez jamais `.env`.
- Changez régulièrement les secrets.
- Conservez `TOKEN_ENCRYPTION_KEY` : la perdre rend les jetons stockés illisibles.
- Utilisez HTTPS en production.
- Gardez `HUMAN_APPROVAL_REQUIRED=true` pendant les premiers tests.
