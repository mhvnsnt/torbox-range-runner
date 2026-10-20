#!/usr/bin/env python3
"""
PROTOTYPE — secret-URL zip Range reader / streamer for GitHub Actions,
Kaggle kernels, Colab, or any throwaway Linux compute.

The download URL is read ONLY from the SOURCE_URL environment variable.
In GitHub Actions that env var comes from a repository secret
(Settings -> Secrets and variables -> Actions), typed into GitHub's own UI
by a human in a browser. The URL therefore never passes through chat,
agent tool parameters, git history, or workflow files. This script never
prints the URL (only scheme://host and the redacted path), and GitHub
additionally masks exact secret values in run logs.

This is a streaming rework of the proven local extractor
~/workspace/hero-parts-pull/zip_extract.py (same EOCD / Zip64 / central
directory parsing), with three differences that matter on a 14 GB runner:

  1. Nothing is ever held whole: the central directory is parsed
     incrementally, extracted files are decompressed in 1 MiB slices,
     and raw slices are emitted in 4 MiB slices. Peak RAM is a few MiB,
     peak disk is ZERO unless you explicitly ask for --out FILE.
  2. File integrity is verified against the CRC32 stored in the central
     directory (the local script did not check it).
  3. `slice` mode turns any byte range of the source into a stdout
     generator that can be piped straight into an uploads.github.com
     POST (the pattern proven by github_upload_stream.py), so a source
     archive can be re-chunked into GitHub release assets without ever
     touching the runner's disk.

Usage:
  SOURCE_URL='https://host/path/file.zip?token=...' \
    python3 range_list.py list [--out manifest.json] [--limit N]

  SOURCE_URL=... python3 range_list.py extract-one <name-or-suffix> [--out PATH]
      # --out - writes to stdout

  SOURCE_URL=... python3 range_list.py slice <start> <size> [--hash-out FILE]
      # raw source bytes -> stdout (pipe to curl), sha256 -> stderr / file
"""
import hashlib
import json
import os
import struct
import sys
import time
import urllib.request
import urllib.parse
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SLICE = 1024 * 1024          # 1 MiB network slice for extraction
CD_CHUNK = 8 * 1024 * 1024   # 8 MiB central-directory fetch chunk


def redacted(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path} (query redacted, {len(p.query)} chars)"


def _open(url, start=None, end=None, method=None, tries=5):
    """Open a (ranged) request with retry. Caller must close / consume."""
    last = None
    for a in range(tries):
        headers = {"User-Agent": UA}
        if start is not None:
            headers["Range"] = f"bytes={start}-{end}"
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=180)
            if start is not None and r.status != 206:
                r.close()
                raise RuntimeError(f"range failed: HTTP {r.status} "
                                   "(source does not honour Range?)")
            return r
        except Exception as e:  # noqa: BLE001 - retry any transient failure
            last = e
            time.sleep(2 * (a + 1))
    raise RuntimeError(f"request failed after {tries} tries: {last}")


def fetch(url, start, end):
    """Fetch one bounded range fully into memory (bounded by caller)."""
    with _open(url, start, end) as r:
        data = r.read()
    assert len(data) == end - start + 1, f"short read {len(data)} != {end-start+1}"
    return data


def fetch_iter(url, start, end, slice_size=SLICE):
    """Yield a (possibly huge) range in slices; never holds it all."""
    pos = start
    while pos <= end:
        n = min(slice_size, end - pos + 1)
        yield fetch(url, pos, pos + n - 1)
        pos += n


def source_size(url):
    """Total size: HEAD first, Range probe (bytes=0-0) as fallback —
    signed CDN links sometimes reject HEAD but always answer a probe."""
    try:
        with _open(url, method="HEAD") as r:
            n = r.headers.get("Content-Length")
            if n:
                return int(n)
    except Exception:
        pass
    with _open(url, 0, 0) as r:
        cr = r.headers.get("Content-Range", "")
        # form: bytes 0-0/123456
        if "/" in cr:
            return int(cr.rsplit("/", 1)[1])
        n = r.headers.get("Content-Length")
        if n and r.status == 200:
            return int(n)
    raise RuntimeError("could not determine source size")


