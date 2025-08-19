# curl_downloader.py

A production-ready Python 3.8+ CLI tool that leverages the system `curl` binary to download files concurrently.  
This version includes enhanced filename resolution (honors `Content-Disposition`, URL basenames, and infers extensions from `Content-Type`), robust retry logic, resume support, categorization, progress bars, and atomic writes.

---

# Prerequisites

- Python 3.8 or newer  

- `curl` installed and on your `PATH` (Linux/macOS/WSL/Windows)  

- `pip` for managing Python packages  

---

# Installation

1. Clone or download this repository.  

2. Install minimal Python dependencies:

   ```bash
   pip install tqdm aiofiles

# CLI Options

| Option              | Description                                                                 | Default    |
|---------------------|-----------------------------------------------------------------------------|------------|
| `-u, --url`         | Single URL to download (can be repeated)                                   | —          |
| `-i, --input-file`  | Path to `.txt` file with one URL per non-empty, non-`#` line               | —          |
| `-o, --output-dir`  | Destination directory for downloads                                        | `downloads`  |
| `-c, --concurrency` | Number of parallel downloads                                               | 8          |
| `--retries`         | Retries per URL on transient failures (exponential backoff)                | 3          |
| `--timeout`         | Timeout in seconds for HEAD or download operations                        | 60         |
| `--resume`          | Resume partial downloads using `curl --continue-at -`                     | off        |
| `--categorize`      | Save files in `images/`, `videos/`, `archives/`, and `other/` subdirectories | off        |
| `--preserve-filename` | Trust server filename via `Content-Disposition` or URL; otherwise prevent collisions with suffixes | off |
| `--user-agent`      | Custom User-Agent string to pass to `curl`                                | —          |
| `--quiet`           | Suppress progress bars; only show errors and final summary                 | off        |
| `--no-ssl-verify`   | Skip SSL certificate verification (`-k` with `curl`)                      | off        |
| `--dry-run`         | Show planned downloads without downloading                                | off        |
| `--headers`         | Path to file with extra HTTP headers (Key: Value) per line                                | off        |
| `--test`         | Run built‑in unit tests and exit                                | off        |

## How It Works
1. Input Handling
- Accepts URLs via `-u/--url` or` -i/--input-file`
- Skips blank lines and lines starting with #
- Validates http/https and deduplicates

2. Filename Resolution
-HEAD Request for headers (Content-Disposition, Content-Type):

```bash
  curl -I -L -sS [URL]
```
- Parse Content-Disposition for filename
- Fallback: last segment of URL path (decoded)
- Default: download if none found
- Add extension if missing, using mimetypes + common fallbacks

3. Type Detection & Categorization
Maps extensions to:
- images (jpg, png, gif, …)
- videos (mp4, mkv, avi, …)
- archives (zip, tar, rar, …)
- other (everything else)
- Falls back to Content-Type when needed
- Creates subdirectories if --categorize enabled
4. Download Strategy
Builds curl command with:
`-L` (follow redirects)
`-f` (fail on HTTP errors)
`-sS` (silent but show errors)
`--continue-at` - if resuming
`-k` for SSL skip (optional)
`-A` for custom UA
`-H` for extra headers
`--output` - to stream to stdout
- Writes .part file with aiofiles, 64 KiB chunks
- Shows per‑file and overall tqdm progress bars
- Atomically renames .part → final file
- Retries with exponential backoff
5. Concurrency & Graceful Shutdown
- Limits parallel downloads via asyncio.Semaphore
- Ctrl+C cancels tasks, kills curl processes, exits cleanly
6. Summary & Exit Codes
After completion:
`Summary: downloaded=<n> failed=<m> images=<x> videos=<y> archives=<z> other=<w>`

Custom Headers File Example:

```bash
Authorization: Bearer <token>
X-API-Version: 2
```
## Testing
- Run built‑in tests:
```bash
python curl_downloader.py --test
```
- Expected:
```bash
All tests passed.
```
