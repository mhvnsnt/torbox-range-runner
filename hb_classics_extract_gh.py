#!/usr/bin/env python3
"""Runner-side HB Classics gap-fill extractor — GITHUB SLICES source,
SPAN-BATCHED fetch (GitHub Actions). v2.

History: v1 (rs_extract_gh.py pattern) fetched each file with a separate
30 B header request + 8 MB data chunks — ~2 requests per file, ~5,300
requests per part. Three parallel parts (~16,000 requests in ~6 min)
tripped the GitHub API rate limit for user 197314870 (run 45041468297:
parts 04/05 failed HTTP 403 on slice asset 660522431; part 01 succeeded).
v2 groups consecutive-in-lho files into <=48 MB spans and fetches each
span with ONE API request, carving files out of the span buffer in memory.
Request count drops to ~40-200 per part (part 04: 40 data-spans touched
by lho, part 05: 46). Files tile contiguously in lho order (median gap 0),
so spans are almost entirely HB data.

Source: Documents.zip SLICE assets on GitHub
(mhvnsnt/cn-gigaleak-documents-archive, release documents-zip-slices-v1)
via the API asset endpoint + Range header. No SOURCE_URL needed.
Verifies CRC32 per file, packs them into an uncompressed .tar part,
uploads it + its manifest to the cartoon-vault hb-classics-gap-fill-v1
release, and verifies both assets via the API. Skip logic: if the part
asset AND its manifest already exist and the manifest's recorded
part-sha256 matches the asset digest, the part is done — exit 0.

The token is interpolated in Python in upload_file — NEVER pass a literal
'Bearer $TOKEN' to curl.

Env: GH_TOKEN (TARGET_PAT, Contents:RW on the target repo AND read on the
slices repo).
Usage: hb_classics_extract_gh.py --tsv hb-classics-cd-full.tsv --start 4069 --end 6905
       --part-no 4 [--release-id 454521048]
"""
import argparse, hashlib, io, json, os, struct, subprocess, sys, tarfile, time, urllib.request, zlib

TARGET_REPO = "mhvnsnt/cn-gigaleak-cartoon-vault"
RELEASE_ID_DEFAULT = 454521048
PART_CAP = 1900000000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
GROUP_CAP = 48 * 1024 * 1024
EXTRA_SLACK = 256  # assumed max local-header extra length for grouping estimates

SRC_REPO = "mhvnsnt/cn-gigaleak-documents-archive"
SLICE = 2000000000
SLICE_MAP = None  # slice_start -> asset id

