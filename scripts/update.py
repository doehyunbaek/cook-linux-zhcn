#!/usr/bin/env python3
"""Build data/status.json from linux-doc lore and Alex Shi's docs-next tree.

The collector deliberately uses conservative heuristics. Lore supplies patch
metadata, and git patch subjects identify work merged into docs-next.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LORE = "https://lore.kernel.org/linux-doc/"
TREE = "https://git.kernel.org/pub/scm/linux/kernel/git/alexs/linux.git"
GITILES_LOG = (
    "https://kernel.googlesource.com/pub/scm/linux/kernel/git/alexs/linux.git/"
    "+log/refs/heads/docs-next"
)
USER_AGENT = "linux-doc-cook/0.1 (+https://github.com/doehyunbaek/linux-doc-cook)"
PATCH_RE = re.compile(
    r"^\[(?:RFC\s+)?PATCH(?:\s+(?P<version>v\d+))?"
    r"(?:\s+(?P<part>\d+)/(?P<total>\d+))?\]\s*(?P<title>.+)$",
    re.IGNORECASE,
)
PREFIX_RE = re.compile(r"^(?:docs?|Documentation)/(?:translations/)?zh_CN(?::|/)", re.I)
TOPIC_STOP_WORDS = {
    "add", "admin-guide", "and", "chinese", "cleanup", "config", "doc",
    "docs", "documentation", "fix", "formatting", "for", "how-to", "index",
    "refine", "rst", "subsystem", "sync", "the", "translation",
    "translations", "update", "with", "wording",
}
MAX_REROLL_GAP_DAYS = 60
COLD_AFTER_DAYS = 30
ATOM = {
    "a": "http://www.w3.org/2005/Atom",
    "thr": "http://purl.org/syndication/thread/1.0",
}
LOG = logging.getLogger("linux-doc-cook")


@dataclass
class Message:
    message_id: str
    subject: str
    author: str
    email: str
    date: str
    url: str
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)


@dataclass
class Series:
    key: str
    author: str
    email: str
    date: str
    version: int
    total: int
    title: str
    patches: list[dict[str, Any]]
    source: str
    status: str = "cooking"
    note: str = "Needs review."
    references: list[str] = field(default_factory=list)
    previous_versions: list[dict[str, Any]] = field(default_factory=list)


def fetch(url: str) -> bytes:
    LOG.debug("Fetching %s", url)
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = response.read()
        LOG.debug(
            "Fetched %s bytes from %s in %.2fs",
            len(payload),
            url,
            time.monotonic() - started,
        )
        return payload


def text(node: ET.Element | None, path: str) -> str:
    found = node.find(path, ATOM) if node is not None else None
    return (found.text or "").strip() if found is not None else ""


def atom_messages(query: str, limit: int) -> list[Message]:
    """Read matching messages from lore's Atom search endpoint."""
    url = f"{LORE}?{urllib.parse.urlencode({'q': query, 'x': 'A'})}"
    LOG.info("Querying lore for up to %d messages", limit)
    LOG.debug("Lore query: %s", query)
    root = ET.fromstring(fetch(url))
    messages: list[Message] = []
    for entry in root.findall("a:entry", ATOM)[:limit]:
        link = entry.find("a:link", ATOM)
        author = entry.find("a:author", ATOM)
        href = link.get("href", "") if link is not None else ""
        message_id = urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])
        reply = entry.find("thr:in-reply-to", ATOM)
        messages.append(
            Message(
                message_id=message_id,
                subject=text(entry, "a:title"),
                author=text(author, "a:name"),
                email=text(author, "a:email"),
                date=text(entry, "a:updated"),
                url=href,
                in_reply_to=urllib.parse.unquote(
                    (reply.get("href", "").rstrip("/").rsplit("/", 1)[-1])
                    if reply is not None else ""
                ),
            )
        )
    LOG.info("Lore returned %d messages", len(messages))
    return messages


def patch_info(subject: str) -> dict[str, Any] | None:
    match = PATCH_RE.match(subject)
    if not match:
        return None
    part_text = match.group("part")
    total_text = match.group("total")
    title = match.group("title").strip()
    if not PREFIX_RE.match(title):
        return None
    version_text = match.group("version") or "v1"
    return {
        "version": int(version_text[1:]),
        "part": int(part_text) if part_text is not None else 1,
        "total": int(total_text) if total_text is not None else 1,
        "title": title,
    }


def normalize_title(title: str) -> str:
    title = PREFIX_RE.sub("", title)
    title = re.sub(r"\s+", " ", title).strip().lower()
    return title


def topic_tokens(title: str) -> set[str]:
    """Return meaningful tokens used to associate renamed rerolls."""
    return {
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", normalize_title(title))
        if len(token) >= 3 and token not in TOPIC_STOP_WORDS
    }


