# tokens_max — 4 connecteurs MCP par URL pour claude.ai

Ponytail, Graphify, Agent Skills et OmniRoute, utilisables dans claude.ai via
**Paramètres → Connecteurs → Ajouter un connecteur personnalisé → URL**.

## Pourquoi ce dépôt existe

Vérifié le 2026-09-28 : aucun des 4 projets n'a de connecteur dans l'annuaire claude.ai ni
d'URL MCP publique hébergée.

| Projet | Ce que l'original fournit | Ce qui manquait pour claude.ai | Ce que fait ce dépôt |
|---|---|---|---|
| [Ponytail](https://github.com/DietrichGebert/ponytail) | Plugin (hooks + 6 skills) et un serveur MCP **stdio** (1 prompt + 1 outil) | Pas de transport HTTP, pas d'URL | Même prompt, même outil, **même texte** (parité octet par octet avec le JS d'origine, testée), plus les 5 skills compagnons et le ruleset en `instructions` serveur (équivalent du hook « toujours actif ») |
| [Agent Skills](https://github.com/addyosmani/agent-skills) | Plugin : 25 skills, 9 commandes, 4 agents, 7 checklists. Aucun MCP | Tout | Contenu d'origine servi tel quel : index des skills en contexte, chargement à la demande (`get_skill`), les 9 commandes (/spec … /ship) et les 4 personas en prompts |
| [Graphify](https://github.com/Graphify-Labs/graphify) | Serveur MCP HTTP officiel, mais sur un `graph.json` **local** | Claude.ai n'a pas de disque local | Le paquet officiel `graphifyy` tourne côté serveur : `index_repository` clone le dépôt et lance le pipeline d'origine ; les 10 outils et 6 ressources d'origine sont délégués **sans modification** au serveur graphify |
| [OmniRoute](https://github.com/diegosouzapw/OmniRoute) | Serveur MCP HTTP officiel (110 outils) dans l'app OmniRoute | Exige un en-tête `Bearer` que claude.ai ne sait pas envoyer ; MCP désactivé par défaut | L'app officielle tourne telle quelle ; un proxy injecte la clé et active le MCP automatiquement via l'API d'OmniRoute |

Rien n'est réimplémenté quand l'original existait : Graphify et OmniRoute exécutent leur
propre code, Ponytail et Agent Skills servent leurs fichiers d'origine (dossier `vendor/`,
commits figés dans `vendor/UPSTREAM.txt`).

## URLs à coller dans claude.ai

```
https://<domaine>/<MCP_SECRET>/ponytail/mcp
https://<domaine>/<MCP_SECRET>/agent-skills/mcp
https://<domaine>/<MCP_SECRET>/graphify/mcp
https://<domaine>/<MCP_SECRET>/omniroute/mcp
```

Claude.ai → Paramètres → Connecteurs → « Ajouter un connecteur personnalisé » → nom +
URL → Ajouter. Pas d'OAuth à configurer. Activer ensuite le connecteur dans la
conversation (menu « + » / outils).

**Sécurité** : claude.ai ne peut pas envoyer d'en-tête d'authentification fixe, donc
l'accès repose sur le segment secret `MCP_SECRET` (URL-capacité, 256 bits). Toute personne
qui a l'URL peut utiliser le connecteur : ne la publie pas. Pour la révoquer, change
`MCP_SECRET` sur Railway puis recrée les connecteurs.

## Alternative native pour Ponytail et Agent Skills : les Skills claude.ai

Ces deux projets sont des *skills*. claude.ai sait charger des skills nativement
(Paramètres → Capacités → Skills → Téléverser), ce qui reproduit exactement leur
fonctionnement dans Claude Code (description toujours en contexte, contenu chargé quand la
tâche s'y prête). Les 31 paquets prêts à téléverser sont dans `claude-skills/`
(`python scripts/build_claude_skills.py` pour les régénérer). Tu peux utiliser les skills,
le connecteur MCP, ou les deux.

## Outils exposés

- **ponytail** — prompts `ponytail` (mode lite/full/ultra), `ponytail-review`, `ponytail-audit`,
  `ponytail-debt`, `ponytail-gain`, `ponytail-help` ; outils `ponytail_instructions`,
  `ponytail_skill`. Mode par défaut : variable `PONYTAIL_DEFAULT_MODE` (comme l'original).
- **agent-skills** — outils `list_skills`, `get_skill`, `search_skills`, `read_file`,
  `list_references`, `get_agent`, `run_command` ; prompts `spec`, `plan`, `build`, `test`,
  `constraints`, `review`, `webperf`, `code-simplify`, `ship`, `agent-*`.
- **graphify** — `index_repository` (URL https ou `owner/repo`, branche/tag/commit
  optionnel), `list_graphs`, `graph_insights` (report, god-nodes, surprises, questions,
  audit, stats), `remove_graph`, puis les outils d'origine : `query_graph`, `get_node`,
  `get_neighbors`, `get_community`, `god_nodes`, `graph_stats`, `shortest_path`,
  `list_prs`, `get_pr_impact`, `triage_prs` (ces 3 derniers exigent `GITHUB_TOKEN`).
- **omniroute** — les 110 outils d'OmniRoute (`omniroute_route_request`,
  `omniroute_web_search`, compression, cache, mémoire, combos, quotas…). `model: "auto"`
  répond sans aucune clé fournisseur (OpenCode Free préconfiguré) ; ajoute tes comptes
  fournisseurs dans le tableau de bord OmniRoute pour le reste.

## Déploiement (Railway)

Deux services dans un projet :

1. **omniroute** — image `diegosouzapw/omniroute:latest`, volume sur `/app/data`, domaine
   public (pour le tableau de bord), variables : `JWT_SECRET`, `API_KEY_SECRET`,
   `INITIAL_PASSWORD`, `REQUIRE_API_KEY=true`, `RAILWAY_RUN_UID=0`.
2. **gateway** — ce dépôt (Dockerfile), volume sur `/data`, domaine public, variables :
   - `MCP_SECRET` (obligatoire, ≥ 24 caractères : `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
     Sans lui, le service démarre en mode « non configuré » : `/` affiche l'erreur et aucun
     endpoint MCP n'est exposé.
   - `OMNIROUTE_URL=https://<domaine omniroute>`
   - `OMNIROUTE_PASSWORD=${{omniroute.INITIAL_PASSWORD}}` (active le MCP d'OmniRoute et
     crée une clé `manage` au premier appel ; ou `OMNIROUTE_API_KEY` si tu la crées toi-même)
   - optionnel : `GITHUB_TOKEN` (dépôts privés + outils PR de graphify),
     `PONYTAIL_DEFAULT_MODE`, `GRAPHIFY_MAX_GRAPHS` (25), `GRAPHIFY_MAX_REPO_MB` (1024),
     `GRAPHIFY_ALLOWED_HOSTS`.

N'importe quel hébergeur Docker convient (Fly.io, Render, VPS) : il faut HTTPS public, un
volume persistant et les mêmes variables.

## Vérifier que ça marche

```bash
pip install -r requirements.txt pytest
python -m pytest -q tests                               # 14 tests, dont parité Ponytail vs JS d'origine
python scripts/smoke_test.py https://<domaine>/<MCP_SECRET>   # appelle les 4 endpoints avec le client MCP officiel
```

Le smoke test affiche `ponytail ok`, `agent-skills ok`, `graphify ok` (indexe un vrai
dépôt et l'interroge) et `omniroute ok: 110 tools`.

## Limites connues

- **OmniRoute dans claude.ai ≠ OmniRoute dans Claude Code.** Sa fonction principale
  (faire passer le trafic de Claude Code par des fournisseurs gratuits) n'a pas de sens
  dans claude.ai, qui utilise toujours les modèles d'Anthropic. Le connecteur donne ses 110
  outils : appeler d'autres modèles, recherche web, compression, mémoire, gestion.
- **Ponytail « toujours actif »** repose sur les `instructions` du serveur MCP ; si le
  client ne les injecte pas, invoque le prompt `ponytail` ou utilise la Skill téléversée.
- **Graphify** indexe le code (AST tree-sitter, sans LLM, comme `graphify extract
  --code-only`). La passe sémantique sur docs/PDF/images de l'original utilise le modèle de
  l'assistant côté client ; elle n'est pas reproduite côté serveur.
- Les hooks des plugins d'origine (statusline, sous-agents) n'ont pas d'équivalent dans
  claude.ai.

## Licences

Ponytail (MIT, Dietrich Gebert), agent-skills (MIT, Addy Osmani), graphify (Apache-2.0,
Graphify Labs), OmniRoute (MIT, Diego Souza). Licences d'origine dans `vendor/*/LICENSE`
et dans chaque paquet de `claude-skills/`.
