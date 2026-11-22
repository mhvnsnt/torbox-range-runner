# HANDOFF — torbox-range-runner

**Last updated: 2026-11-22 (America/New_York).**
**Repo:** `mhvnsnt/torbox-range-runner` (PUBLIC), default branch `main`.
**Purpose of this file:** any agent that picks up work here reads this FIRST, then the linked docs. It states what we've done, where we're at, and what we're working toward — so the owner can say "go to this repo, pick up the work" and you just know. Update this file in the same PR whenever the state changes (add, don't delete).

## 1. What this repo is

The range-extraction runner. It streams byte slices of a remote zip straight to GitHub releases — code only, no data lives here. One extractor script per show/set, each driven by a `.tsv` file list, plus a shared range-list helper.

Extractors on main: Bakugan (`bakugan_extract.py`), Camp Lazlo (`camp_lazlo_extract.py`), Gumball (`gumball_extract.py`), Hanna-Barbera Classics (`hb_classics_extract.py`), RS (`rs_extract.py`, `rs_extract_gh.py`). `at_extract.py` and `range_list.py` are shared helpers. `WORKING-METHOD.md` is the method doc. Run logs live in `runs/`, `runs-bakugan/`, `runs-gumball/`, `runs-hb-classics/`, `runs-rs/`, `runs-camp-lazlo/`.

## 2. Where we're at

### DONE
- **Bakugan extraction (2026-10/11):** full run landed; `bakugan-cd-full.tsv` + `runs-bakugan/` on main.
- **Gumball extraction:** `gumball-cd-full.tsv` + `runs-gumball/` on main.
- **Camp Lazlo extraction:** `camp_lazlo_extract.py` + `camp-lazlo-cd-full.tsv` + `runs-camp-lazlo/` on main.
- **RS extraction:** `rs-cd-full.tsv` + `rs_extract.py` / `rs_extract_gh.py` + `runs-rs/` on main.
- **HB Classics wave 2 (2026-11-22, TODAY):** `hb_classics_extract.py` + `hb-classics-cd-full-ALL.tsv` + `hb-classics-release-contents-20261122.tsv` + `runs-hb-classics/` — latest commits `898d167` (extraction trigger), `8a9e793` (workflow), `c0d8af8` (runner extractor), all 2026-11-22. This repo is ACTIVE today.

### IN PROGRESS
- **HB Classics wave 2 extraction (owner: active lane, 2026-11-22).** The wave-2 trigger, workflow, and runner extractor landed today. Next step: confirm the wave-2 slices/releases landed and verify them the same way the Bakugan/Gumball runs were verified — do not assume the trigger finishing means the releases are good.

### BLOCKED
- Nothing recorded as blocked in this repo. No open PRs, no releases on this repo (releases land on the destination repos, not here).

## 3. What we're working toward

Keep the extractor set working show by show: each new set gets an extractor + a `.tsv` file list + a run log, and the slices land as GitHub releases on the destination repo. The bar: every slice hash-verified, every run logged in its `runs-*` folder, nothing left only on local disk.

## 4. Standing rules that apply here

- **Branch → PR → merge, always.** Never push to main directly. Add-only, zero-conflict PRs, no force pushes.
- **Checkpoint-and-save law:** CHECKPOINT.md first, update after every step; the repo is the save; resume, never restart.
- **No-temp-storage law:** nothing in /tmp or any wipeable location, ever. Run logs and lists go straight into the repo.
- **Safe-storage-first:** everything lands in a repo/release before local copies are cleared, and only per the one-store rule (100% verified on GitHub or TeraBox).
- **Never kill in-progress work.** An extraction run in flight finishes — never stop it to "optimize" it.
- **Archive.org first:** for any game/show acquisition, archive.org is checked first, live, before any other source.

## 5. Key files and paths

| What | Where |
|---|---|
| Method doc | `WORKING-METHOD.md` |
| Shared helpers | `range_list.py`, `at_extract.py` |
| Extractors + file lists | `bakugan_extract.py` / `bakugan-cd-full.tsv`, `gumball_extract.py` / `gumball-cd-full.tsv`, `camp_lazlo_extract.py` / `camp-lazlo-cd-full.tsv`, `hb_classics_extract.py` / `hb-classics-cd-full-ALL.tsv`, `rs_extract.py` / `rs_extract_gh.py` / `rs-cd-full.tsv` |
| HB Classics wave-2 contents | `hb-classics-release-contents-20261122.tsv`, `slices-assets-20261122.tsv` |
| Run logs | `runs/`, `runs-bakugan/`, `runs-gumball/`, `runs-hb-classics/`, `runs-rs/`, `runs-camp-lazlo/` |
| Repo readme | `README.md` |

## 6. Open decisions needing the owner

- **Next extraction wave targets:** which show/set gets an extractor after HB Classics wave 2. His call — lanes do not pick the next set.
- No other decisions are recorded in this repo.

## 7. How to pick up

1. Read this HANDOFF.md first, then `WORKING-METHOD.md` and `README.md`.
2. Check the latest `runs-hb-classics/` log to see where wave 2 actually stands — the trigger landing is not proof the releases are good.
3. Check what other agents are already doing before starting anything (never start a duplicate extraction).
4. Resume from the stated next step — never restart, never re-derive finished work.
5. Land work branch → PR → merge, add-only. Stage finished work to the repo as it lands.
6. Update this HANDOFF.md in the same PR when the state changes. Verify with your own eyes before claiming anything done.
