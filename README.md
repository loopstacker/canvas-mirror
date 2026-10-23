# canvas-mirror

A Python script that downloads your (Instructure) Canvas courses to your computer: every page and assignment as Markdown, all deadlines in one list, and every file Canvas links to, including Google Docs, Slides and Sheets. You get one folder per course that you can give to an AI tool like Claude Code as context.

Finding one detail on Canvas takes forever. The info is spread over modules, pages, assignments and Google Docs that link to even more Google Docs.

> [!WARNING]
> **Don't lean on it too hard.** With the whole course in one folder, an AI can do most of your assignments for you, and you'll show up at the exam knowing nothing. The script should save you the boring part (clicking through Canvas and Drive), not the learning.
>
> If a calculator does math for you, why do you learn addition?

## What you get

```
Programming/
├── CLAUDE.md                     instructions for Claude Code (unless you have them already)
├── _MIRRORING_PROBLEM.md         only there when the last run had a problem
└── Canvas_docs/
    ├── _overview.md              all deadlines and the module structure
    ├── Syllabus.md               if the course has one
    ├── _old/                     previous versions of files that changed
    ├── Announcements/            one file per announcement, starting with its date
    ├── Week 1 - Classes and Objects/
    │   ├── Assignment week 1.md
    │   └── Slides week 1.pdf
    └── ...
```

- Next to the modules you get the syllabus, all announcements, and the pages, assignments and discussions that aren't in any module (in folders like `Pages not in a module/`). So courses that don't use modules work too.
- Google Docs and Slides are saved as PDF, Sheets as XLSX. A linked Google Drive folder is downloaded with everything in it (this needs the Google login). Files linked inside documents are downloaded too (one level deep).
- A file is only downloaded again when Canvas or Google Drive says it changed, and only rewritten when its content changed. The old version moves to `_old/`, so you can see what the teacher changed. Nothing gets deleted.

## Setup

You need Python 3.10 or newer. There's nothing to install.

### 1. Canvas token

In Canvas, go to Account > Settings > New access token. Save the token as the environment variable `CANVAS_TOKEN`:

- Windows: `setx CANVAS_TOKEN "your-token"`, then open a new terminal
- macOS/Linux: add `export CANVAS_TOKEN="your-token"` to `~/.bashrc` or `~/.zshrc`

WARNING: The token can do anything you can do on Canvas, so do not publish it.

### 2. Config

Copy `config.example.json` to `~/.canvas_mirror/config.json` (on Windows that's `C:\Users\<you>\.canvas_mirror\config.json`) and fill it in:

```json
{
  "canvas_url": "https://canvas.yourschool.edu",
  "courses_folder": "C:/Users/you/University",
  "courses": {
    "Programming": 12345,
    "Databases": 67890
  }
}
```

- `courses_folder` is where the course folders go. Use `/` instead of `\` on all OS-es.
- `courses` maps a folder name to a Canvas course id. Run `python canvas_mirror.py --list-courses` to see your ids (it's also the number in the course URL).
- If you don't want the CLAUDE.md files, add `"create_claude_md": false`.

### 3. Google login (optional)

Most course documents can only be opened with your school's Google account. Without this step you only get the public ones, and the script lists the rest with their link.

1. Log in to [console.cloud.google.com](https://console.cloud.google.com/) with your school account and create a project.
2. APIs & Services > Library > Google Drive API > Enable.
3. Google Auth platform > Branding > Get started: fill in an app name and your e-mail, and choose audience "Internal".
4. Data Access > Add or remove scopes > `.../auth/drive.readonly`.
5. Clients > Create client > Desktop app. Download the JSON right away (Google shows the secret only once) and save it as `~/.canvas_mirror/google_client_secret.json`.
6. Run the script and log in with your school account in the browser that opens.

If you get "Access blocked by your organization", your school doesn't allow this, so use `--no-google`.

## Usage

```
python canvas_mirror.py                           mirror all courses
python canvas_mirror.py --course "Programming"    only this course (repeat for more)
python canvas_mirror.py --dry-run                 show what would change, write nothing
python canvas_mirror.py --no-google               skip the Google login
python canvas_mirror.py --list-courses            show your Canvas courses and their ids
```

## Run it every day

Run it once by hand first, so the Google login is saved. Then schedule it with `--scheduled`: the output goes to `~/.canvas_mirror/mirror.log` and it never opens a browser.

Nobody reads that log, so when a run has a real problem (Canvas refuses your token, the Google login is gone, a course can't be read), it also writes `_MIRRORING_PROBLEM.md` into the course folders. You see it next to `CLAUDE.md`, and Claude is told to mention it. The next run without problems deletes it. Single files your account can't open don't count as a problem; they're only listed in the output.

**Windows**, in PowerShell (`pythonw` runs it without a window):

```powershell
$action = New-ScheduledTaskAction -Execute (Get-Command pythonw).Source -Argument '"C:\path\to\canvas_mirror.py" --scheduled'
$trigger = New-ScheduledTaskTrigger -Daily -At 12:00
Register-ScheduledTask -TaskName "Canvas mirror" -Action $action -Trigger $trigger -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

If your PC is off at 12:00, it runs as soon as it's back on.

**macOS/Linux**: run `crontab -e` and add these lines. Cron doesn't read your shell profile, so the token goes here too. `which python3` shows your Python path.

```
CANVAS_TOKEN=your-token
0 12 * * * /usr/bin/python3 /path/to/canvas_mirror.py --scheduled
```

## CLAUDE.md

Claude Code reads the `CLAUDE.md` in the folder you work in. The script adds this file to each course folder that doesn't have one yet, and never touches it after that. It tells Claude where the course material is, to follow the course's rules on AI use (or act as a tutor if there are none), and to keep short notes about the course for next time. Edit `CLAUDE.template.md` to change what new course folders get.
