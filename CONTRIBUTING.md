# Contributing & Deployment Runbook

This repo follows the IST team's workflow:
[Git/GitHub Workflows IST](https://docs.google.com/document/d/16htXC8GEOXmUPXJqqW2T7umcnRe6c-F5paPm2t-13Yg/edit).
That document is the authority; this file is the short version plus what
is specific to this repo.

## Environments — how deploys actually happen

Deploys are driven by **branch** (`.drone.yml`). Merging to a branch
triggers that branch's Drone pipeline, which builds the four images and
`helm`-deploys them to Kanopy.

| Branch    | Deploys to      | Image tags                      | Config |
|-----------|-----------------|---------------------------------|--------|
| `staging` | **staging** env | `git-<sha>`, `latest`           | `environment/staging-*.yaml` |
| `main`    | **PRODUCTION**  | `git-<sha>-prod`, `prod-latest` | `environment/production-*.yaml` |

> ⚠️ **`main` == production.** Any merge to `main` ships to prod, including
> Dependabot merges and docs-only changes.

> ⚠️ **Staging and production share one database** (`agent_registry` on
> ist-shared-europe). Every change must stay compatible with the schema
> and data the production demo expects, including what staging writes to
> the shared, unprefixed collections (`mcp_services`, `agent_cards`, the
> reference fixtures). Don't add or remove `mcp_servers/` files or change
> `AGENT_MODE` on staging alone: the registry and agent-card sync delete
> entries the other environment still uses.

The Atlas Agent Engine agents (`agent-engine/`) are not deployed by Kanopy
and use their own cluster (`telco-oss-agents`).

## The workflow

```
staging ─► feature/xyz ─► PR → staging (squash) ─► test on staging
staging ─► PR → main (merge commit) ─► production
```

1. **Branch off `staging`**, with a prefix: `feature/`, `fix/` (or
   `bugfix/`), `docs/`, `exp/`.
   ```bash
   git checkout staging && git pull
   git checkout -b feature/my-change
   ```
2. **Commit in small logical steps**, with the Jira ticket in the message:
   `git commit -m "ISTTH-<n>: feat(ibn): …"`. Commits must be signed.
3. **Open a PR into `staging`**: one feature or fix, ideally under ~400
   lines, Jira ticket in the description. Draft PRs are fine for early
   feedback.
4. **Review and merge**: one approval from someone who didn't push the last
   commit, all threads resolved, Drone (build + gitleaks) green. The PR
   author merges with **Squash and merge**. Then verify on the staging env.
5. **Promote to production** with a PR from `staging` into `main`, merged
   with **Create a merge commit** (the only method `main` allows). Trivy
   runs on it; a CRITICAL/HIGH finding blocks the merge.

A long-lived branch must merge `staging` back in after each squash merge,
or its next PR shows the already-merged changes as conflicts. Force pushes
are blocked on every branch.

## Dependabot

Dependabot opens its PRs against `main`; an automated pipeline then syncs
`main` back into `staging`. Don't re-apply its bumps on `staging` by hand.
Major version bumps need a manual review. Because a merge to `main`
redeploys production here, check each bump against the running demo first.

## Local development

Not deployment — just running the whole demo on one machine:

```bash
source <venv>/bin/activate      # or PYTHON=<venv>/bin/python
bin/start.sh                    # shell :8070, IBN :8060, DTW :8080 (detached)
bin/stop.sh                     # stop all (+ sweep MCP subprocesses)
bin/restart.sh
```

See `CLAUDE.md` for architecture, env vars, and the one-time Atlas seed
step.
