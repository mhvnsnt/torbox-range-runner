#!/usr/bin/env python3
"""Runner-side Adventure Time extractor (GitHub Actions).

Range-reads one tar part's worth of Adventure Time files (method 0 / stored)
from the Documents.zip SOURCE_URL (TorBox CDN secret), verifies CRC32 per file,
packs them into an uncompressed .tar part, uploads it + its manifest to the
adventure-time gap-fill release, and verifies both assets via the API.
Skip logic: if the part asset AND its manifest already exist and the
manifest's recorded part-sha256 matches the asset digest, the part is done —
exit 0.

Method note: all Adventure Time CD entries are method 0 (stored) — verified
by re-parsing the Documents.zip central directory (at-cd-full.tsv, 13,484
entries, method histogram {0: 13484}). Method != 0 is a hard failure, same
as the Bakugan script this was adapted from.

Env: SOURCE_URL (secret, never printed), GH_TOKEN (TARGET_PAT, Contents:RW
on the target repo).
Usage: at_extract.py --tsv at-cd-full.tsv --start 0 --end 140
       --part-no 1 [--release-id 454447035]
"""
import argparse, hashlib, io, json, os, struct, subprocess, sys, tarfile, time, urllib.request, zlib

TARGET_REPO = "mhvnsnt/cn-gigaleak-adventure-time"
RELEASE_ID_DEFAULT = 454447035
PART_CAP = 1900000000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CHUNK = 8 * 1024 * 1024

def redacted(url):
    import urllib.parse as _up
    p = _up.urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path} (query redacted)"

def fetch(url, start, end, tries=5):
    """GET byte range [start, end] inclusive; returns bytes."""
    last = None
    for a in range(tries):
        req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                assert r.status == 206, f"range failed: HTTP {r.status}"
                data = r.read()
            assert len(data) == end - start + 1, f"short read {len(data)} != {end-start+1}"
            return data
        except Exception as e:
            last = e
            time.sleep(2 * (a + 1))
    raise RuntimeError(f"fetch failed [{start},{end}] after {tries}: {last}")

def fetch_chunks(url, start, end):
    pos = start
    while pos <= end:
        n = min(CHUNK, end - pos + 1)
        yield fetch(url, pos, pos + n - 1)
        pos += n

def load_entries(tsv):
    entries = []
    with open(tsv, encoding="utf-8") as f:
        for line in f:
            name, method, crc, csize, usize, lho = line.rstrip("\n").split("\t")
            entries.append({"name": name, "method": int(method), "crc": int(crc, 16),
                            "csize": int(csize), "usize": int(usize), "lho": int(lho)})
    return entries

def gh_api(args, token):
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    p = subprocess.run(["gh", "api"] + args, capture_output=True, text=True, timeout=120, env=env)
    return p

def list_assets(release_id, token):
    p = gh_api(["repos/" + TARGET_REPO + f"/releases/{release_id}/assets",
                "--paginate", "--jq", ".[] | {name, id, size, state, digest}"],
               token)
    if p.returncode != 0:
        raise RuntimeError(f"asset list failed: {p.stderr[:300]}")
    return [json.loads(l) for l in p.stdout.strip().splitlines() if l.strip()]

def asset_by_name(assets, name):
    for a in assets:
        if a["name"] == name:
            return a
    return None