def read_central_directory(url, total):
    """Parse EOCD (+Zip64) and stream-parse the central directory.
    Returns list of dicts. Peak memory ~ the manifest itself."""
    tail_len = min(total, 70000)
    tail = fetch(url, total - tail_len, total - 1)
    eocd_off = tail.rfind(b"\x50\x4b\x05\x06")
    assert eocd_off >= 0, "EOCD not found (not a zip, or comment > 64 KiB?)"
    eocd = tail[eocd_off:eocd_off + 22]
    cd_count = struct.unpack("<H", eocd[10:12])[0]
    cd_size = struct.unpack("<I", eocd[12:16])[0]
    cd_offset = struct.unpack("<I", eocd[16:20])[0]
    zip64 = False
    if cd_count == 0xFFFF or cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        loc_off = tail.rfind(b"\x50\x4b\x06\x07", 0, eocd_off)
        assert loc_off >= 0, "zip64 locator not found"
        z64_off = struct.unpack("<Q", tail[loc_off + 8:loc_off + 16])[0]
        z64 = fetch(url, z64_off, z64_off + 56 - 1)
        assert z64[:4] == b"\x50\x4b\x06\x06", "bad zip64 eocd sig"
        cd_count = struct.unpack("<Q", z64[32:40])[0]
        cd_size = struct.unpack("<Q", z64[40:48])[0]
        cd_offset = struct.unpack("<Q", z64[48:56])[0]
        zip64 = True
    print(f"central directory: {cd_count} entries, {cd_size} bytes "
          f"at offset {cd_offset}{' (zip64)' if zip64 else ''}", flush=True)

    entries = []
    buf = bytearray()
    fetched = 0
    for chunk in fetch_iter(url, cd_offset, cd_offset + cd_size - 1, CD_CHUNK):
        buf += chunk
        fetched += len(chunk)
        # parse every complete entry currently in the buffer
        while True:
            if len(buf) < 46:
                break
            assert buf[:4] == b"\x50\x4b\x01\x02", "bad CD sig"
            fn_len = struct.unpack("<H", buf[28:30])[0]
            ef_len = struct.unpack("<H", buf[30:32])[0]
            fc_len = struct.unpack("<H", buf[32:34])[0]
            entry_len = 46 + fn_len + ef_len + fc_len
            if len(buf) < entry_len:
                break
            e = bytes(buf[:entry_len])
            del buf[:entry_len]
            method = struct.unpack("<H", e[10:12])[0]
            crc = struct.unpack("<I", e[16:20])[0]
            comp = struct.unpack("<I", e[20:24])[0]
            uncomp = struct.unpack("<I", e[24:28])[0]
            lho = struct.unpack("<I", e[42:46])[0]
            name = e[46:46 + fn_len].decode("utf-8", "replace")
            if comp == 0xFFFFFFFF or uncomp == 0xFFFFFFFF or lho == 0xFFFFFFFF:
                ef = e[46 + fn_len:46 + fn_len + ef_len]
                j = 0
                vals = []
                while j + 4 <= len(ef):
                    tag, sz = struct.unpack("<HH", ef[j:j + 4])
                    if tag == 0x0001:
                        k = j + 4
                        if uncomp == 0xFFFFFFFF:
                            vals.append(struct.unpack("<Q", ef[k:k + 8])[0]); k += 8
                        if comp == 0xFFFFFFFF:
                            vals.append(struct.unpack("<Q", ef[k:k + 8])[0]); k += 8
                        if lho == 0xFFFFFFFF:
                            vals.append(struct.unpack("<Q", ef[k:k + 8])[0]); k += 8
                        break
                    j += 4 + sz
                vi = 0
                if uncomp == 0xFFFFFFFF:
                    uncomp = vals[vi]; vi += 1
                if comp == 0xFFFFFFFF:
                    comp = vals[vi]; vi += 1
                if lho == 0xFFFFFFFF:
                    lho = vals[vi]; vi += 1
            entries.append({"name": name, "method": method,
                            "crc32": f"{crc:08x}", "comp_size": comp,
                            "uncomp_size": uncomp, "local_header_offset": lho})
        print(f"  cd fetch {fetched/1e6:.1f}/{cd_size/1e6:.1f} MB, "
              f"{len(entries)} entries parsed", flush=True)
    assert len(entries) == cd_count, f"parsed {len(entries)} != EOCD count {cd_count}"
    return entries


