#!/usr/bin/env python3
import argparse
import base64
import hashlib
import http.server
import io
import json
import os
import re
import secrets
import shutil
import sys
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ElementTree
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

CONFIG_DIR = Path.home() / ".canvas_mirror"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "mirror.log"
GOOGLE_CLIENT_FILE = CONFIG_DIR / "google_client_secret.json"
GOOGLE_TOKEN_FILE = CONFIG_DIR / "google_token.json"
CLAUDE_TEMPLATE = Path(__file__).resolve().parent / "CLAUDE.template.md"
PROBLEM_FILE = "_MIRRORING_PROBLEM.md"

USER_AGENT = "canvas_mirror.py"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"

GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
}
PUBLIC_EXPORT_URLS = {
    "document": "https://docs.google.com/document/d/{id}/export?format=pdf",
    "presentation": "https://docs.google.com/presentation/d/{id}/export/pdf",
    "drawings": "https://docs.google.com/drawings/d/{id}/export/pdf",
    "spreadsheets": "https://docs.google.com/spreadsheets/d/{id}/export?format=xlsx",
    "file": "https://drive.google.com/uc?export=download&id={id}",
}

GOOGLE_LINK = re.compile(
    r"https://(?:docs|drive)\.google\.com/"
    r"(?P<kind>document|presentation|spreadsheets|drawings|file)(?:/u/\d+)?/d/"
    r"(?P<id>[-\w]{20,})")
GOOGLE_FOLDER_LINK = re.compile(r"https://drive\.google\.com/drive/(?:u/\d+/)?folders/(?P<id>[-\w]{20,})")
FOLDER_MIME = "application/vnd.google-apps.folder"
CANVAS_FILE_LINK = re.compile(r"/files/(\d+)")
SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PDF_PRODUCER = re.compile(rb"/Producer\s*\((?:[^()\\]|\\.)*\)")

REPORT_HEADINGS = {
    "new": "New",
    "updated": "Updated (previous version moved to _old/)",
    "moved": "Moved or renamed (old location -> new)",
    "login needed": "Not downloaded, these need a Google login (see the README)",
    "skipped": "Skipped",
    "error": "Errors",
    "gone": "No longer linked from Canvas (kept, not deleted)",
}
TEXT_TYPES = ("Page", "Assignment", "Discussion", "Announcement", "Syllabus")

PROBLEM_TEXT = """# Canvas mirror problem

The run of canvas_mirror.py on {time} had a problem, so `Canvas_docs/` may be out of date:

{problems}

Run `python canvas_mirror.py` by hand to see the full output. This file is deleted after the next run without problems.
"""


class Fatal(Exception):
    pass


class LoginNeeded(Exception):
    pass


class Skipped(Exception):
    pass


@dataclass(frozen=True)
class Course:
    name: str
    id: int
    folder: Path
    canvas_url: str

    @property
    def url(self):
        return f"{self.canvas_url}/courses/{self.id}"

    @property
    def out_dir(self):
        return self.folder / "Canvas_docs"

    @property
    def old_dir(self):
        return self.out_dir / "_old"

    @property
    def state_file(self):
        return self.out_dir / ".mirror_state.json"


@dataclass(frozen=True)
class Link:
    source: str
    url: str
    kind: str
    id: str


@dataclass
class Item:
    module: str
    type: str
    title: str
    url: str = ""
    html: str = ""
    due_at: str | None = None
    posted_at: str | None = None
    in_module: bool = True
    source: str = ""
    links: list = field(default_factory=list)
    error: str = ""
    local: Path | None = None


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def http_request(url, token=None, data=None):
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.headers, response.read()


class Canvas:
    def __init__(self, url, token):
        self.url = url
        self.token = token

    def get(self, path):
        return json.loads(http_request(f"{self.url}/api/v1{path}", self.token)[1])

    def get_all(self, path):
        url, results = f"{self.url}/api/v1{path}", []
        while url:
            headers, body = http_request(url, self.token)
            results += json.loads(body)
            match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", ""))
            url = match.group(1) if match else None
        return results

    def get_optional(self, path):
        try:
            return self.get_all(path)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 404):
                return []
            raise

    def download(self, file_id, known_version=None):
        meta = self.get(f"/files/{file_id}")
        if meta.get("locked_for_user"):
            raise Skipped("locked on Canvas")
        version = f"{meta.get('updated_at')} {meta.get('modified_at')} {meta.get('size')}"
        if version == known_version:
            return meta["display_name"], None, version
        return meta["display_name"], http_request(meta["url"])[1], version


