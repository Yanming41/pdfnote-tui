# pdfnote-tui

Terminal-first PDF reading notes MVP.

## Features

- Extract PDF text by page with PyMuPDF.
- Browse pages and paragraphs in the terminal.
- Select a paragraph and ask/explain it.
- Save selections, questions, answers, and notes to SQLite.
- Export one PDF's reading record to Markdown.
- Optional AI integration through an external command.

## Usage

```powershell
cd C:\Users\13116\pdfnote-tui
python .\pdfnote.py open "C:\path\paper.pdf"
```

Inside the app:

- `j` / `k`: move paragraph selection
- `]` / `[`: next / previous page
- `g <page>`: jump to page
- `/ text`: search text
- `pick <text>`: narrow selection to a word or phrase in the current paragraph
- `range <start> <end>`: narrow selection by character offsets in the current paragraph
- `clear`: clear narrow selection and return to whole-paragraph selection
- `e`: explain selected text
- `q <question>`: ask about selected paragraph
- `n <note>`: save a manual note on selected paragraph
- `notes`: show recent saved records
- `export`: export Markdown notes for this PDF
- `help`: show commands
- `quit`: exit

## AI Hook

By default, `e` and `q` create a draft answer so the note flow works offline.

To connect an AI, set `PDFNOTE_AI_CMD` to a command that reads the prompt from stdin and writes the answer to stdout.

Example:

```powershell
$env:PDFNOTE_AI_CMD = "python C:\Users\13116\my-ai-wrapper.py"
python .\pdfnote.py open "paper.pdf"
```

The prompt includes the PDF title, page number, selected text, nearby context, and the user question.

## Data

Data is stored under:

```text
~\.pdfnote\
  pdfnote.sqlite3
  exports\
```

## Install

```powershell
python -m pip install pymupdf rich
```

## Repository Notes

This repository stores the terminal UI source only. Local reading data, exported notes, PDF files, logs, virtual environments, and SQLite databases are intentionally ignored.

