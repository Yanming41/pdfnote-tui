from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


APP_DIR = Path.home() / ".pdfnote"
DB_PATH = APP_DIR / "pdfnote.sqlite3"
EXPORT_DIR = APP_DIR / "exports"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return value.encode("utf-8", "replace").decode("utf-8")


def normalize_paragraphs(text: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = clean_text(" ".join(raw.strip().split()))
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    return [p for p in paragraphs if p]


@dataclass
class PdfPage:
    page_number: int
    paragraphs: list[str]


@dataclass
class PdfDoc:
    id: int
    path: Path
    title: str
    fingerprint: str
    pages: list[PdfPage]


class Store:
    def __init__(self, path: Path = DB_PATH):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            create table if not exists pdfs (
                id integer primary key autoincrement,
                title text not null,
                path text not null,
                fingerprint text not null unique,
                created_at text not null
            );

            create table if not exists records (
                id integer primary key autoincrement,
                pdf_id integer not null references pdfs(id),
                page integer not null,
                paragraph_index integer not null,
                selected_text text not null,
                surrounding_text text not null,
                kind text not null,
                question text,
                answer text,
                note text,
                tags text,
                created_at text not null
            );
            """
        )
        self.conn.commit()

    def upsert_pdf(self, path: Path, title: str, fingerprint: str) -> int:
        row = self.conn.execute(
            "select id from pdfs where fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row:
            self.conn.execute(
                "update pdfs set title = ?, path = ? where id = ?",
                (title, str(path), row["id"]),
            )
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "insert into pdfs(title, path, fingerprint, created_at) values (?, ?, ?, ?)",
            (title, str(path), fingerprint, utc_now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_record(
        self,
        pdf_id: int,
        page: int,
        paragraph_index: int,
        selected_text: str,
        surrounding_text: str,
        kind: str,
        question: str | None = None,
        answer: str | None = None,
        note: str | None = None,
        tags: list[str] | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            insert into records(
                pdf_id, page, paragraph_index, selected_text, surrounding_text,
                kind, question, answer, note, tags, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pdf_id,
                page,
                paragraph_index,
                clean_text(selected_text),
                clean_text(surrounding_text),
                kind,
                clean_text(question),
                clean_text(answer),
                clean_text(note),
                json.dumps(tags or [], ensure_ascii=False),
                utc_now(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_records(self, pdf_id: int, limit: int = 8) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                select * from records
                where pdf_id = ?
                order by id desc
                limit ?
                """,
                (pdf_id, limit),
            )
        )

    def all_records(self, pdf_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "select * from records where pdf_id = ? order by page, paragraph_index, id",
                (pdf_id,),
            )
        )


def load_pdf(path: Path, store: Store) -> PdfDoc:
    if not path.exists():
        raise FileNotFoundError(path)
    fingerprint = file_sha256(path)
    doc = fitz.open(path)
    title = (doc.metadata or {}).get("title") or path.stem
    pages: list[PdfPage] = []
    for idx in range(doc.page_count):
        page = doc.load_page(idx)
        text = page.get_text("text")
        pages.append(PdfPage(page_number=idx + 1, paragraphs=normalize_paragraphs(text)))
    pdf_id = store.upsert_pdf(path.resolve(), title, fingerprint)
    return PdfDoc(id=pdf_id, path=path.resolve(), title=title, fingerprint=fingerprint, pages=pages)