def series_key(message: Message, info: dict[str, Any]) -> str:
    # A cover and its patches normally share the same References root.  Broken
    # threading falls back to author + normalized cover/patch title.
    thread = message.references[0] if message.references else message.in_reply_to
    if info["part"] == 0:
        thread = message.message_id
    identity = thread or f"{message.email}:{normalize_title(info['title'])}"
    return hashlib.sha1(identity.encode()).hexdigest()[:12]


def collect_series(messages: list[Message]) -> list[Series]:
    parsed: list[tuple[Message, dict[str, Any]]] = []
    for message in messages:
        info = patch_info(message.subject)
        if info:
            parsed.append((message, info))

    # Group by the cover id. Atom exposes in-reply-to, including an id for a
    # cover that lore itself may not have archived. The fallback handles senders
    # that did not thread a multi-patch series at all.
    groups: dict[tuple[str, int], list[tuple[Message, dict[str, Any]]]] = defaultdict(list)
    for message, info in parsed:
        thread = message.message_id if info["part"] == 0 else message.in_reply_to
        if not thread and message.references:
            thread = message.references[0]
        if not thread and info["total"] > 1:
            thread = f"{message.email}:{message.date[:10]}:{info['version']}:{info['total']}"
        key = thread or series_key(message, info)
        groups[(key, info["version"])].append((message, info))

    result: list[Series] = []
    for (thread_key, version), items in groups.items():
        items.sort(key=lambda item: (item[1]["part"], item[0].date))
        cover = next((item for item in items if item[1]["part"] == 0), items[0])
        actual_parts = {item[1]["part"] for item in items if item[1]["part"] != 0}
        total = max([item[1]["total"] for item in items] + [len(actual_parts), 1])
        title = cover[1]["title"]
        stable = f"{cover[0].email}:{normalize_title(title)}"
        patches = []
        seen_parts: set[int] = set()
        for item in items:
            part = item[1]["part"]
            if part in seen_parts:
                continue
            seen_parts.add(part)
            patches.append({"subject": item[0].subject, "url": item[0].url, "part": part})
        result.append(
            Series(
                key=hashlib.sha1(stable.encode()).hexdigest()[:12],
                author=cover[0].author,
                email=cover[0].email,
                date=cover[0].date[:10],
                version=version,
                total=total,
                title=title,
                patches=patches,
                source=cover[0].url,
            )
        )

    LOG.debug("Grouped patch messages into %d revisions", len(result))

    # Associate rerolls even if the cover title changed. Mailing-list rerolls
    # commonly retain an author and a technical topic token (DAMON, LSM,
    # sphinx, etc.) while changing both patch count and wording.
    families: list[list[Series]] = []
    for revision in sorted(result, key=lambda item: (item.date, item.version), reverse=True):
        tokens = topic_tokens(revision.title)
        normalized = normalize_title(revision.title)
        revision_date = datetime.fromisoformat(revision.date).date()
        family = next(
            (
                candidate
                for candidate in families
                if candidate[0].email.lower() == revision.email.lower()
                and any(
                    abs((revision_date - datetime.fromisoformat(member.date).date()).days)
                    <= MAX_REROLL_GAP_DAYS
                    and (
                        normalized == normalize_title(member.title)
                        or bool(tokens.intersection(topic_tokens(member.title)))
                    )
                    for member in candidate
                )
            ),
            None,
        )
        if family is None:
            families.append([revision])
        else:
            family.append(revision)

    current_series: list[Series] = []
    for family in families:
        # Join cover letters split from their patches before counting versions.
        revisions: dict[tuple[int, str], Series] = {}
        for fragment in family:
            identity = (fragment.version, fragment.date)
            if identity not in revisions:
                revisions[identity] = fragment
                continue
            combined = revisions[identity]
            known = {(patch["part"], patch["subject"]) for patch in combined.patches}
            combined.patches.extend(
                patch
                for patch in fragment.patches
                if (patch["part"], patch["subject"]) not in known
            )
            combined.patches.sort(key=lambda patch: patch["part"])
            combined.total = max(combined.total, fragment.total)
            if any(patch["part"] == 0 for patch in fragment.patches):
                combined.title = fragment.title
                combined.source = fragment.source

        ordered = sorted(revisions.values(), key=lambda item: (item.date, item.version), reverse=True)
        current = ordered[0]
        current.previous_versions = [
            {
                "version": previous.version,
                "date": previous.date,
                "title": previous.title,
                "source": previous.source,
                "archived_messages": len(previous.patches),
            }
            for previous in ordered[1:]
        ]
        current_series.append(current)
    previous_count = sum(len(item.previous_versions) for item in current_series)
    LOG.info(
        "Detected %d current series (%d previous versions grouped)",
        len(current_series),
        previous_count,
    )
    return current_series