def cmd_list(url, out=None, limit=0):
    total = source_size(url)
    print(f"source: {redacted(url)}")
    print(f"source size: {total} bytes ({total/1e9:.3f} GB)")
    entries = read_central_directory(url, total)
    files = [e for e in entries if e["uncomp_size"] or e["comp_size"]]
    print(f"TOTAL: {len(entries)} entries, {len(files)} files, "
          f"{sum(e['uncomp_size'] for e in entries)/1e9:.3f} GB uncompressed")
    show = entries[:limit] if limit else entries
    for e in show:
        print(f"  {e['uncomp_size']:>12}  method={e['method']}  "
              f"crc={e['crc32']}  {e['name']}")
    if limit and len(entries) > limit:
        print(f"  ... ({len(entries)-limit} more in manifest)")
    if out:
        with open(out, "w") as f:
            json.dump({"source_size": total, "entry_count": len(entries),
                       "entries": entries}, f, indent=1)
        print(f"manifest written: {out}")
    return 0


def cmd_extract_one(url, match, out):
    total = source_size(url)
    entries = read_central_directory(url, total)
    hits = [e for e in entries if e["name"] == match or e["name"].endswith(match)]
    if len(hits) != 1:
        print(f"extract-one needs exactly 1 match for {match!r}, got {len(hits)}",
              file=sys.stderr)
        for h in hits[:20]:
            print(f"  {h['name']}", file=sys.stderr)
        return 2
    e = hits[0]
    print(f"extracting: {e['name']}  comp={e['comp_size']} "
          f"uncomp={e['uncomp_size']} method={e['method']} crc={e['crc32']}")
    if e["comp_size"] == 0 and e["uncomp_size"] == 0:
        data_crc = 0
        written = 0
        sink = None
    else:
        lh = fetch(url, e["local_header_offset"], e["local_header_offset"] + 29)
        assert lh[:4] == b"\x50\x4b\x03\x04", "bad local header sig"
        lfn = struct.unpack("<H", lh[26:28])[0]
        lef = struct.unpack("<H", lh[28:30])[0]
        data_start = e["local_header_offset"] + 30 + lfn + lef
        sink = sys.stdout.buffer if out == "-" else open(out, "wb")
        written = 0
        crc = 0
        if e["method"] == 8:
            d = zlib.decompressobj(-15)
            for chunk in fetch_iter(url, data_start, data_start + e["comp_size"] - 1):
                data = d.decompress(chunk)
                if data:
                    sink.write(data); written += len(data)
                    crc = zlib.crc32(data, crc)
            tail = d.flush()
            if tail:
                sink.write(tail); written += len(tail)
                crc = zlib.crc32(tail, crc)
        elif e["method"] == 0:
            for chunk in fetch_iter(url, data_start, data_start + e["comp_size"] - 1):
                sink.write(chunk); written += len(chunk)
                crc = zlib.crc32(chunk, crc)
        else:
            print(f"unsupported compression method {e['method']}", file=sys.stderr)
            return 2
        data_crc = crc & 0xFFFFFFFF
        if out != "-":
            sink.close()
    ok_size = written == e["uncomp_size"]
    ok_crc = f"{data_crc:08x}" == e["crc32"]
    print(f"written: {written} bytes  size_ok={ok_size}  "
          f"crc_computed={data_crc:08x} crc_ok={ok_crc}")
    if out != "-":
        print(f"out: {out}")
    return 0 if (ok_size and ok_crc) else 1


def cmd_slice(url, start, size, hash_out=None):
    """Stream raw source bytes [start, start+size) to stdout, 4 MiB slices.
    Pipe into: curl --data-binary @- https://uploads.github.com/...
    sha256 is reported on stderr (stdout is the byte stream)."""
    h = hashlib.sha256()
    n = 0
    out = sys.stdout.buffer
    for chunk in fetch_iter(url, start, start + size - 1, 4 * 1024 * 1024):
        out.write(chunk)
        h.update(chunk)
        n += len(chunk)
    out.flush()
    digest = h.hexdigest()
    print(f"slice bytes={n} sha256={digest}", file=sys.stderr)
    if hash_out:
        with open(hash_out, "w") as f:
            f.write(digest + "\n")
    return 0 if n == size else 1