class PdfNoteApp:
    def __init__(self, doc: PdfDoc, store: Store):
        self.console = Console(legacy_windows=False)
        self.doc = doc
        self.store = store
        self.page_index = 0
        self.paragraph_index = 0
        self.selection_override: str | None = None
        self.message = "Type help for commands."

    @property
    def page(self) -> PdfPage:
        return self.doc.pages[self.page_index]

    @property
    def selected_text(self) -> str:
        if self.selection_override:
            return self.selection_override
        if not self.page.paragraphs:
            return ""
        return self.page.paragraphs[self.paragraph_index]

    def surrounding_text(self) -> str:
        paragraphs = self.page.paragraphs
        if not paragraphs:
            return ""
        start = max(0, self.paragraph_index - 2)
        end = min(len(paragraphs), self.paragraph_index + 3)
        return "\n\n".join(paragraphs[start:end])

    def render(self) -> None:
        self.console.clear()
        header = (
            f"{self.doc.title} | page {self.page.page_number}/{len(self.doc.pages)} "
            f"| paragraph {self.paragraph_index + 1}/{max(1, len(self.page.paragraphs))}"
        )
        self.console.print(Panel(header, title="pdfnote", style="bold cyan"))

        if not self.page.paragraphs:
            self.console.print(Panel("No extractable text on this page.", title="PDF Text"))
        else:
            body = Text()
            for i, paragraph in enumerate(self.page.paragraphs):
                prefix = f"{i + 1:02d} "
                wrapped = textwrap.wrap(paragraph, width=max(40, self.console.width - 8)) or [""]
                style = "black on bright_yellow" if i == self.paragraph_index else ""
                body.append(prefix + wrapped[0] + "\n", style=style)
                for continuation in wrapped[1:3]:
                    body.append("   " + continuation + "\n", style=style)
                if len(wrapped) > 3:
                    body.append("   ...\n", style=style)
            self.console.print(Panel(body, title="PDF Text"))

        self.render_recent_notes()
        if self.selection_override:
            self.console.print(Panel(self.selection_override, title="Current narrow selection", style="yellow"))
        self.console.print(Panel(self.message, title="Status", style="green"))

    def render_recent_notes(self) -> None:
        records = self.store.recent_records(self.doc.id, limit=5)
        table = Table(title="Recent records", expand=True)
        table.add_column("id", width=5)
        table.add_column("p", width=4)
        table.add_column("kind", width=8)
        table.add_column("question / note")
        if not records:
            table.add_row("-", "-", "-", "No notes yet.")
        for row in records:
            preview = row["question"] or row["note"] or row["answer"] or ""
            table.add_row(str(row["id"]), str(row["page"]), row["kind"], preview[:100])
        self.console.print(table)

    def run(self) -> None:
        while True:
            self.render()
            try:
                command = self.console.input("[bold cyan]pdfnote> [/]").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                break
            if not command:
                continue
            if not self.handle(command):
                break

    def handle(self, command: str) -> bool:
        name, _, rest = command.partition(" ")
        if name in {"quit", "exit"}:
            return False
        if name == "help":
            self.message = (
                "j/k move | [/ ] pages | g <page> | / text search | e explain | "
                "pick <text> | range <start> <end> | clear | q <question> | n <note> | notes | export | quit"
            )
        elif name == "j":
            self.move_paragraph(1)
        elif name == "k":
            self.move_paragraph(-1)
        elif name == "]":
            self.move_page(1)
        elif name == "[":
            self.move_page(-1)
        elif name == "g":
            self.goto_page(rest)
        elif name == "/":
            self.search(rest)
        elif name == "e":
            self.ask_ai("Explain the selected text clearly.", kind="explain")
        elif name == "pick":
            self.pick_text(rest)
        elif name == "range":
            self.pick_range(rest)
        elif name == "clear":
            self.selection_override = None
            self.message = "Narrow selection cleared; selected text is the current paragraph."
        elif name == "q":
            if not rest.strip():
                self.message = "Usage: q <question>"
            else:
                self.ask_ai(rest.strip(), kind="qa")
        elif name == "n":
            if not rest.strip():
                self.message = "Usage: n <note>"
            else:
                self.save_note(rest.strip())
        elif name == "notes":
            self.message = self.format_recent_records(limit=12)
        elif name == "export":
            exported = export_markdown(self.doc, self.store)
            self.message = f"Exported notes to {exported}"
        else:
            self.message = f"Unknown command: {name}. Type help."
        return True

    def move_paragraph(self, delta: int) -> None:
        if not self.page.paragraphs:
            self.message = "No paragraphs on this page."
            return
        self.paragraph_index = min(
            max(0, self.paragraph_index + delta), len(self.page.paragraphs) - 1
        )
        self.selection_override = None
        self.message = "Selection moved."

    def move_page(self, delta: int) -> None:
        self.page_index = min(max(0, self.page_index + delta), len(self.doc.pages) - 1)
        self.paragraph_index = 0
        self.selection_override = None
        self.message = "Page changed."

    def goto_page(self, raw: str) -> None:
        try:
            page = int(raw.strip())
        except ValueError:
            self.message = "Usage: g <page-number>"
            return
        self.page_index = min(max(0, page - 1), len(self.doc.pages) - 1)
        self.paragraph_index = 0
        self.selection_override = None
        self.message = f"Jumped to page {self.page.page_number}."

    def search(self, query: str) -> None:
        query = query.strip().lower()
        if not query:
            self.message = "Usage: / <text>"
            return
        for pidx in range(self.page_index, len(self.doc.pages)):
            start_para = self.paragraph_index + 1 if pidx == self.page_index else 0
            for para_idx, paragraph in enumerate(self.doc.pages[pidx].paragraphs[start_para:], start_para):
                if query in paragraph.lower():
                    self.page_index = pidx
                    self.paragraph_index = para_idx
                    match_pos = paragraph.lower().find(query)
                    self.selection_override = paragraph[match_pos : match_pos + len(query)]
                    self.message = f"Found on page {self.page.page_number}, paragraph {para_idx + 1}."
                    return
        self.message = f"No later match for: {query}"

    def pick_text(self, text: str) -> None:
        needle = text.strip()
        if not needle:
            self.message = "Usage: pick <text-in-current-paragraph>"
            return
        paragraph = self.page.paragraphs[self.paragraph_index] if self.page.paragraphs else ""
        pos = paragraph.lower().find(needle.lower())
        if pos < 0:
            self.message = f"Text not found in current paragraph: {needle}"
            return
        self.selection_override = paragraph[pos : pos + len(needle)]
        self.message = "Narrow selection set."

    def pick_range(self, raw: str) -> None:
        parts = raw.split()
        if len(parts) != 2:
            self.message = "Usage: range <start-char> <end-char>"
            return
        try:
            start, end = int(parts[0]), int(parts[1])
        except ValueError:
            self.message = "range expects integer character offsets."
            return
        paragraph = self.page.paragraphs[self.paragraph_index] if self.page.paragraphs else ""
        start = max(0, min(start, len(paragraph)))
        end = max(start, min(end, len(paragraph)))
        self.selection_override = paragraph[start:end]
        self.message = f"Narrow selection set to chars {start}:{end}."

    def save_note(self, note: str) -> None:
        record_id = self.store.add_record(
            pdf_id=self.doc.id,
            page=self.page.page_number,
            paragraph_index=self.paragraph_index,
            selected_text=self.selected_text,
            surrounding_text=self.surrounding_text(),
            kind="note",
            note=note,
        )
        self.message = f"Saved note #{record_id}."

    def ask_ai(self, question: str, kind: str) -> None:
        if not self.selected_text:
            self.message = "No selected text to ask about."
            return
        prompt = build_ai_prompt(
            title=self.doc.title,
            page=self.page.page_number,
            selected_text=self.selected_text,
            surrounding_text=self.surrounding_text(),
            question=question,
        )
        answer = run_ai_command(prompt)
        record_id = self.store.add_record(
            pdf_id=self.doc.id,
            page=self.page.page_number,
            paragraph_index=self.paragraph_index,
            selected_text=self.selected_text,
            surrounding_text=self.surrounding_text(),
            kind=kind,
            question=question,
            answer=answer,
        )
        self.message = f"Saved {kind} #{record_id}: {answer[:300]}"

    def format_recent_records(self, limit: int) -> str:
        records = self.store.recent_records(self.doc.id, limit=limit)
        if not records:
            return "No records yet."
        lines: list[str] = []
        for row in records:
            content = row["question"] or row["note"] or ""
            lines.append(f"#{row['id']} p{row['page']} {row['kind']}: {content}")
        return "\n".join(lines)


