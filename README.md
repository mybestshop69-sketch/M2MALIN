# M2 Malin Social Manager

Application web en francais pour preparer, approuver, programmer et publier le contenu de **M2 Malin** sur Facebook, Instagram et TikTok.

## Fonctionnalites incluses

- Tableau de bord protege par identifiant et mot de passe.
- Calendrier editorial avec programmation en heure de Paris.
- Statuts : brouillon, approuve, publie, echec.
- Publication manuelle immediate ou automatique toutes les minutes lorsque l'heure est atteinte.
- Connexion OAuth a Meta et TikTok.
- Publication Facebook : texte, image ou video.
- Publication Instagram professionnel : image ou Reel.
- Publication TikTok : photo ou video via une URL publique.
- Assistant IA Messenger en francais avec webhook Meta securise.
- Chiffrement des jetons d'acces avant stockage.
- Base SQLite en local ou PostgreSQL en production.
- Docker et configuration Render.

## Important avant utilisation

Le depot est public. **Ne mettez jamais les cles, mots de passe ou jetons dans GitHub.** Utilisez les variables d'environnement de l'hebergeur.

Si une cle Meta ou TikTok a deja ete copiee dans un message, un fichier ou une capture d'ecran, regenerez-la depuis le portail developpeur avant la mise en production.

## Demarrage local

1. Installez Python 3.12.
2. Copiez `.env.example` vers `.env`.
3. Generez une cle de chiffrement :

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

5. Lancez l'application :

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

Creez une application Meta, ajoutez Facebook Login et configurez l'URI de redirection exacte :

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

Autorisations demandees par l'application :

- `pages_show_list`
- `pages_read_engagement`
- `pages_manage_posts`
- `instagram_basic`
- `instagram_content_publish`
- `business_management`
- `pages_messaging`
- `pages_manage_metadata`

La Page Facebook doit etre administree par le compte connecte. Le compte Instagram doit etre professionnel et relie a cette Page.

## Assistant IA Messenger

Webhook public a configurer dans Meta :

```text
https://m2malin-social-manager.onrender.com/webhooks/meta
```

Le endpoint `GET /webhooks/meta` valide le webhook avec `META_WEBHOOK_VERIFY_TOKEN`. Le endpoint `POST /webhooks/meta` verifie `X-Hub-Signature-256` avec `META_APP_SECRET`, enregistre les evenements Messenger, ignore les echos, deduplique les messages puis les place dans une file traitee par APScheduler toutes les 7 secondes.

Variables Render a ajouter :

```env
META_WEBHOOK_VERIFY_TOKEN=
OPENAI_API_KEY=
OPENAI_MODEL=
MESSENGER_AUTO_REPLY_ENABLED=true
M2MALIN_SITE_URL=https://m2malin.fr
```

Ne mettez jamais de vraie valeur secrete dans GitHub. `OPENAI_MODEL` doit etre defini dans Render. Quand `MESSENGER_AUTO_REPLY_ENABLED=false`, le webhook continue de recevoir et stocker les messages, mais aucune reponse IA n'est envoyee.

La page protegee `/messenger` affiche l'etat du webhook, de Meta, d'OpenAI, les dernieres conversations, les messages en attente, les erreurs et les conversations necessitant une intervention humaine. Le bouton **Activer Messenger sur la page Meta** appelle `POST /{PAGE_ID}/subscribed_apps` avec les champs `messages,messaging_postbacks` en utilisant le Page Access Token chiffre deja stocke.

Les PSID Messenger sont chiffres en base et identifies par hash SHA-256. Les jetons Meta, secrets et PSID bruts ne doivent pas etre affiches dans l'interface ni dans les logs.

### Etapes Meta Messenger

1. Ajouter le produit Messenger si necessaire.
2. Configurer le webhook.
3. Callback URL :

```text
https://m2malin-social-manager.onrender.com/webhooks/meta
```

4. Verify Token : la valeur de `META_WEBHOOK_VERIFY_TOKEN`.
5. S'abonner aux evenements :
   - `messages`
   - `messaging_postbacks`
6. Ajouter ou demander les autorisations :
   - `pages_messaging`
   - `pages_manage_metadata`