def cmd_upload_slice(url, start, size, hash_out=None):
    """Single-pass: stream source range [start, start+size) straight into an
    uploads.github.com POST using chunked transfer encoding, hashing as the
    bytes flow. Replaces the Lane 4 curl pipe, whose --data-binary @- buffers
    the ENTIRE stdin body in RAM (2 GiB slice -> 'curl: option --data-binary:
    out of memory', verified in run 39479148383 job logs, slice lane 2026-10-20).
    Target URL + bearer token come from env (UPLOAD_URL, GH_TOKEN); neither is
    printed. Prints 'slice bytes=N sha256=H' on stderr like slice mode."""
    import http.client
    import urllib.parse as _up
    upload_url = os.environ.get("UPLOAD_URL", "").strip()
    token = os.environ.get("GH_TOKEN", "").strip()
    if not upload_url or not token:
        print("ERROR: UPLOAD_URL / GH_TOKEN env not set", file=sys.stderr)
        return 2
    p = _up.urlsplit(upload_url)
    conn = http.client.HTTPSConnection(p.netloc, timeout=300)
    path = p.path + ("?" + p.query if p.query else "")
    conn.putrequest("POST", path)
    conn.putheader("Authorization", f"Bearer {token}")
    conn.putheader("Content-Type", "application/octet-stream")
    # uploads.github.com REQUIRES Content-Length (chunked -> HTTP 400
    # 'Bad Content-Length', run 39486224869 job logs). The slice size is
    # known exactly, so declare it and stream the body raw — still no
    # buffering anywhere, RAM stays at one 4 MiB slice.
    conn.putheader("Content-Length", str(size))
    conn.endheaders()
    h = hashlib.sha256()
    n = 0
    for chunk in fetch_iter(url, start, start + size - 1, 4 * 1024 * 1024):
        h.update(chunk)
        n += len(chunk)
        conn.send(chunk)
    resp = conn.getresponse()
    body = resp.read()[:400]
    digest = h.hexdigest()
    print(f"slice bytes={n} sha256={digest} upload_status={resp.status}",
          file=sys.stderr)
    if hash_out:
        with open(hash_out, "w") as f:
            f.write(digest + "\n")
    if resp.status >= 400 or n != size:
        print(f"upload failed: HTTP {resp.status} body={body!r}", file=sys.stderr)
        return 1
    return 0


def main(argv):
    url = os.environ.get("SOURCE_URL", "").strip()
    if not url:
        print("ERROR: SOURCE_URL env var is not set. In GitHub Actions set it "
              "from a repository secret; locally export it in your shell. "
              "Never put the URL on the command line (shell history / logs).",
              file=sys.stderr)
        return 2
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "list":
        out, limit = None, 0
        i = 0
        while i < len(args):
            if args[i] == "--out":
                out = args[i + 1]; i += 2
            elif args[i] == "--limit":
                limit = int(args[i + 1]); i += 2
            else:
                raise SystemExit(f"unknown arg {args[i]}")
        return cmd_list(url, out, limit)
    if cmd == "extract-one":
        match = args[0]
        out = os.path.basename(match.rstrip("/")) or "extracted.bin"
        if "--out" in args:
            out = args[args.index("--out") + 1]
        return cmd_extract_one(url, match, out)
    if cmd == "slice":
        start, size = int(args[0]), int(args[1])
        hash_out = None
        if "--hash-out" in args:
            hash_out = args[args.index("--hash-out") + 1]
        return cmd_slice(url, start, size, hash_out)
    if cmd == "upload-slice":
        start, size = int(args[0]), int(args[1])
        hash_out = None
        if "--hash-out" in args:
            hash_out = args[args.index("--hash-out") + 1]
        return cmd_upload_slice(url, start, size, hash_out)
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