def git_subjects(limit: int = 10000) -> tuple[set[str], str]:
    """Read recent docs-next subjects, paginating at Gitiles' 10,000 cap."""
    LOG.info("Reading up to %d recent docs-next commits from Gitiles", limit)
    subjects: set[str] = set()
    revision = ""
    cursor = ""
    loaded = 0
    page = 0
    while loaded < limit:
        page += 1
        params = {"format": "JSON", "n": str(min(10000, limit - loaded))}
        if cursor:
            params["s"] = cursor
        payload = fetch(f"{GITILES_LOG}?{urllib.parse.urlencode(params)}")
        # Gitiles prefixes JSON with an anti-XSSI guard: )]}' and a newline.
        document = json.loads(payload.split(b"\n", 1)[1])
        commits = document.get("log", [])
        if not revision and commits:
            revision = commits[0]["commit"]
        subjects.update(
            normalize_title(commit.get("message", "").splitlines()[0])
            for commit in commits
            if commit.get("message")
        )
        loaded += len(commits)
        LOG.debug("Gitiles page %d loaded %d commits", page, len(commits))
        cursor = document.get("next", "")
        if not commits or not cursor:
            break
    LOG.info(
        "Loaded %d commits (%d normalized subjects); docs-next is %.12s",
        loaded,
        len(subjects),
        revision or "unknown",
    )
    return subjects, revision


def merged(series: Series, subjects: set[str]) -> bool:
    patch_titles = []
    for patch in series.patches:
        info = patch_info(patch["subject"])
        if info and info["part"] != 0:
            patch_titles.append(normalize_title(info["title"]))
    return bool(patch_titles) and all(title in subjects for title in patch_titles)


def is_cold(series_date: str, today: datetime | None = None) -> bool:
    """Return whether a pending series has had no update for over 30 days."""
    current_date = (today or datetime.now(timezone.utc)).date()
    submitted = datetime.fromisoformat(series_date).date()
    return current_date - submitted > timedelta(days=COLD_AFTER_DAYS)


def applied_series_ids(document: dict[str, Any] | None) -> set[tuple[str, str]]:
    """Return exact revisions that must not regress with a shorter history scan."""
    if not document:
        return set()
    return {
        (item["key"], item["source"])
        for item in document.get("series", [])
        if item.get("key")
        and item.get("source")
        and item.get("status") in {"applied", "graduated"}
    }


def build(
    args: argparse.Namespace,
    known_applied: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    messages = atom_messages(args.query, args.limit)
    patch_messages = [message for message in messages if patch_info(message.subject)]
    LOG.info("Found %d matching patch messages", len(patch_messages))
    series = collect_series(patch_messages)
    subjects, revision = git_subjects(args.commits)
    known_applied = known_applied or set()
    applied = 0
    cold = 0
    retained = 0
    now = datetime.now(timezone.utc)
    for item in series:
        found_in_history = merged(item, subjects)
        was_applied = (item.key, item.source) in known_applied
        if found_in_history or was_applied:
            item.status = "applied"
            item.note = "Applied."
            applied += 1
            retained += int(was_applied and not found_in_history)
            LOG.debug("Applied: %s", item.title)
        elif is_cold(item.date, now):
            item.status = "cold"
            item.note = "No update for over one month."
            cold += 1
            LOG.debug("Cold: %s", item.title)
    LOG.info(
        "Classified %d of %d series as applied and %d as cold "
        "(%d applied retained from prior runs)",
        applied,
        len(series),
        cold,
        retained,
    )
    order = {"applied": 0, "cooking": 1, "cold": 2}
    series.sort(key=lambda item: item.date, reverse=True)
    series.sort(key=lambda item: order.get(item.status, 9))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": LORE,
        "tree": TREE,
        "docs_next_revision": revision,
        "series": [asdict(item) for item in series],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default='s:"PATCH" AND s:"docs/zh_CN"')
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--output", default=str(ROOT / "data/status.json"))
    parser.add_argument(
        "--commits",
        type=int,
        default=10000,
        help="number of recent docs-next commits to inspect (default: 10000)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.commits < 1:
        parser.error("--commits must be at least 1")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime
    LOG.info("Starting dashboard update")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if output.exists():
        try:
            previous = json.loads(output.read_text())
        except json.JSONDecodeError as error:
            LOG.warning("Could not read existing output %s: %s", output, error)
    known_applied = applied_series_ids(previous)
    if known_applied:
        LOG.info("Loaded %d previously applied series", len(known_applied))
    result = build(args, known_applied)
    if previous is not None:
        old_content = {key: value for key, value in previous.items() if key != "generated_at"}
        new_content = {key: value for key, value in result.items() if key != "generated_at"}
        if old_content == new_content:
            result["generated_at"] = previous["generated_at"]
            LOG.info("Patch status is unchanged; preserving generated_at")
        else:
            LOG.info("Patch status changed")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    counts: dict[str, int] = defaultdict(int)
    for item in result["series"]:
        counts[item["status"]] += 1
    LOG.info(
        "Wrote %d series to %s (applied=%d, cooking=%d, cold=%d)",
        len(result["series"]),
        output,
        counts["applied"],
        counts["cooking"],
        counts["cold"],
    )


if __name__ == "__main__":
    main()