7. Reconnecter Meta dans M2 Malin Social Manager.
8. Cliquer sur **Activer Messenger** dans `/messenger`.
9. Tester depuis un autre compte Facebook.

Meta App Review peut encore etre necessaire pour repondre aux personnes qui ne sont pas administrateurs, developpeurs ou testeurs de l'application. Tant que Meta n'a pas valide les permissions Messenger, les reponses peuvent etre limitees aux roles autorises.

Une reponse instantanee est actuellement active dans Meta Business Suite. Apres validation complete du bot IA, desactivez cette reponse instantanee afin d'eviter qu'un client recoive deux reponses au premier message.

### Premier test Messenger

1. Verifiez que Render contient `META_APP_SECRET`, `META_WEBHOOK_VERIFY_TOKEN`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `TOKEN_ENCRYPTION_KEY` et `MESSENGER_AUTO_REPLY_ENABLED=true`.
2. Ouvrez `https://m2malin-social-manager.onrender.com/health` et verifiez la reponse.
3. Dans Meta for Developers, validez le webhook avec l'URL ci-dessus.
4. Reconnectez Meta dans l'application pour obtenir `pages_messaging` et `pages_manage_metadata`.
5. Ouvrez `/messenger` et cliquez sur **Activer Messenger sur la page Meta**.
6. Depuis un autre compte Facebook, envoyez un message simple a la page M2Malin.
7. Verifiez que la conversation apparait dans `/messenger` et que le client recoit une reponse concise en francais.

## Configuration TikTok

Dans TikTok for Developers :

1. Creez une application.
2. Ajoutez **Login Kit** et **Content Posting API**.
3. Configurez l'URI exacte :
   `https://VOTRE-DOMAINE/oauth/tiktok/callback`
4. Demandez les autorisations `video.publish` et `video.upload`.
5. Faites verifier le domaine qui heberge vos images et videos.
6. Soumettez l'application a l'audit TikTok pour permettre des publications publiques.

Variables :

```env
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_REDIRECT_URI=https://VOTRE-DOMAINE/oauth/tiktok/callback
TIKTOK_SCOPES=user.info.basic,video.publish,video.upload
```

Sans audit, TikTok peut limiter les publications de test au mode prive.

## Deploiement Render

1. Creez un nouveau service Render depuis ce depot.
2. Render detectera `render.yaml`.
3. Ajoutez les secrets dans **Environment**.
4. Definissez :
   - `APP_BASE_URL=https://votre-service.onrender.com`
   - les deux URI OAuth avec ce meme domaine ;
   - un `ADMIN_PASSWORD` fort.
5. Redeployez.

Le fichier Docker utilise un seul worker afin d'eviter que plusieurs planificateurs publient la meme publication.

## Mode automatique

Par defaut, chaque nouvelle publication est enregistree en brouillon.

```env
HUMAN_APPROVAL_REQUIRED=true
AUTO_MODE_ENABLED=false
```

Pour approuver automatiquement les nouvelles publications :

```env
HUMAN_APPROVAL_REQUIRED=false
AUTO_MODE_ENABLED=true
```

L'automatisation ne remplace pas les validations exigees par Meta ou TikTok. Elle ne peut publier que lorsque les comptes, autorisations et jetons sont valides.

## Limites actuelles de cette premiere version

- Le premier compte/Page Facebook renvoye par Meta est selectionne automatiquement.
- Les videos et images doivent deja etre hebergees sur une URL HTTPS publique.
- Le renouvellement automatique des jetons TikTok et les jetons Meta longue duree devront etre ajoutes avant une exploitation continue.
- La generation IA des textes et des visuels n'est pas encore branchee a un fournisseur d'IA.
- Pour une montee en charge, deplacez le planificateur vers un worker separe avec verrou distribue.

## Securite

- Ne commitez jamais `.env`.
- Changez regulierement les secrets.
- Conservez `TOKEN_ENCRYPTION_KEY` : la perdre rend les jetons stockes illisibles.
- Utilisez HTTPS en production.
- Gardez `HUMAN_APPROVAL_REQUIRED=true` pendant les premiers tests.
