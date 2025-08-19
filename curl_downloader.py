#!/usr/bin/env python3
"""
curl_downloader.py - A fast, robust CLI for downloading files using curl.

Usage Examples:
  python curl_downloader.py -u "https://example.com/path/photo.jpg"
  python curl_downloader.py -i urls.txt -o ./mydownloads -c 16 --categorize
  python curl_downloader.py -i urls.txt --resume --retries 5

Sample Output:
  Total:   3/3 [##########] 100%|█| 3 files downloaded
    ✔ photo.jpg → downloads/images/photo.jpg
    ✔ video.mp4 → downloads/videos/video.mp4
    ✔ archive.zip → downloads/archives/archive.zip
  Summary: downloaded=3 failed=0 skipped=0 images=1 videos=1 archives=1 other=0
"""

import argparse
import asyncio
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import aiofiles
from tqdm import tqdm

# Supported file categories
EXT_CATEGORIES: Dict[str, List[str]] = {
    "images": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg"],
    "videos": ["mp4", "mkv", "webm", "avi", "mov"],
    "archives": ["zip", "tar", "tgz", "tar.gz", "rar"],
}


def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments.
    """
    parser = argparse.ArgumentParser(
        description="Download files concurrently using curl with Python CLI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=False)
    src.add_argument("-u", "--url", action="append", help="Single URL to download.")
    src.add_argument(
        "-i", "--input-file", help="Path to .txt file with one URL per line."
    )

    parser.add_argument(
        "-o",
        "--output-dir",
        default="downloads",
        help="Destination directory for downloads.",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=8,
        help="Number of parallel downloads.",
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="Number of retries per URL on failure."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout (seconds) per download or HEAD request.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Attempt resuming partially downloaded files.",
    )
    parser.add_argument(
        "--categorize",
        action="store_true",
        help="Save files into subfolders by type (images/, videos/, archives/, other/).",
    )
    parser.add_argument(
        "--preserve-filename",
        action="store_true",
        help="Use server filename or URL basename; otherwise avoid collisions with suffixes.",
    )
    parser.add_argument(
        "--user-agent", default=None, help="Custom User-Agent for curl."
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Only show errors, no progress bars."
    )
    parser.add_argument(
        "--no-ssl-verify",
        action="store_true",
        help="Pass -k/--insecure to curl to skip SSL verification.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate URLs and show what would be downloaded without downloading.",
    )
    parser.add_argument(
        "--headers",
        help="Path to file containing extra headers (one per line, 'Key: Value').",
    )
    parser.add_argument(
        "--test", action="store_true", help="Run internal tests and exit."
    )

    return parser.parse_args()


def is_valid_url(url: str) -> bool:
    """
    Simple URL validation: http:// or https:// and non-empty netloc.
    """
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.netloc)


def load_urls(args: argparse.Namespace) -> List[str]:
    """
    Load and dedupe URLs from --url and/or --input-file.
    """
    urls: List[str] = []
    if args.url:
        urls.extend(args.url)
    if args.input_file:
        path = Path(args.input_file)
        if not path.is_file():
            print(f"Input file not found: {args.input_file}", file=sys.stderr)
            sys.exit(1)
        for line in path.read_text().splitlines():
            trimmed = line.strip()
            if not trimmed or trimmed.startswith("#"):
                continue
            urls.append(trimmed)

    seen = set()
    valid = []
    for u in urls:
        if not is_valid_url(u):
            print(f"Invalid URL skipped: {u}", file=sys.stderr)
            continue
        if u not in seen:
            seen.add(u)
            valid.append(u)
    return valid


def ext_to_category(ext: str) -> str:
    """
    Map file extension to category.
    """
    ext = ext.lower().lstrip(".")
    for cat, exts in EXT_CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"


async def run_head(
    url: str, args: argparse.Namespace, extra_headers: List[str]
) -> Dict[str, str]:
    """
    Perform a HEAD request via curl -I to retrieve headers.
    """
    cmd = ["curl", "-I", "-L", "-sS"]
    if args.no_ssl_verify:
        cmd.append("-k")
    if args.user_agent:
        cmd += ["-A", args.user_agent]
    for hdr in extra_headers:
        cmd += ["-H", hdr]
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=args.timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"HEAD request timed out: {url}")

    headers: Dict[str, str] = {}
    for line in out.decode(errors="ignore").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    return headers


def parse_content_disposition(cd: str) -> Optional[str]:
    """
    Parse filename from Content-Disposition header.
    """
    # e.g. attachment; filename="fname.ext" or filename*=UTF-8''fname.ext
    m = re.search(r"filename\*?=(?:UTF-8''\s*)?\"?([^\";]+)\"?", cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1))
    return None


def prepare_output_path(
    raw_name: str,
    category: str,
    args: argparse.Namespace,
    counters: Dict[str, int],
) -> Tuple[Path, int]:
    """
    Determine final output path, handle categorization, collisions, create dirs.
    Returns (final_path, starting_offset).
    """
    outdir = Path(args.output_dir)
    if args.categorize:
        outdir = outdir / category
    outdir.mkdir(parents=True, exist_ok=True)

    name = raw_name or "download"
    base, ext = os.path.splitext(name)
    candidate = outdir / name

    if not args.preserve_filename:
        idx = 1
        while candidate.exists():
            candidate = outdir / f"{base}_{idx}{ext}"
            idx += 1

    part_path = candidate.with_suffix(candidate.suffix + ".part")
    offset = 0
    if args.resume and part_path.exists():
        offset = part_path.stat().st_size

    return candidate, offset


async def download_with_curl(
    url: str,
    args: argparse.Namespace,
    sem: asyncio.Semaphore,
    pbar_mgr: "ProgressManager",
    counters: Dict[str, int],
    extra_headers: List[str],
) -> Tuple[str, bool, Optional[str]]:
    """
    Download a single URL using curl, streaming to disk with tqdm.
    Returns (url, success, final_path or error_message).
    """
    await sem.acquire()
    slot = pbar_mgr.acquire()
    try:
        # 1) HEAD request for metadata
        try:
            headers = await run_head(url, args, extra_headers)
        except Exception as e:
            if args.dry_run:
                # Simulate path even if HEAD fails
                raw = urlparse(url).path
                fn = unquote(Path(raw).name) or "download"
                cat = ext_to_category(Path(fn).suffix)
                fp, _ = prepare_output_path(fn, cat, args, counters)
                return url, True, str(fp)
            return url, False, f"HEAD failed: {e}"

        # Content-Length
        cl = headers.get("content-length")
        total = int(cl) if cl and cl.isdigit() else None

        # --- FILENAME RESOLUTION ---

        # 1) Content-Disposition
        cd = headers.get("content-disposition", "")
        filename: Optional[str] = None
        if cd:
            filename = parse_content_disposition(cd)

        # 2) URL basename fallback
        if not filename:
            raw = urlparse(url).path
            filename = unquote(Path(raw).name)

        # 3) Ensure non-empty
        if not filename:
            filename = "download"

        # 4) If no extension, derive from Content-Type
        if not Path(filename).suffix:
            ctype = headers.get("content-type", "").split(";", 1)[0].lower()
            ext = None
            if ctype:
                guessed = mimetypes.guess_extension(ctype)
                if guessed:
                    ext = guessed
                else:
                    # Common fallbacks
                    if ctype == "image/jpeg":
                        ext = ".jpg"
                    elif ctype == "image/png":
                        ext = ".png"
                    elif ctype == "video/mp4":
                        ext = ".mp4"
                    elif ctype in ("application/zip", "application/x-zip-compressed"):
                        ext = ".zip"
            if ext:
                filename += ext

        # Category detection
        ext = Path(filename).suffix
        category = ext_to_category(ext.lstrip("."))
        if category == "other" and headers.get("content-type"):
            ctype = headers["content-type"].split(";", 1)[0].lower()
            if ctype.startswith("image/"):
                category = "images"
            elif ctype.startswith("video/"):
                category = "videos"
            elif ctype in ("application/zip", "application/x-tar"):
                category = "archives"

        # Prepare paths
        final_path, offset = prepare_output_path(filename, category, args, counters)
        part_path = final_path.with_suffix(final_path.suffix + ".part")

        if args.dry_run:
            return url, True, str(final_path)

        # Cleanup non-resume parts
        if part_path.exists() and not args.resume:
            part_path.unlink()

        # Build curl download command
        cmd = ["curl", "-L", "-f", "-sS", "--output", "-"]
        if args.no_ssl_verify:
            cmd.append("-k")
        if args.user_agent:
            cmd += ["-A", args.user_agent]
        if args.resume:
            cmd += ["--continue-at", "-"]
        for h in extra_headers:
            cmd += ["-H", h]
        cmd.append(url)

        # Setup tqdm bar
        bar = None
        if not args.quiet:
            bar = tqdm(
                total=total,
                unit="B",
                unit_scale=True,
                desc=final_path.name[:20],
                position=slot,
                initial=offset,
                leave=False,
            )

        # Download with retries
        attempt = 0
        backoff = 1.0
        while attempt <= args.retries:
            attempt += 1
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                mode = "ab" if args.resume else "wb"
                async with aiofiles.open(part_path, mode) as f:
                    while True:
                        chunk = await asyncio.wait_for(
                            proc.stdout.read(64 * 1024), timeout=args.timeout
                        )
                        if not chunk:
                            break
                        await f.write(chunk)
                        if bar:
                            bar.update(len(chunk))
                rc = await proc.wait()
                if rc != 0:
                    err = (await proc.stderr.read()).decode(errors="ignore").strip()
                    raise RuntimeError(f"curl exited {rc}: {err}")

                if bar:
                    bar.close()
                part_path.rename(final_path)
                counters[category] += 1
                return url, True, str(final_path)

            except (asyncio.TimeoutError, RuntimeError) as e:
                if bar:
                    bar.clear()
                if attempt > args.retries:
                    if bar:
                        bar.close()
                    if part_path.exists() and not args.resume:
                        part_path.unlink()
                    return url, False, str(e)
                await asyncio.sleep(backoff)
                backoff *= 2

    finally:
        pbar_mgr.release(slot)
        sem.release()


class ProgressManager:
    """
    Manage slot-based tqdm positions for concurrent bars.
    """

    def __init__(self, total_slots: int):
        self._slots = asyncio.Queue()
        for i in range(1, total_slots + 1):
            self._slots.put_nowait(i)

    def acquire(self) -> int:
        return self._slots.get_nowait()

    def release(self, slot: int):
        self._slots.put_nowait(slot)


async def run_concurrent_downloads(
    urls: List[str], args: argparse.Namespace
) -> Tuple[int, int, Dict[str, int]]:
    """
    Schedule and await all download tasks with concurrency control.
    Returns (success_count, failure_count, category_counters).
    """
    sem = asyncio.Semaphore(args.concurrency)
    pbar_mgr = ProgressManager(args.concurrency)
    overall = tqdm(
        total=len(urls),
        desc="Total",
        unit="file",
        position=0,
        disable=args.quiet,
    )

    extra_headers: List[str] = []
    if args.headers:
        hf = Path(args.headers)
        if not hf.is_file():
            print(f"Headers file not found: {args.headers}", file=sys.stderr)
            sys.exit(1)
        for line in hf.read_text().splitlines():
            h = line.strip()
            if h and ":" in h:
                extra_headers.append(h)

    counters = {"images": 0, "videos": 0, "archives": 0, "other": 0}
    success = 0
    failure = 0

    tasks = [
        asyncio.create_task(
            download_with_curl(url, args, sem, pbar_mgr, counters, extra_headers)
        )
        for url in urls
    ]

    for coro in asyncio.as_completed(tasks):
        url, ok, info = await coro
        overall.update(1)
        if ok:
            success += 1
            if args.quiet:
                print(f"✔ {url} -> {info}")
        else:
            failure += 1
            print(f"✖ {url} : {info}", file=sys.stderr)

    overall.close()
    return success, failure, counters


def run_tests():
    """
    Simple unit tests for extension->category and URL validation.
    """
    assert ext_to_category(".jpg") == "images"
    assert ext_to_category("mp4") == "videos"
    assert ext_to_category("tar.gz") == "archives"
    assert ext_to_category("unknown") == "other"
    assert is_valid_url("http://example.com")
    assert not is_valid_url("ftp://nope.com")
    print("All tests passed.")


def main():
    args = parse_args()
    if args.test:
        run_tests()
        sys.exit(0)

    if not args.url and not args.input_file:
        print("Error: specify --url or --input-file", file=sys.stderr)
        sys.exit(1)

    urls = load_urls(args)
    if not urls:
        print("No valid URLs to download.", file=sys.stderr)
        sys.exit(1)

    try:
        success, failure, counters = asyncio.run(
            run_concurrent_downloads(urls, args)
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting.", file=sys.stderr)
        sys.exit(1)

    print(
        f"Summary: downloaded={success} failed={failure} "
        f"images={counters['images']} videos={counters['videos']} "
        f"archives={counters['archives']} other={counters['other']}"
    )
    sys.exit(0 if failure == 0 else 1)


if __name__ == "__main__":
    main()