def build_ai_prompt(
    title: str,
    page: int,
    selected_text: str,
    surrounding_text: str,
    question: str,
) -> str:
    return f"""You are helping read and annotate a PDF.

PDF: {title}
Page: {page}

Selected text:
{selected_text}

Nearby context:
{surrounding_text}

User request:
{question}

Answer in concise Chinese. Explain terms and reasoning directly. If the selected text is ambiguous, say what extra context is needed.
"""


def run_ai_command(prompt: str) -> str:
    command = os.environ.get("PDFNOTE_AI_CMD")
    if not command:
        return (
            "[offline draft] AI is not configured. Set PDFNOTE_AI_CMD to enable real answers. "
            "This record still saved the selected text, context, and question."
        )
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            shell=True,
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:
        return f"[ai command failed] {exc}"
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        return f"[ai command exited {completed.returncode}] {stderr[:1000]}"
    return completed.stdout.strip() or "[ai command returned empty output]"


def export_markdown(doc: PdfDoc, store: Store) -> Path:
    safe_title = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in doc.title)[:80]
    out = EXPORT_DIR / f"{safe_title or 'pdf'}-{doc.id}.md"
    records = store.all_records(doc.id)
    lines = [
        f"# {doc.title} 阅读笔记",
        "",
        f"- PDF: `{doc.path}`",
        f"- Exported: {utc_now()}",
        "",
    ]
    current_page = None
    for row in records:
        if row["page"] != current_page:
            current_page = row["page"]
            lines.extend(["", f"## Page {current_page}", ""])
        lines.extend(
            [
                f"### #{row['id']} {row['kind']}",
                "",
                "> " + row["selected_text"].replace("\n", "\n> "),
                "",
            ]
        )
        if row["question"]:
            lines.extend([f"**Question:** {row['question']}", ""])
        if row["answer"]:
            lines.extend([row["answer"], ""])
        if row["note"]:
            lines.extend([f"**Note:** {row['note']}", ""])
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def cmd_open(args: argparse.Namespace) -> int:
    store = Store()
    doc = load_pdf(Path(args.pdf), store)
    PdfNoteApp(doc, store).run()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = Store()
    doc = load_pdf(Path(args.pdf), store)
    out = export_markdown(doc, store)
    print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdfnote")
    sub = parser.add_subparsers(dest="command", required=True)

    open_parser = sub.add_parser("open", help="Open a PDF in the terminal note UI.")
    open_parser.add_argument("pdf")
    open_parser.set_defaults(func=cmd_open)

    export_parser = sub.add_parser("export", help="Export saved notes for a PDF.")
    export_parser.add_argument("pdf")
    export_parser.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())