def slice_map():
    global SLICE_MAP
    if SLICE_MAP is None:
        SLICE_MAP = {}
        with open("slices-assets-20261122.tsv", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2 and parts[1].startswith("slice-") and parts[1].endswith(".bin"):
                    segs = parts[1][len("slice-"):-len(".bin")].split("-")
                    SLICE_MAP[int(segs[0])] = parts[0]
        assert len(SLICE_MAP) == 176, f"slice map has {len(SLICE_MAP)} entries, want 176"
    return SLICE_MAP

def fetch_asset_range(token, aid, start, end, tries=5):
    """GET byte range [start, end] inclusive of one slice asset via the API."""
    last = None
    api_url = f"https://api.github.com/repos/{SRC_REPO}/releases/assets/{aid}"
    for a in range(tries):
        req = urllib.request.Request(api_url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
            "Range": f"bytes={start}-{end}",
            "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                assert r.status == 206, f"range failed: HTTP {r.status}"
                data = r.read()
            assert len(data) == end - start + 1, f"short read {len(data)} != {end-start+1}"
            return data
        except Exception as e:
            last = e
            time.sleep(2 * (a + 1))
    raise RuntimeError(f"fetch failed asset {aid} [{start},{end}] after {tries}: {last}")

def fetch_zip_span(token, start, end, tries=5):
    """GET byte range [start, end] inclusive of the whole Documents.zip, split across slices."""
    smap = slice_map()
    out = bytearray()
    pos = start
    while pos <= end:
        sstart = (pos // SLICE) * SLICE
        send = min(end, sstart + SLICE - 1)
        out += fetch_asset_range(token, smap[sstart], pos - sstart, send - sstart, tries)
        pos = send + 1
    return bytes(out)

def fetch_span_chunked(token, start, end, chunk=48 * 1024 * 1024):
    """Fetch [start, end] inclusive in <=chunk API requests (a single huge
    Range request risks timeouts; v1 never tested >8 MB in one request).
    Splits by slice inside fetch_zip_span as needed."""
    out = bytearray()
    pos = start
    while pos <= end:
        out += fetch_zip_span(token, pos, min(end, pos + chunk - 1))
        pos += chunk
    return bytes(out)

def est_end(e):
    """Estimated exclusive end of a file's zip record (header + data).
    Name byte length uses UTF-8 (an over-estimate vs cp437, exact vs UTF-8);
    extra length is assumed <= EXTRA_SLACK (tail fallback covers the rare
    overrun). Used ONLY for grouping, never for correctness."""
    return e["lho"] + 30 + len(e["name"].encode("utf-8", "replace")) + EXTRA_SLACK + e["csize"]

def build_groups(entries):
    """Group consecutive-in-lho files into spans of <= GROUP_CAP bytes
    (estimated). Returns list of (group_start, group_end_est, [entries])."""
    ordered = sorted(entries, key=lambda e: e["lho"])
    groups, cur, cur_start, cur_end = [], [], None, 0
    for e in ordered:
        ee = est_end(e)
        if cur and ee - cur_start > GROUP_CAP:
            groups.append((cur_start, cur_end, cur))
            cur, cur_start, cur_end = [], None, 0
        if not cur:
            cur_start = e["lho"]
        cur.append(e)
        cur_end = max(cur_end, ee)
    if cur:
        groups.append((cur_start, cur_end, cur))
    return groups

def carve_file(token, g_start, buf, e):
    """Parse one file's local header from the group buffer (which starts at
    g_start) and return its data bytes, extending the buffer with a tail
    fetch if the estimate undershot. Verifies size + CRC32."""
    off = e["lho"] - g_start
    if off + 30 > len(buf) or buf[off:off+4] != b"PK\x03\x04":
        raise RuntimeError(f"bad local sig for {e['name']} at lho {e['lho']}")
    lfn, lef = struct.unpack_from("<HH", buf, off + 26)
    ds = e["lho"] + 30 + lfn + lef
    de = ds + e["csize"]
    if e["usize"] == 0:
        if e["crc"] != 0:
            raise RuntimeError(f"zero-size nonzero CRC: {e['name']}")
        return b""
    end_off = de - g_start
    if end_off > len(buf):
        # estimate undershot (extra field > slack, or name longer than
        # estimated): fetch the missing tail and extend the buffer
        extra = fetch_zip_span(token, g_start + len(buf), de - 1)
        buf = buf + extra
    data = buf[ds - g_start:de - g_start]
    if len(data) != e["usize"]:
        raise RuntimeError(f"size mismatch: {e['name']} got {len(data)} want {e['usize']}")
    if (zlib.crc32(data) & 0xFFFFFFFF) != e["crc"]:
        raise RuntimeError(f"CRC mismatch: {e['name']}")
    return data

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
    # NOTE: the token is interpolated here in Python. An earlier version passed
    # the literal string "Bearer $GH_UPLOAD_TOKEN" as the header value — curl
    # does not expand env vars in -H arguments, so GitHub answered 401 on
    # every part upload (Bakugan run 45024112890). The token value itself is never
    # printed or logged.
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
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        print("ERROR: GH_TOKEN env not set", file=sys.stderr)
        return 2
    smap = slice_map()
    print(f"source: GitHub slices, span-batched ({len(smap)} slice assets, {SRC_REPO})", flush=True)
    entries = load_entries(args.tsv)
    part_entries = entries[args.start:args.end]
    part_name = f"hb-classics-gap-fill-part-{args.part_no:02d}.tar"
    manifest_name = f"MANIFEST-hb-classics-gap-fill-part-{args.part_no:02d}.txt"
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
    # extract + pack (span-batched, files processed in lho order)
    t0 = time.time()
    groups = build_groups(part_entries)
    print(f"  {len(groups)} span groups (<=48 MB each) for {len(part_entries)} files", flush=True)
    recs = []
    nreq = 0
    with tarfile.open(part_path, "w", format=tarfile.GNU_FORMAT) as tf:
        done = 0
        for gi, (g_start, g_end, g_files) in enumerate(groups):
            buf = fetch_span_chunked(token, g_start, g_end - 1)
            nreq += 1
            for e in g_files:
                if e["method"] != 0:
                    print(f"FAIL unsupported method {e['method']}: {e['name']}", file=sys.stderr, flush=True)
                    return 1
                data = carve_file(token, g_start, buf, e)
                sha = hashlib.sha256(data).hexdigest()
                tp = e["name"][len("Documents/"):]
                ti = tarfile.TarInfo(tp)
                ti.size = len(data)
                ti.mtime = 0
                tf.addfile(ti, io.BytesIO(data))
                recs.append((sha, e["usize"], e["crc"], e["name"], tp))
                done += 1
                if done % 500 == 0 or done == len(part_entries):
                    el = time.time() - t0
                    print(f"  {done}/{len(part_entries)} files in {el:.0f}s (group {gi+1}/{len(groups)})", flush=True)
    print(f"  span groups fetched: {len(groups)} (+ rare tail fetches)", flush=True)
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
