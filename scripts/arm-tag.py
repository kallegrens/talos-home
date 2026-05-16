#!/usr/bin/env python3
"""arm-tag — Tag + rename ARM-ripped opus files from a nyamusik.se product page.

Usage:
    arm-tag.py <URL> [--dir DIR] [--music-root DIR] [--dry-run]

Workflow:
    1. Scrape album title, artist, track list, cover image from URL
    2. Auto-detect newest Unknown Artist/Unknown Album_* dir (or use --dir)
    3. Download cover art and embed into each .opus file
    4. Write TITLE/ARTIST/ALBUM/TRACKNUMBER/TRACKTOTAL tags (idempotent)
    5. Rename files (01 - Title.opus) and directory (Artist/Album/)

Requirements: opustags

Examples:
    arm-tag.py https://www.nyamusik.se/musik/barn/jesus-ar-stark---cd-399449
    arm-tag.py https://www.nyamusik.se/musik/barn/... --dry-run
    arm-tag.py https://www.nyamusik.se/musik/barn/... --dir /mnt/.../Unknown\\ Artist/Unknown\\ Album_abc123
"""

import argparse
import html
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

DEFAULT_MUSIC_ROOT = "/mnt/plex-media/Audiobooks/Originals/automatic-ripping-machine"


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def download_file(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        f.write(r.read())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def normalize_title(title: str) -> str:
    """Strip the '(sång)' track-type label; title-case fully-uppercase titles."""
    title = re.sub(r"\s*\(sång\)\s*$", "", title, flags=re.IGNORECASE).strip()
    # Detect stylistic ALL-CAPS titles (ignore parenthetical subtitles when checking)
    main_part = re.sub(r"\([^)]*\)", "", title).strip()
    if (
        main_part
        and main_part == main_part.upper()
        and any(c.isalpha() for c in main_part)
    ):
        title = title.title()
    return title


def parse_tracks(description: str) -> list[tuple[int, str]]:
    """Extract a numbered track list from a description string."""
    description = html.unescape(description)
    m = re.search(r"(?<!\d)1\.\s+\S", description)
    if not m:
        return []
    block = description[m.start() :]
    # Negative lookbehind (?<!\d) prevents splitting "10." into "1." + "0."
    parts = re.split(r"(?<!\d)(?=\d{1,2}\.\s+)", block)
    tracks = []
    for part in parts:
        tm = re.match(r"(\d{1,2})\.\s+(.+?)$", part.strip(), re.MULTILINE)
        if tm:
            num = int(tm.group(1))
            title = normalize_title(tm.group(2).strip())
            tracks.append((num, title))
    return sorted(tracks)


def scrape(page_html: str) -> dict:
    """Parse nyamusik.se product page for album metadata."""

    # JSON-LD structured data is the most reliable source
    ld_blocks = re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        page_html,
        re.DOTALL | re.IGNORECASE,
    )
    product = {}
    for block in ld_blocks:
        try:
            data = json.loads(block)
            entity = data.get("mainEntity", data)
            if entity.get("@type") == "Product":
                product = entity
                break
        except json.JSONDecodeError:
            continue

    # Album name — strip " - CD" suffix
    name = product.get("name", "")
    album = re.sub(r"\s*-\s*CD\s*$", "", name, flags=re.IGNORECASE).strip()

    # Cover image (direct full-resolution URL in JSON-LD)
    cover_url = product.get("image", "")

    # Track list embedded in the description field
    tracks = parse_tracks(product.get("description", ""))

    # Artist is not in JSON-LD for this site — parse from HTML link
    m = re.search(r'musik/artist/[^"]+">([^<]+)</a>', page_html)
    artist = m.group(1).strip() if m else ""

    return {"album": album, "artist": artist, "cover_url": cover_url, "tracks": tracks}


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def find_newest_unknown_dir(music_root: Path) -> Path | None:
    candidates = list(music_root.glob("Unknown Artist/Unknown Album_*"))
    # Also match plain "Unknown Album" (created when abcde has no discid suffix)
    candidates += list(music_root.glob("Unknown Artist/Unknown Album"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def safe_filename(name: str) -> str:
    """Strip characters not allowed in directory/file names."""
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------


def tag_file(
    opus_file: Path,
    *,
    artist: str,
    album: str,
    title: str,
    track_num: int,
    track_total: int,
    cover: Path,
    dry_run: bool,
) -> None:
    cmd = [
        "opustags",
        "-i",
        "-d",
        "ARTIST",
        "-s",
        f"ARTIST={artist}",
        "-d",
        "ALBUMARTIST",
        "-s",
        f"ALBUMARTIST={artist}",
        "-d",
        "ALBUM",
        "-s",
        f"ALBUM={album}",
        "-d",
        "TITLE",
        "-s",
        f"TITLE={title}",
        "-d",
        "TRACKNUMBER",
        "-s",
        f"TRACKNUMBER={track_num:02d}",
        "-d",
        "TRACKTOTAL",
        "-s",
        f"TRACKTOTAL={track_total}",
        "--set-cover",
        str(cover),
        str(opus_file),
    ]
    if dry_run:
        print("    " + subprocess.list2cmdline(cmd))
        return
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # opustags warns about chmod on NFS — suppress, surface real errors
        real_errors = [
            l for l in result.stderr.splitlines() if "Could not set mode" not in l
        ]
        if real_errors:
            print(f"  WARNING: {'; '.join(real_errors)}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tag + rename ARM-ripped opus files from a nyamusik.se product page",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", help="Product page URL (nyamusik.se)")
    parser.add_argument(
        "--dir",
        metavar="DIR",
        help="Album directory containing *.opus files (overrides auto-detect)",
    )
    parser.add_argument(
        "--music-root",
        metavar="DIR",
        default=DEFAULT_MUSIC_ROOT,
        help=f"ARM music NFS mount root (default: {DEFAULT_MUSIC_ROOT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making any changes",
    )
    args = parser.parse_args()

    music_root = Path(args.music_root)

    # ------------------------------------------------------------------
    # 1. Scrape metadata
    # ------------------------------------------------------------------
    print(f"Fetching {args.url} ...")
    page_html = fetch(args.url)
    meta = scrape(page_html)

    album = meta["album"]
    artist = meta["artist"]
    cover_url = meta["cover_url"]
    tracks = meta["tracks"]

    if not album or not artist or not tracks:
        print(
            f"ERROR: Parsing failed — album={album!r}  artist={artist!r}  tracks={len(tracks)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"  Album:  {album}")
    print(f"  Artist: {artist}")
    print(f"  Cover:  {cover_url}")
    print(f"  Tracks ({len(tracks)}):")
    for n, t in tracks:
        print(f"    {n:02d}. {t}")

    # ------------------------------------------------------------------
    # 2. Locate album directory
    # ------------------------------------------------------------------
    if args.dir:
        album_dir = Path(args.dir)
        if not album_dir.is_dir():
            print(f"ERROR: Directory not found: {album_dir}", file=sys.stderr)
            sys.exit(1)
    else:
        album_dir = find_newest_unknown_dir(music_root)
        if not album_dir:
            print(
                f"ERROR: No 'Unknown Artist/Unknown Album_*' found under {music_root}. "
                "Use --dir to specify explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"\nAuto-detected directory: {album_dir}")

    opus_files = sorted(album_dir.glob("*.opus"))
    if not opus_files:
        print(f"ERROR: No .opus files in {album_dir}", file=sys.stderr)
        sys.exit(1)

    if len(opus_files) != len(tracks):
        print(
            f"WARNING: {len(opus_files)} .opus files but {len(tracks)} tracks in metadata"
        )

    # ------------------------------------------------------------------
    # 3. Download cover art
    # ------------------------------------------------------------------
    cover_path = album_dir / "cover.jpg"
    print(f"\nDownloading cover art → {cover_path.name}")
    if not args.dry_run:
        download_file(cover_url, cover_path)

    # ------------------------------------------------------------------
    # 4. Tag files
    # ------------------------------------------------------------------
    track_map = {n: t for n, t in tracks}
    print(f"\nTagging {len(opus_files)} files...")
    for opus_file in opus_files:
        m = re.match(r"^(\d{1,2})", opus_file.name)
        if not m:
            print(f"  SKIP (no leading track number): {opus_file.name}")
            continue
        track_num = int(m.group(1))
        title = track_map.get(track_num, f"Track {track_num}")
        print(f"  {track_num:02d}: {title}")
        tag_file(
            opus_file,
            artist=artist,
            album=album,
            title=title,
            track_num=track_num,
            track_total=len(tracks),
            cover=cover_path,
            dry_run=args.dry_run,
        )

    # ------------------------------------------------------------------
    # 5. Rename files: "01 - Title.opus"
    # ------------------------------------------------------------------
    print("\nRenaming files...")
    for opus_file in sorted(album_dir.glob("*.opus")):
        m = re.match(r"^(\d{1,2})", opus_file.name)
        if not m:
            continue
        track_num = int(m.group(1))
        title = track_map.get(track_num, f"Track {track_num}")
        new_name = f"{track_num:02d} - {safe_filename(title)}.opus"
        if opus_file.name != new_name:
            print(f"  {opus_file.name!r} → {new_name!r}")
            if not args.dry_run:
                opus_file.rename(opus_file.parent / new_name)
        else:
            print(f"  {opus_file.name!r} (unchanged)")

    # ------------------------------------------------------------------
    # 6. Rename directory structure: music_root/Artist/Album/
    # ------------------------------------------------------------------
    new_album_dir = music_root / safe_filename(artist) / safe_filename(album)
    if album_dir.resolve() != new_album_dir.resolve():
        print(f"\nRenaming directory:")
        try:
            print(f"  {album_dir.relative_to(music_root)}")
            print(f"  → {new_album_dir.relative_to(music_root)}")
        except ValueError:
            print(f"  {album_dir}")
            print(f"  → {new_album_dir}")
        if not args.dry_run:
            new_album_dir.parent.mkdir(parents=True, exist_ok=True)
            if new_album_dir.exists():
                # Target already exists (e.g. from a previous partial rip).
                # Move files individually then remove the now-empty source dir.
                import shutil

                for f in album_dir.iterdir():
                    shutil.move(str(f), str(new_album_dir / f.name))
                album_dir.rmdir()
            else:
                album_dir.rename(new_album_dir)
            try:
                album_dir.parent.rmdir()  # clean up empty "Unknown Artist/" dir
            except OSError:
                pass
    else:
        print(f"\nDirectory already correctly named: {album_dir.name}")

    print("\nDone.")


if __name__ == "__main__":
    main()