def upload_file(release_id, token, path, name, content_type):
    url = f"https://uploads.github.com/repos/{TARGET_REPO}/releases/{release_id}/assets?name={name}"
    # NOTE: the token is interpolated here in Python. An earlier version of the
    # Bakugan script passed the literal string "Bearer $GH_UPLOAD_TOKEN" as the
    # header value — curl does not expand env vars in -H arguments, so GitHub
    # answered 401 on every part upload (bakugan run 45024112890). Fixed in
    # commit bbc8b1bf; this adaptation carries the fix. The token value itself
    # is never printed or logged.
    cmd = ["curl", "-sS", "--fail", "-X", "POST",
           "-H", f"Authorization: Bearer {token}",
           "-H", f"Content-Type: {content_type}",
           "--data-binary", f"@{path}", url]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0:
        raise RuntimeError(f"upload failed for {name}: {p.stderr.strip()[:500]}")
    return json.loads(p.stdout)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tsv", required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--part-no", type=int, required=True)
    ap.add_argument("--release-id", type=int, default=RELEASE_ID_DEFAULT)
    args = ap.parse_args()
    url = os.environ.get("SOURCE_URL", "").strip()
    token = os.environ.get("GH_TOKEN", "").strip()
    if not url or not token:
        print("ERROR: SOURCE_URL / GH_TOKEN env not set", file=sys.stderr)
        return 2
    print(f"source: {redacted(url)}", flush=True)
    entries = load_entries(args.tsv)
    part_entries = entries[args.start:args.end]
    part_name = f"at-gap-fill-part-{args.part_no:02d}.tar"
    manifest_name = f"MANIFEST-at-gap-fill-part-{args.part_no:02d}.txt"
    part_path = os.path.join(os.getcwd(), part_name)
    manifest_path = os.path.join(os.getcwd(), manifest_name)
    print(f"part {args.part_no}: files {args.start}..{args.end-1} ({len(part_entries)} files, "
          f"{sum(e['usize'] for e in part_entries)/1e9:.2f} GB)", flush=True)
    # skip logic: part + manifest present, manifest's part-sha256 == asset digest
    assets = list_assets(args.release_id, token)
    existing = asset_by_name(assets, part_name)
    if existing and existing.get("state") == "uploaded" and existing.get("digest"):
        m = asset_by_name(assets, manifest_name)
        if m and m.get("state") == "uploaded":
            import urllib.request as _ur
            req = _ur.Request(f"https://api.github.com/repos/{TARGET_REPO}/releases/assets/{m['id']}",
                              headers={"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"})
            with _ur.urlopen(req, timeout=120) as r:
                mtext = r.read().decode("utf-8", "replace")
            for line in mtext.splitlines():
                if line.startswith("# part-sha256 "):
                    want = "sha256:" + line.split()[2]
                    if want == existing["digest"]:
                        print(f"SKIP: {part_name} already on release, digest matches manifest", flush=True)
                        return 0
                    print(f"digest mismatch (release {existing['digest']} vs manifest {want}) — rebuilding", flush=True)
                    break
    # extract + pack
    t0 = time.time()
    recs = []
    with tarfile.open(part_path, "w", format=tarfile.GNU_FORMAT) as tf:
        for i, e in enumerate(part_entries):
            if e["method"] != 0:
                print(f"FAIL unsupported method {e['method']}: {e['name']}", file=sys.stderr, flush=True)
                return 1
            if e["usize"] == 0:
                if e["crc"] != 0:
                    print(f"FAIL zero-size nonzero CRC: {e['name']}", file=sys.stderr, flush=True)
                    return 1
                data, sha = b"", hashlib.sha256(b"").hexdigest()
            else:
                lh = fetch(url, e["lho"], e["lho"] + 29)
                if lh[:4] != b"PK\x03\x04":
                    print(f"FAIL bad local sig: {e['name']}", file=sys.stderr, flush=True)
                    return 1
                lfn, lef = struct.unpack_from("<HH", lh, 26)
                ds = e["lho"] + 30 + lfn + lef
                buf, crc = io.BytesIO(), 0
                for chunk in fetch_chunks(url, ds, ds + e["csize"] - 1):
                    buf.write(chunk)
                    crc = zlib.crc32(chunk, crc)
                data = buf.getvalue()
                if len(data) != e["usize"] or (crc & 0xFFFFFFFF) != e["crc"]:
                    print(f"FAIL size/CRC: {e['name']}", file=sys.stderr, flush=True)
                    return 1
                sha = hashlib.sha256(data).hexdigest()
            tp = e["name"][len("Documents/"):]
            ti = tarfile.TarInfo(tp)
            ti.size = len(data)
            ti.mtime = 0
            tf.addfile(ti, io.BytesIO(data))
            recs.append((sha, e["usize"], e["crc"], e["name"], tp))
            if (i + 1) % 25 == 0 or i == len(part_entries) - 1:
                el = time.time() - t0
                print(f"  {i+1}/{len(part_entries)} files in {el:.0f}s", flush=True)
    psize = os.path.getsize(part_path)
    psha = hashlib.sha256()
    with open(part_path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            psha.update(chunk)
    phex = psha.hexdigest()
    print(f"part built: {part_name} {psize} B sha256 {phex}", flush=True)
    with open(manifest_path, "w", encoding="utf-8") as mf:
        mf.write(f"# {manifest_name}\n# part: {part_name}\n# part-sha256 {phex}\n# part-size {psize}\n")
        mf.write("# format: sha256<TAB>size<TAB>crc32<TAB>zip-path<TAB>tar-path\n")
        for sha, size, crc, zp, tp in recs:
            mf.write(f"{sha}\t{size}\t{crc:08x}\t{zp}\t{tp}\n")
    # upload (delete a stale asset of the same name first: re-upload 422s otherwise)
    assets = list_assets(args.release_id, token)
    for nm in (part_name, manifest_name):
        old = asset_by_name(assets, nm)
        if old:
            print(f"deleting stale asset {nm} (id {old['id']})", flush=True)
            gh_api(["-X", "DELETE", f"repos/{TARGET_REPO}/releases/assets/{old['id']}"], token)
    print(f"uploading {part_name} ({psize} B)…", flush=True)
    upload_file(args.release_id, token, part_path, part_name, "application/octet-stream")
    a = asset_by_name(list_assets(args.release_id, token), part_name)
    if not a or a["size"] != psize or a.get("state") != "uploaded" or not a.get("digest"):
        print(f"VERIFY FAILED for {part_name}: {a}", file=sys.stderr, flush=True)
        return 1
    if a["digest"] != "sha256:" + phex:
        print(f"DIGEST MISMATCH for {part_name}: release {a['digest']} vs local sha256:{phex}",
              file=sys.stderr, flush=True)
        return 1
    print(f"part verified: {part_name} size={a['size']} state={a['state']} digest={a['digest']}", flush=True)
    print(f"uploading {manifest_name}…", flush=True)
    upload_file(args.release_id, token, manifest_path, manifest_name, "text/plain")
    msize = os.path.getsize(manifest_path)
    m = asset_by_name(list_assets(args.release_id, token), manifest_name)
    if not m or m["size"] != msize or m.get("state") != "uploaded":
        print(f"VERIFY FAILED for {manifest_name}: {m}", file=sys.stderr, flush=True)
        return 1
    print(f"manifest verified: {manifest_name} size={m['size']} state={m['state']}", flush=True)
    os.remove(part_path)
    os.remove(manifest_path)
    print(f"PART {args.part_no} DONE in {time.time()-t0:.0f}s", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
