# Contributing

This repository is the **Paperclip operationalization** (`hcoala-in-paperclip`) of the
upstream CoALA-aligned substrate
[`hermes-interprets-coala`](https://github.com/loboroboto/hermes-interprets-coala).
The substrate is materialized into this image at build time via a pinned `HCOALA_REF`;
this repo owns the machinery that runs it as a Paperclip fleet.

Because work flows in one direction (substrate → consumers), **it matters that every
issue and PR lands in the repo that owns the layer it changes.** Substrate work done
here can't propagate back up to other consumers; downstream operationalization filed
upstream pollutes the shared substrate.

## Where does this issue/PR go?

Apply this test — it is identical in both repos:

- **Does it touch the CoALA substrate?** — `AGENTS.md`, `SOUL.md`, `hermes.toml`,
  `mcp.json`, the seed `skills/`, the decision-cycle / cognitive model, or any Hermes
  capability that *any* consumer of the substrate would want.
  → **Upstream** — [`hermes-interprets-coala`](https://github.com/loboroboto/hermes-interprets-coala).
  After it lands, it is tagged `vYYYY.M.D` and we pull it by bumping `HCOALA_REF` in
  `docker/Dockerfile`.

- **Does it touch Paperclip operationalization?** — the composing Dockerfile, the
  `paperclip-hermes-gateway` runner, the onboarder/reconciler, `fleet/agents.yaml`,
  `companies/`, role ports, or the Railway/`.env` deployment surface.
  → **Downstream** — `hcoala-in-paperclip` (**this repo**).

- **Unsure, or it spans both?** → file it where the *larger* half lives, label it
  `scope: needs-triage`, and call out the cross-layer part so it can be split. A
  downstream issue whose real fix is in the substrate should spawn a separate upstream
  issue rather than being implemented here.

## Scope labels

Every issue carries exactly one `scope:` label so its correct home is unambiguous:

| Label | Meaning |
|-------|---------|
| `scope: upstream` | Belongs in `hermes-interprets-coala`. |
| `scope: downstream` | Belongs in `hcoala-in-paperclip` (this repo). |
| `scope: needs-triage` | Layer not yet determined — **do not implement until resolved.** |

If an issue is filed in the wrong repo, maintainers relabel it `scope: upstream` /
`scope: downstream` and transfer it (`gh issue transfer <n> <dest-repo>`). The scope
labels exist in both repos so they survive the transfer.

The **New issue** chooser is wired to this boundary: the operationalization template
pre-applies `scope: downstream`, a "belongs upstream" template pre-applies
`scope: upstream` + `scope: needs-triage` for substrate work that lands here by mistake,
and a contact link redirects substrate work straight to the upstream repo.
