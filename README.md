# torbox-gha-prototype — secret-URL Range streaming on free compute

Proven pattern, new home: the HTTP-Range zip reader from
`hero-parts-pull/zip_extract.py` and the pipe-to-GitHub uploader from
`hero-parts-pull/github_upload_stream.py`, moved onto a GitHub Actions
runner so that

1. **zero bytes touch local disk** (the local machine was at 96–97% full),
2. **the download link never passes through chat or agent tooling** — a
   human pastes it once into the repo's Actions secrets in GitHub's UI;
   the runner reads it from the environment; nothing prints it.

## Files

| File | What it is |
|---|---|
| `range_list.py` | Stdlib-only Range zip reader/streamer. Modes: `list`, `extract-one`, `slice`. URL only from `SOURCE_URL` env var. |
| `.github/workflows/torbox-range.yml` | `workflow_dispatch` wrapper: list smoke test, per-file extract→release asset, matrix slice-upload with skip-if-present resume + size verify. |
| `kaggle/kaggle_kernel.py` | Same script on Kaggle, secret via Kaggle Secrets (`UserSecretsClient`). |
| `cloudflare-worker/worker.js` | Optional dumb Range proxy (URL laundering only — see the evaluation doc for why it is not an engine). |
| `test-output/` | Local test evidence against a public archive.org zip (below). |

## Local test evidence (2026-10-20, run on this machine, public file)

Target: `https://archive.org/download/CNleaks/OK%20KO.zip` — a real
7,910,137,932-byte **Zip64** archive, the same one the local pipeline had
already parsed (`hero-parts-pull/okko-ia-centraldir.json`).

- `list` → **476 entries, 428 files, 12.046 GB uncompressed** — entry count
  and total size match the earlier independent local parse exactly.
  (`test-output/list-run.log`, `test-output/okko-manifest.json`)
- `extract-one ENID_01_RGB.ai` (streaming, 1 MiB slices) → 444,118 bytes
  written, `size_ok=True`, **CRC32 computed `4843a44f` == central-directory
  CRC `4843a44f`**. (`test-output/extract-one-run.log`)
- `slice 1000000 4194304` → sha256 `b8103775…519`, **byte-identical to an
  independent `curl -L -r` fetch of the same range**.
  (`test-output/slice-crosscheck.txt`)

## Use (parent / browser lane does the secret steps)

1. Create a repo containing this folder's code (no secrets in git, ever).
   Public runner repo + private target repo is the unlimited-minutes
   shape — see the evaluation doc for the trade-off and the ToS note.
2. Repo **Settings → Secrets and variables → Actions**:
   secret `SOURCE_URL` = the CDN/debrid link (pasted in the browser);
   optional secret `TARGET_PAT` (fine-grained, target repo only,
   Contents RW) and variables `TARGET_REPO`, `RELEASE_TAG` when the
   release lives in a different repo.
3. **Actions → torbox-range → Run workflow**, `mode=list` first
   (smoke test: costs only the zip tail + central directory, ~70 KB–
   a few MB). Then `extract-one` per wanted file, or `slice-upload`
   with a JSON list of offsets for a whole-archive mirror.
4. After the run: delete the secrets / revoke the PAT. Bearer links and
   PATs are spent credentials once the transfer is done.

Full evaluation of every platform (GHA, Kaggle, Cloudflare Workers,
Colab, HF Spaces, Oracle, MultCloud):
`~/workspace/agent-ops/torbox-lane4-compute.md`