class GoogleDrive:
    def __init__(self, client, interactive=True):
        self.client = client
        self.interactive = interactive
        self.access_token = None
        self.expires_at = 0.0
        self.refresh_token = read_json(GOOGLE_TOKEN_FILE).get("refresh_token")

    def token(self):
        if self.access_token and time.time() < self.expires_at:
            return self.access_token
        response = None
        if self.refresh_token:
            try:
                response = self.token_request(grant_type="refresh_token", refresh_token=self.refresh_token)
            except urllib.error.HTTPError as error:
                if error.code not in (400, 401):
                    raise
                print("The Google login expired, log in again.")
        if response is None:
            if not self.interactive:
                raise RuntimeError("a new Google login is needed: run canvas_mirror.py once by hand")
            response = self.login()
        self.access_token = response["access_token"]
        self.expires_at = time.time() + response["expires_in"] - 60
        if "refresh_token" in response:
            self.refresh_token = response["refresh_token"]
            CONFIG_DIR.mkdir(exist_ok=True)
            GOOGLE_TOKEN_FILE.write_text(json.dumps({"refresh_token": self.refresh_token}), encoding="utf-8")
        return self.access_token

    def token_request(self, **fields):
        fields.update(client_id=self.client["client_id"], client_secret=self.client["client_secret"])
        token_uri = self.client.get("token_uri", "https://oauth2.googleapis.com/token")
        return json.loads(http_request(token_uri, data=urllib.parse.urlencode(fields).encode())[1])

    def login(self):
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        received = {}

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                received.update((key, values[0]) for key, values in query.items())
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Done, you can close this tab and go back to the terminal.")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), RedirectHandler)
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
            "client_id": self.client["client_id"], "redirect_uri": redirect_uri,
            "response_type": "code", "scope": GOOGLE_SCOPE,
            "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        print("Log in with your school Google account in the browser window that opens.")
        print(f"If no window opens, visit:\n{url}")
        webbrowser.open(url)
        deadline = time.monotonic() + 300
        while "code" not in received and "error" not in received and time.monotonic() < deadline:
            server.handle_request()
        server.server_close()
        if "error" in received:
            raise RuntimeError(f"Google refused the login: {received['error']}")
        if "code" not in received:
            raise RuntimeError("no login within 5 minutes")
        if received.get("state") != state:
            raise RuntimeError("the login answer doesn't belong to this request (state mismatch)")
        return self.token_request(grant_type="authorization_code", code=received["code"],
                                  redirect_uri=redirect_uri, code_verifier=verifier)

    def download(self, file_id, known_version=None):
        meta_url = f"{DRIVE_API}/{file_id}?fields=name,mimeType,modifiedTime,exportLinks&supportsAllDrives=true"
        meta = json.loads(http_request(meta_url, self.token())[1])
        name, mime, version = meta["name"], meta["mimeType"], meta.get("modifiedTime")
        if mime in GOOGLE_EXPORTS:
            export_mime, extension = GOOGLE_EXPORTS[mime]
            url = (meta.get("exportLinks", {}).get(export_mime)
                   or f"{DRIVE_API}/{file_id}/export?mimeType={urllib.parse.quote(export_mime)}")
            name += extension
        elif mime.startswith("application/vnd.google-apps."):
            raise Skipped(f"a Google {mime.rsplit('.', 1)[-1]} isn't a downloadable file")
        else:
            url = f"{DRIVE_API}/{file_id}?alt=media&supportsAllDrives=true"
        if version and version == known_version:
            return name, None, version
        return name, http_request(url, self.token())[1], version

    def list_folder(self, folder_id):
        meta_url = f"{DRIVE_API}/{folder_id}?fields=name&supportsAllDrives=true"
        name = json.loads(http_request(meta_url, self.token())[1])["name"]
        query = urllib.parse.urlencode({
            "q": f"'{folder_id}' in parents and trashed = false", "fields": "nextPageToken,files(id,name,mimeType)",
            "orderBy": "name", "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
        children, page = [], ""
        while True:
            answer = json.loads(http_request(f"{DRIVE_API}?{query}{page}", self.token())[1])
            children += answer.get("files", [])
            if "nextPageToken" not in answer:
                return name, children
            page = "&pageToken=" + urllib.parse.quote(answer["nextPageToken"])


def download_public_google(kind, file_id):
    try:
        headers, body = http_request(PUBLIC_EXPORT_URLS[kind].format(id=file_id))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise LoginNeeded() from None
        raise
    if headers.get_content_type() == "text/html":
        raise LoginNeeded()
    return filename_from_headers(headers) or f"{file_id}.pdf", body, None


def filename_from_headers(headers):
    match = re.search(r"filename\*=UTF-8''([^;]+)", headers.get("Content-Disposition", ""), re.IGNORECASE)
    return urllib.parse.unquote(match.group(1).strip()) if match else headers.get_filename()


def collect_items(canvas, course):
    base = f"/courses/{course.id}"
    assignments = {a["id"]: a for a in canvas.get_all(f"{base}/assignments?per_page=100")}
    discussions = {d["id"]: d for d in canvas.get_optional(f"{base}/discussion_topics?per_page=100")}
    items, in_modules, discussions_in_modules, pages_in_modules = [], set(), set(), set()
    syllabus = syllabus_item(canvas, course)
    if syllabus:
        items.append(syllabus)
    for module in canvas.get_all(f"{base}/modules?include[]=items&per_page=100"):
        entries = module.get("items")
        if entries is None:
            entries = canvas.get_all(f"{base}/modules/{module['id']}/items?per_page=100")
        for entry in entries:
            items.append(load_item(canvas, course, module["name"], entry, assignments, discussions))
            if entry["type"] == "Assignment":
                in_modules.add(entry["content_id"])
            elif entry["type"] == "Discussion":
                discussions_in_modules.add(entry["content_id"])
                in_modules.add(discussions.get(entry["content_id"], {}).get("assignment_id"))
            elif entry["type"] == "Page":
                pages_in_modules.add(entry.get("page_url"))
    items += [assignment_item(course, "Assignments not in a module", a, f"assignment:{a['id']}")
              for a in assignments.values() if a["id"] not in in_modules]
    items += [discussion_item(course, "Discussions not in a module", d, f"discussion:{d['id']}", assignments)
              for d in discussions.values()
              if d["id"] not in discussions_in_modules and d.get("assignment_id") not in assignments]
    items += [page_item(canvas, course, "Pages not in a module", page["url"], f"page:{page['page_id']}", page["title"])
              for page in canvas.get_optional(f"{base}/pages?per_page=100") if page["url"] not in pages_in_modules]
    items += announcement_items(canvas, course)
    return items


def load_item(canvas, course, module, entry, assignments, discussions):
    kind, title = entry["type"], entry["title"]
    source = f"module-item:{entry['id']}"
    try:
        if kind == "Page":
            return page_item(canvas, course, module, entry["page_url"], source, title)
        if kind == "Assignment":
            assignment = (assignments.get(entry["content_id"])
                          or canvas.get(f"/courses/{course.id}/assignments/{entry['content_id']}"))
            return assignment_item(course, module, assignment, source)
        if kind == "Discussion":
            topic = (discussions.get(entry["content_id"])
                     or canvas.get(f"/courses/{course.id}/discussion_topics/{entry['content_id']}"))
            return discussion_item(course, module, topic, source, assignments)
    except urllib.error.HTTPError as error:
        return Item(module, kind, title, entry.get("html_url", ""), source=source,
                    error=f"Canvas answered HTTP {error.code} (still locked?)")
    item = Item(module, kind, title, entry.get("html_url", ""), source=source)
    if kind == "File":
        item.links = [Link(f"canvas-file:{entry['content_id']}", item.url, "canvas", str(entry["content_id"]))]
    elif kind == "ExternalUrl":
        item.url = entry.get("external_url", "")
        item.links = find_links(item.url, course)
    return item


def assignment_item(course, module, assignment, source):
    html = assignment.get("description") or ""
    return Item(module, "Assignment", assignment["name"], assignment["html_url"], html,
                due_at=assignment.get("due_at"), source=source, links=find_links(html, course))


def page_item(canvas, course, module, page_url, source, title):
    try:
        page = canvas.get(f"/courses/{course.id}/pages/{page_url}")
    except urllib.error.HTTPError as error:
        return Item(module, "Page", title, f"{course.url}/pages/{page_url}", source=source,
                    error=f"Canvas answered HTTP {error.code} (still locked?)")
    html = page.get("body") or ""
    return Item(module, "Page", page["title"], page["html_url"], html, source=source, links=find_links(html, course))


def discussion_item(course, module, topic, source, assignments):
    html = topic.get("message") or ""
    due_at = assignments.get(topic.get("assignment_id"), {}).get("due_at")
    return Item(module, "Discussion", topic["title"], topic["html_url"], html, due_at=due_at,
                posted_at=topic.get("posted_at"), source=source, links=find_links(html, course))


def announcement_items(canvas, course):
    topics = canvas.get_optional(f"/courses/{course.id}/discussion_topics?only_announcements=true&per_page=100")
    items = []
    for topic in sorted(topics, key=lambda t: t.get("posted_at") or "", reverse=True):
        html = topic.get("message") or ""
        title = topic["title"]
        if topic.get("posted_at"):
            title = f"{local_time(topic['posted_at']):%Y-%m-%d} {title}"
        items.append(Item("Announcements", "Announcement", title, topic["html_url"], html,
                          posted_at=topic.get("posted_at"), in_module=False,
                          source=f"announcement:{topic['id']}", links=find_links(html, course)))
    return items


def syllabus_item(canvas, course):
    try:
        html = canvas.get(f"/courses/{course.id}?include[]=syllabus_body").get("syllabus_body") or ""
    except urllib.error.HTTPError:
        return None
    if not html.strip():
        return None
    return Item("", "Syllabus", "Syllabus", f"{course.url}/assignments/syllabus", html,
                in_module=False, source="syllabus", links=find_links(html, course))


def find_links(text, course):
    links = {}
    for match in GOOGLE_LINK.finditer(text):
        source = f"google:{match['id']}"
        links.setdefault(source, Link(source, match.group(0), match["kind"], match["id"]))
    for match in GOOGLE_FOLDER_LINK.finditer(text):
        source = f"google:{match['id']}"
        links.setdefault(source, Link(source, match.group(0), "folder", match["id"]))
    for match in CANVAS_FILE_LINK.finditer(text):
        source = f"canvas-file:{match[1]}"
        links.setdefault(source, Link(source, f"{course.url}/files/{match[1]}", "canvas", match[1]))
    return list(links.values())


def urls_in_document(content):
    if content.startswith(b"%PDF"):
        chunks = [content]
        for match in re.finditer(rb"stream\r?\n(.*?)endstream", content, re.DOTALL):
            try:
                chunks.append(zlib.decompressobj().decompress(match.group(1), 50_000_000))
            except zlib.error:
                pass
        return [url.decode("latin-1") for chunk in chunks for url in re.findall(rb"/URI\s*\(([^)]*)\)", chunk)]
    if content.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                text = " ".join(archive.read(name).decode("utf-8", "replace")
                                for name in archive.namelist() if name.endswith((".xml", ".rels")))
        except zipfile.BadZipFile:
            return []
    else:
        text = content.decode("utf-8", "replace")
    return re.findall(r"https?://[^\s\"'<>)\]]+", text)


def links_in_document(content, course):
    followed = ("https://docs.google.com/", "https://drive.google.com/", f"{course.url}/")
    return find_links(" ".join(url for url in urls_in_document(content) if url.startswith(followed)), course)


class MarkdownConverter(HTMLParser):
    HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
    BLOCKS = ("p", "div", "blockquote")

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts = []
        self.cell = None
        self.row = None
        self.table_rows = 0
        self.lists = []
        self.hrefs = []
        self.hidden = 0
        self.in_pre = False
        self.just_opened = False

    def absolute(self, href):
        return urllib.parse.urljoin(self.base_url + "/", href)

    def target(self):
        return self.parts if self.cell is None else self.cell

    def write(self, text):
        self.target().append(text)
        self.just_opened = False

    def last(self):
        target = self.target()
        return target[-1] if target else "\n"

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in self.HEADINGS:
            self.write("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in self.BLOCKS:
            self.write(" " if self.lists else "\n\n")
        elif tag == "table":
            self.write("\n\n")
        elif tag == "br":
            self.write("\n" if self.cell is None else " ")
        elif tag in ("strong", "b"):
            self.write("**")
            self.just_opened = True
        elif tag in ("em", "i"):
            self.write("*")
            self.just_opened = True
        elif tag == "a":
            self.hrefs.append(attrs.get("href"))
            if attrs.get("href"):
                self.write("[")
        elif tag in ("ul", "ol"):
            self.write("\n" if self.lists else "\n\n")
            self.lists.append([tag, 0])
        elif tag == "li" and self.lists:
            self.lists[-1][1] += 1
            kind, number = self.lists[-1]
            bullet = f"{number}." if kind == "ol" else "-"
            self.write("\n" + "  " * (len(self.lists) - 1) + bullet + " ")
        elif tag == "iframe" and attrs.get("src"):
            self.write(f"\n\n[Embedded document]({attrs['src']})\n\n")
        elif tag == "img" and attrs.get("src"):
            self.write(f"![{attrs.get('alt') or ''}]({self.absolute(attrs['src'])})")
        elif tag == "pre":
            self.write("\n\n```\n")
            self.in_pre = True
        elif tag == "hr":
            self.write("\n\n---\n\n")
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in self.HEADINGS:
            self.write("\n\n")
        elif tag in self.BLOCKS:
            self.write(" " if self.lists else "\n\n")
        elif tag in ("strong", "b"):
            self.close_inline("**")
        elif tag in ("em", "i"):
            self.close_inline("*")
        elif tag == "a" and self.hrefs:
            href = self.hrefs.pop()
            if href:
                self.write(f"]({self.absolute(href)})")
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self.write("\n" if self.lists else "\n\n")
        elif tag == "pre":
            self.write("\n```\n\n")
            self.in_pre = False
        elif tag in ("td", "th") and self.cell is not None:
            text = " ".join("".join(self.cell).split()).replace("|", "\\|")
            self.cell = None
            self.row = (self.row or []) + [text]
        elif tag == "tr" and self.row is not None:
            self.write("| " + " | ".join(self.row) + " |\n")
            if self.table_rows == 0:
                self.write("|" + " --- |" * len(self.row) + "\n")
            self.table_rows += 1
            self.row = None
        elif tag == "table":
            self.write("\n")
            self.table_rows = 0

    def close_inline(self, marker):
        target = self.target()
        if target and target[-1].endswith(" "):
            target[-1] = target[-1].rstrip(" ")
            self.write(marker + " ")
        else:
            self.write(marker)

    def handle_data(self, data):
        if self.hidden:
            return
        if not self.in_pre:
            data = re.sub(r"\s+", " ", data)
            if self.just_opened and data.startswith(" "):
                target = self.target()
                target[-1] = " " + target[-1]
            if self.last().endswith((" ", "\n")) or self.just_opened:
                data = data.lstrip()
        if data:
            self.write(data)


def html_to_markdown(html, base_url):
    converter = MarkdownConverter(base_url)
    converter.feed(html)
    converter.close()
    text = "".join(converter.parts).replace("****", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def md_escape(text):
    return text.replace("[", "\\[").replace("]", "\\]").replace("|", "\\|")


def md_link(text, target):
    return f"[{md_escape(text)}](<{target}>)"


def relative(path, start):
    return Path(os.path.relpath(path, start)).as_posix()


def local_time(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()


def format_date(iso):
    return local_time(iso).strftime("%a %d %b %Y, %H:%M")


def page_markdown(item, files, course):
    def file_line(link, path):
        if path:
            return md_link(path.name, relative(path, item.local.parent))
        return f"{link.url} (not downloaded, see the output of canvas_mirror.py)"

    lines = [f"# {item.title}", ""]
    lines.append(f"- **Type:** {item.type} in module \"{item.module}\"" if item.in_module else f"- **Type:** {item.type}")
    if item.posted_at:
        lines.append(f"- **Posted:** {format_date(item.posted_at)}")
    if item.due_at:
        lines.append(f"- **Due:** {format_date(item.due_at)}")
    lines.append(f"- **On Canvas:** {item.url}")
    if files:
        lines += ["", "Linked documents:", ""]
        for link, path, nested in files:
            lines.append(f"- {file_line(link, path)}")
            lines += [f"  - linked inside it: {file_line(n_link, n_path)}" for n_link, n_path in nested]
    lines += ["", "---", "", html_to_markdown(item.html, course.canvas_url)]
    return "\n".join(lines).rstrip() + "\n"


def overview_markdown(items, course):
    def link(item):
        if item.local:
            return md_link(item.title, relative(item.local, course.out_dir))
        return md_link(item.title, item.url) if item.url else md_escape(item.title)

    lines = [f"# {course.name}: Canvas overview", "",
             f"Made by `canvas_mirror.py` from {course.url}. Times are in this computer's time zone."]
    loose = [f"- {link(item)}" for item in items if not item.module]
    if loose:
        lines += ["", *loose]
    lines += ["", "## Deadlines", "", "| Due | Item | Module |", "| --- | --- | --- |"]
    for item in sorted((i for i in items if i.due_at), key=lambda i: i.due_at):
        lines.append(f"| {format_date(item.due_at)} | {link(item)} | {md_escape(item.module)} |")
    module = None
    for item in items:
        if not item.module:
            continue
        if item.module != module:
            module = item.module
            lines += ["", f"## {module}", ""]
        if item.type == "SubHeader":
            lines.append(f"- **{md_escape(item.title)}**")
        else:
            lines.append(f"- {link(item)} ({item.type})")
    return "\n".join(lines) + "\n"


def sheet_values(archive):
    names = archive.namelist()
    strings = []
    if "xl/sharedStrings.xml" in names:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        strings = ["".join(t.text or "" for t in item.iter(f"{SHEET_NS}t")) for item in root.iter(f"{SHEET_NS}si")]
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    values = [sheet.get("name") for sheet in workbook.iter(f"{SHEET_NS}sheet")]
    for name in sorted(n for n in names if n.startswith("xl/worksheets/sheet")):
        for cell in ElementTree.fromstring(archive.read(name)).iter(f"{SHEET_NS}c"):
            if cell.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{SHEET_NS}t"))
            else:
                value = cell.findtext(f"{SHEET_NS}v")
                if value is not None and cell.get("t") == "s":
                    value = strings[int(value)]
            if value:
                values.append((name, cell.get("r"), value))
    return values


def same_content(old, new):
    if old == new:
        return True
    if old.startswith(b"%PDF") and new.startswith(b"%PDF"):
        return PDF_PRODUCER.sub(b"", old) == PDF_PRODUCER.sub(b"", new)
    if not (old.startswith(b"PK") and new.startswith(b"PK")):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(old)) as a, zipfile.ZipFile(io.BytesIO(new)) as b:
            if "xl/workbook.xml" in a.namelist() and "xl/workbook.xml" in b.namelist():
                return sheet_values(a) == sheet_values(b)
            return a.namelist() == b.namelist() and all(a.read(name) == b.read(name) for name in a.namelist())
    except (zipfile.BadZipFile, ElementTree.ParseError, IndexError, ValueError):
        return False


def safe_filename(name):
    name = unicodedata.normalize("NFC", name)
    name = "".join(c for c in name
                   if unicodedata.category(c) not in ("So", "Sk", "Cf", "Cc") and not "\ufe00" <= c <= "\ufe0f")
    name = re.sub(r'[<>"/\\|?*]', "-", name.replace(":", " -"))
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r" (\.\w+)$", r"\1", name).strip(" .")
    return name or "unnamed"


class Mirror:
    def __init__(self, canvas, drive, course, dry_run):
        self.canvas = canvas
        self.drive = drive
        self.course = course
        self.out = course.out_dir
        self.dry_run = dry_run
        state = read_json(course.state_file)
        self.old_state = state.get("paths", {})
        self.old_versions = state.get("versions", {})
        self.state = {}
        self.versions = {}
        self.paths = {}
        self.downloads = {}
        self.inner_links = {}
        self.nested = {}
        self.vacated = set()
        self.results = {status: [] for status in ["unchanged", *REPORT_HEADINGS]}

    def run(self, items):
        print("Downloading the documents linked from Canvas ...")
        for item in items:
            for link in item.links:
                self.fetch(link, item.module, item.title)
        print("Downloading the documents linked inside those ...")
        for item in items:
            for link in item.links:
                self.fetch_nested(link, item.module)
        for item in items:
            self.save_item(item)
        self.save("overview", self.out / "_overview.md", overview_markdown(items, self.course).encode())
        in_use = set(self.state.values())
        for source, rel in self.old_state.items():
            if source not in self.state and rel not in in_use and (self.out / rel).exists():
                self.note("gone", rel)
        if not self.dry_run:
            self.remove_empty_folders()
            versions = {source: version for source, version in self.versions.items() if source in self.state}
            state = {"paths": self.state, "versions": versions}
            self.course.state_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def folder(self, module):
        return self.out / safe_filename(module) if module else self.out

    def fetch(self, link, module, where, subfolders=()):
        if link.source in self.downloads:
            return
        self.keep_previous(link.source)
        if link.kind == "folder":
            self.fetch_folder(link, module, where, subfolders)
            return
        path = content = None
        try:
            name, content, version = self.download(link)
            if content is None:
                content = (self.out / self.old_state[link.source]).read_bytes()
            path = self.unique_path(self.folder(module).joinpath(*subfolders, safe_filename(name)), link.source)
            self.save(link.source, path, content)
            if version:
                self.versions[link.source] = version
            else:
                self.versions.pop(link.source, None)
        except LoginNeeded:
            self.note("login needed", f"{link.url}  (linked from: {where})")
        except Skipped as reason:
            self.note("skipped", f"{link.url}  ({reason})")
        except urllib.error.HTTPError as error:
            hint = ": your account can't open this file" if error.code in (401, 403, 404) else ""
            self.note("error", f"{link.url}  (HTTP {error.code}{hint}, linked from: {where})")
        except OSError as error:
            self.note("error", f"{link.url}  ({error})")
        if path is None and link.source in self.state:
            previous = self.out / self.state[link.source]
            if previous.exists():
                path, content = previous, previous.read_bytes()
        self.downloads[link.source] = path
        self.inner_links[link.source] = links_in_document(content or b"", self.course)

    def download(self, link):
        previous = self.old_state.get(link.source)
        known = self.old_versions.get(link.source) if previous and (self.out / previous).is_file() else None
        if link.kind == "canvas":
            return self.canvas.download(link.id, known)
        if self.drive:
            return self.drive.download(link.id, known)
        return download_public_google(link.kind, link.id)

    def fetch_folder(self, link, module, where, subfolders):
        previous = self.old_state.get(link.source)
        self.downloads[link.source] = self.out / previous if previous and (self.out / previous).is_dir() else None
        self.inner_links[link.source] = []
        self.nested[link.source] = []
        try:
            if not self.drive:
                raise LoginNeeded()
            name, children = self.drive.list_folder(link.id)
        except LoginNeeded:
            self.note("login needed", f"{link.url}  (a Drive folder, linked from: {where})")
            return self.keep_folder(previous)
        except urllib.error.HTTPError as error:
            hint = ": your account can't open this folder" if error.code in (401, 403, 404) else ""
            self.note("error", f"{link.url}  (HTTP {error.code}{hint}, linked from: {where})")
            return self.keep_folder(previous)
        except OSError as error:
            self.note("error", f"{link.url}  ({error})")
            return self.keep_folder(previous)
        folder = self.unique_path(self.folder(module).joinpath(*subfolders, safe_filename(name)), link.source)
        self.state[link.source] = folder.relative_to(self.out).as_posix()
        self.downloads[link.source] = folder
        for child in children:
            if child["mimeType"] == FOLDER_MIME:
                url, kind = f"https://drive.google.com/drive/folders/{child['id']}", "folder"
            else:
                url, kind = f"https://drive.google.com/file/d/{child['id']}", "file"
            child_link = Link(f"google:{child['id']}", url, kind, child["id"])
            self.nested[link.source].append(child_link)
            self.fetch(child_link, module, f"Drive folder {name}", (*subfolders, folder.name))

    def keep_folder(self, previous):
        for source, rel in self.old_state.items():
            if previous and rel.startswith(previous + "/"):
                self.keep_previous(source)

    def fetch_nested(self, link, module):
        if link.source in self.nested:
            return
        found = [nested for nested in self.inner_links[link.source] if nested.source != link.source]
        self.nested[link.source] = found
        parent = self.downloads[link.source]
        for nested in found:
            self.fetch(nested, module, f"inside {parent.name if parent else link.url}")

    def save_item(self, item):
        files = [(link, self.downloads[link.source],
                  [(nested, self.downloads[nested.source]) for nested in self.nested[link.source]])
                 for link in item.links]
        if item.type in TEXT_TYPES:
            self.save_page(item, files)
        elif files:
            item.local = files[0][1]

    def save_page(self, item, files):
        if item.error:
            self.keep_previous(item.source)
            self.note("error", f"{item.module} / {item.title}: {item.error}")
            return
        item.local = self.unique_path(self.folder(item.module) / f"{safe_filename(item.title)}.md", item.source)
        try:
            self.save(item.source, item.local, page_markdown(item, files, self.course).encode())
        except OSError as error:
            self.note("error", f"{item.local.name}: {error}")

    def save(self, source, path, content):
        rel = path.relative_to(self.out).as_posix()
        previous = self.old_state.get(source)
        self.state[source] = rel
        current = path
        if previous and previous != rel and (self.out / previous).exists():
            old_file = self.out / previous
            self.vacated.add(old_file.parent)
            if path.exists():
                if not self.dry_run:
                    self.move_to_old(old_file)
            elif self.dry_run:
                current = old_file
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(old_file, path)
            self.note("moved", f"{previous} -> {rel}")
        existed = current.exists()
        if existed and same_content(current.read_bytes(), content):
            self.note("unchanged", rel)
        else:
            if not self.dry_run:
                self.write(path, content, backup=path.exists())
            self.note("updated" if existed else "new", rel)

    def write(self, path, content, backup):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".part")
        temp.write_bytes(content)
        try:
            if backup:
                self.move_to_old(path)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    def move_to_old(self, path):
        now = datetime.now()
        folder = self.course.old_dir / path.parent.relative_to(self.out)
        target = folder / f"{path.stem} (replaced {now:%Y-%m-%d}){path.suffix}"
        if target.exists():
            target = target.with_stem(f"{path.stem} (replaced {now:%Y-%m-%d %H%M%S})")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(path, target)

    def unique_path(self, path, source):
        owner = self.paths.setdefault(str(path).lower(), source)
        if owner == source:
            return path
        return path.with_stem(f"{path.stem} ({source.rsplit(':', 1)[-1][:10]})")

    def remove_empty_folders(self):
        for folder in sorted(self.vacated, key=lambda p: len(p.parts), reverse=True):
            while folder != self.out and folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
                folder = folder.parent

    def keep_previous(self, source):
        if source in self.old_state:
            self.state.setdefault(source, self.old_state[source])
        if source in self.old_versions:
            self.versions.setdefault(source, self.old_versions[source])

    def note(self, status, text):
        self.results[status].append(text)

    def print_report(self):
        print()
        if self.dry_run:
            print("DRY RUN: nothing was written.")
        print(f"{len(self.results['unchanged'])} files unchanged.")
        for status, heading in REPORT_HEADINGS.items():
            if self.results[status]:
                print(f"\n{heading}:")
                for line in self.results[status]:
                    print(f"  {line}")


def add_claude_md(course, dry_run):
    path = course.folder / "CLAUDE.md"
    if path.exists() or not CLAUDE_TEMPLATE.exists():
        return
    if dry_run:
        print(f"\nWould add {path}")
        return
    path.write_text(CLAUDE_TEMPLATE.read_text(encoding="utf-8").replace("{course}", course.name), encoding="utf-8")
    print(f"\nAdded {path}")


def load_config():
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"No config at {CONFIG_FILE}. Copy config.example.json there and fill it in.")
    except json.JSONDecodeError as error:
        sys.exit(f"{CONFIG_FILE} isn't valid JSON: {error}")
    if not config.get("canvas_url"):
        sys.exit(f"Set canvas_url in {CONFIG_FILE}.")
    return config


def pick_courses(config, canvas_url, names):
    folder = Path(config.get("courses_folder") or "").expanduser()
    if not config.get("courses_folder") or not folder.is_dir():
        sys.exit(f"Set courses_folder in {CONFIG_FILE} to a folder that exists.")
    courses = [Course(name, int(course_id), folder / name, canvas_url)
               for name, course_id in config.get("courses", {}).items()]
    if not courses:
        sys.exit(f"No courses in {CONFIG_FILE}. Run with --list-courses to find their ids.")
    known = {course.name for course in courses}
    unknown = [name for name in names or [] if name not in known]
    if unknown:
        sys.exit(f"Not in {CONFIG_FILE}: {', '.join(unknown)}")
    return [course for course in courses if not names or course.name in names]


def check_canvas(canvas):
    if not canvas.token:
        raise Fatal("CANVAS_TOKEN is not set. Create a token in Canvas: Account > Settings > New access token.")
    try:
        canvas.get("/users/self")
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise Fatal("Canvas refused CANVAS_TOKEN (HTTP 401). Has it expired?") from None
        raise Fatal(f"{canvas.url} answered HTTP {error.code}. Is canvas_url right?") from None
    except urllib.error.URLError as error:
        raise Fatal(f"Can't reach {canvas.url} ({error.reason}).") from None
    except ValueError:
        raise Fatal(f"{canvas.url} doesn't look like Canvas. Is canvas_url right?") from None


def list_courses(canvas):
    for course in canvas.get_all("/courses?enrollment_state=active&per_page=100"):
        if "name" in course:
            print(f"{course['id']:>8}  {course['name']}")


def connect_google(interactive, problems):
    if not GOOGLE_CLIENT_FILE.exists():
        print(f"No {GOOGLE_CLIENT_FILE}, so only public Google files are downloaded (see the README).")
        return None
    client = read_json(GOOGLE_CLIENT_FILE).get("installed")
    if not client:
        problems.append(f"{GOOGLE_CLIENT_FILE} isn't a 'Desktop app' client, so only public Google files are downloaded.")
        print(problems[-1])
        return None
    drive = GoogleDrive(client, interactive)
    try:
        drive.token()
    except (RuntimeError, OSError) as error:
        problems.append(f"Google login failed ({error}), so only public Google files are downloaded.")
        print(problems[-1])
        return None
    return drive


def report_problems(courses, general, problems, dry_run):
    if dry_run:
        return
    for course in courses:
        path = course.folder / PROBLEM_FILE
        found = general + problems[course]
        try:
            if found:
                course.folder.mkdir(exist_ok=True)
                lines = "\n".join(f"- {problem}" for problem in found)
                path.write_text(PROBLEM_TEXT.format(time=f"{datetime.now():%Y-%m-%d %H:%M}", problems=lines),
                                encoding="utf-8")
                print(f"\nWrote {path}")
            else:
                path.unlink(missing_ok=True)
        except OSError as error:
            print(f"Couldn't update {path}: {error}")


def start_log():
    CONFIG_DIR.mkdir(exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
        LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    print(f"\n######## Scheduled run, {datetime.now():%Y-%m-%d %H:%M}")


def main():
    parser = argparse.ArgumentParser(description="Download Canvas courses and the files they link to.")
    parser.add_argument("--course", action="append", metavar="NAME",
                        help="only mirror this course (can be repeated); default: every course in the config")
    parser.add_argument("--list-courses", action="store_true", help="show your Canvas courses and their ids")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, but write nothing")
    parser.add_argument("--no-google", action="store_true", help="skip the Google login, download public files only")
    parser.add_argument("--scheduled", action="store_true",
                        help=f"for automatic runs: write output to {LOG_FILE}, never open a browser")
    args = parser.parse_args()
    if args.scheduled:
        start_log()
    else:
        sys.stdout.reconfigure(errors="replace")

    config = load_config()
    canvas = Canvas(config["canvas_url"].rstrip("/"), os.environ.get("CANVAS_TOKEN"))
    if args.list_courses:
        try:
            check_canvas(canvas)
        except Fatal as error:
            sys.exit(str(error))
        list_courses(canvas)
        return
    courses = pick_courses(config, canvas.url, args.course)
    general, problems = [], {course: [] for course in courses}
    try:
        check_canvas(canvas)
    except Fatal as error:
        general.append(str(error))
        report_problems(courses, general, problems, args.dry_run)
        sys.exit(str(error))
    drive = None if args.no_google else connect_google(not args.scheduled, general)
    for course in courses:
        print(f"\n======== {course.name}  ({course.url})")
        try:
            print("Reading the course from Canvas ...")
            items = collect_items(canvas, course)
            mirror = Mirror(canvas, drive, course, args.dry_run)
            mirror.run(items)
            mirror.print_report()
            if config.get("create_claude_md", True):
                add_claude_md(course, args.dry_run)
        except urllib.error.HTTPError as error:
            problems[course].append(f"Skipped this course: Canvas answered HTTP {error.code}.")
        except OSError as error:
            problems[course].append(f"Skipped this course: {error}")
        except Exception as error:
            traceback.print_exc()
            problems[course].append(f"Skipped this course: the script crashed ({error!r}).")
        if problems[course]:
            print(problems[course][-1])
    report_problems(courses, general, problems, args.dry_run)


if __name__ == "__main__":
    main()
