# Windows Defender, SmartScreen, and antivirus false positives

Pengy is an **agent harness**: it hands an LLM a set of tools that run shell
commands and PowerShell, write and replace files, download URLs, and walk your
filesystem. Windows' built-in protection is built to be suspicious of exactly that
behaviour. When it decides to act, it rarely asks first — it **force-closes the
process** and **quarantines the executable**.

If a `pengy-web` server vanished mid-session, the desktop app stopped launching, or
a reinstall "works" for a few seconds and then the command is gone again, this page
is for you.

> **Reinstalling on its own will not fix it.** Defender deletes the same bytes
> again as soon as the installer writes them. Exclude the program folder
> **first**, then restore or reinstall.

This is not a code defect and it is not something Pengy can work around: the only
supported fix is a Windows security exclusion. It is also a **false positive** —
Pengy hides nothing, installs no background service, and talks only to the LLM
endpoint you configure. All three editions (Python Pengy,
PengyR, PengyCPP) present the same tool surface and therefore attract the same
verdict — the paths differ, the procedure does not.

> The Python edition is the interesting case for paths: there is no MSI and no
> `Programs\<name>` folder. What Defender quarantines is either the
> **launcher shim** (`pengy.exe`, `pengy-gui.exe`, …) or a file **inside the
> environment** that holds the package. Both live in per-user locations covered
> below.

---

## What the symptom looks like

| What you see | What actually happened |
|---|---|
| The GUI closed with no error, no traceback, no crash dialog | The process was terminated by Defender, not by Pengy |
| `pengy`, `pengy-gui` or `pengy-web` no longer exists / "not recognized as an internal or external command" | The launcher shim was quarantined |
| A pinned Start Menu or taskbar shortcut does nothing | Its target exe is gone, so Windows broke the shortcut |
| `uv tool install --force pengy` or a reinstall succeeds, then the command dies again | The freshly written executable was quarantined again |
| Pengy runs, then stops the moment it runs a command or edits files | Behaviour monitoring (not a file signature) blocked it |
| Nothing in Pengy's own logs | It was killed from outside; there is no in-app record to find |

Nothing in your chats, settings or tasks is lost. They live in
`%APPDATA%\pengy\`, which no reinstall touches.

---

## Why it happens

- **The tool surface is the threat model.** `run_bash`, `run_powershell`,
  `run_python`, `write_file`, `apply_changes`, `download_file` and
  `directory_tree` compose into a program that spawns shells, rewrites files in
  bulk and reads the disk. That is a description of a dropper, and heuristics
  cannot read intent.
- **Unsigned, low-reputation executables.** A freshly built console shim in a
  per-user directory that then spawns PowerShell is the shape of commodity
  malware delivery. A `uv`-installed or venv-installed launcher has no publisher
  signature and no download history.
- **The verdicts are probabilistic.** Machine-learning and behaviour detections
  (names ending in `!ml`, or beginning `Behavior:`) are not deterministic; the same
  build can be clean on one machine and quarantined on another, and a rebuild with
  no behaviour change can flip the verdict. That is why one user is hit and the
  next is not — and why "it worked yesterday" is normal.
- **Notification may be off.** With notifications suppressed or the toast missed,
  the first evidence is an app that simply disappears.

---

## Step 1 — Confirm it was Defender

**Windows Security UI**

1. Open **Windows Security** → **Virus & threat protection**.
2. Click **Protection history**.
3. Look for a recent item naming `pengy.exe` / `pengy-gui.exe`, a folder under
   `%LOCALAPPDATA%\uv`, `%APPDATA%\Python`, or your virtualenv. Expand it — the
   detection name and the action taken (*Quarantined*, *Removed*, *Blocked*) are
   shown there.

**Event log** — *Event Viewer → Applications and Services Logs → Microsoft →
Windows → Windows Defender → Operational*. Detections are **1116**; the action
taken is **1117**.

**PowerShell**

```powershell
# Recent detections, newest first
Get-MpThreatDetection |
  Sort-Object InitialDetectionTime -Descending |
  Select-Object -First 10 InitialDetectionTime, ThreatID, Resources, ActionSuccess

# What each ThreatID was
Get-MpThreat | Select-Object ThreatID, ThreatName, SeverityID, Resources
```

**Find where your install actually is, then check it is still there**

```powershell
(Get-Command pengy).Source          # the launcher shim that runs
uv tool dir                         # uv-tool installs: ...\uv\tools  (pengy lives in ...\tools\pengy)

# venv or pip install: the package directory itself
python -c "import pengy, os; print(os.path.dirname(pengy.__file__))"
```

A `Scripts` folder (`%USERPROFILE%\.local\bin`, `<venv>\Scripts`, or
`%APPDATA%\Python\Python3XX\Scripts`) that is missing `pengy.exe` is the classic
picture.

If Protection history is empty **and** the executables are still there, this is
not Defender: look in *Event Viewer → Windows Logs → Application* for an
application error, and check any third-party antivirus you run (it has its own
console and its own exclusions list — the same steps apply there).

---

## Step 2 — Exclude the program locations (do this first)

Excluding **before** restoring stops Defender from eating the file again the moment
it reappears. Exclude both the shim folder and the environment folder — they are
different directories, and fixing one without the other leaves a half-working
install.

**Windows Security UI**

1. **Windows Security** → **Virus & threat protection** → **Manage settings**.
2. Scroll to **Exclusions** → **Add or remove exclusions** → **Add an exclusion**.
   (Windows will ask for elevation.)
3. Add **Folder** for each path that applies to your install:
   - `%USERPROFILE%\.local\bin` (the `pengy`, `pengy-gui` launchers)
   - `%APPDATA%\uv\tools\pengy` (a `uv tool install`)
   - `%APPDATA%\Python\Python3XX\Scripts` and
     `%APPDATA%\Python\Python3XX\site-packages\pengy` (a `pip --user` install)
   - `<your venv>\Scripts` and `<your venv>\Lib\site-packages\pengy` (a venv install)
4. Add **Process** entries for `pengy.exe`, `pengy-gui.exe`, `pengy-cli.exe` and
   `pengy-web.exe` as belt and braces.

**The same thing in PowerShell, from an elevated prompt**

```powershell
Add-MpPreference -ExclusionPath "$env:USERPROFILE\.local\bin"
Add-MpPreference -ExclusionPath "$env:APPDATA\uv\tools\pengy"
Add-MpPreference -ExclusionProcess "pengy.exe"
Add-MpPreference -ExclusionProcess "pengy-gui.exe"

# Verify
Get-MpPreference | Select-Object -ExpandProperty ExclusionPath
Get-MpPreference | Select-Object -ExpandProperty ExclusionProcess
```

To undo it later:

```powershell
Remove-MpPreference -ExclusionPath "$env:USERPROFILE\.local\bin"
Remove-MpPreference -ExclusionPath "$env:APPDATA\uv\tools\pengy"
Remove-MpPreference -ExclusionProcess "pengy.exe"
Remove-MpPreference -ExclusionProcess "pengy-gui.exe"
```

> **Exclude narrowly.** A folder exclusion is recursive and applies to everything
> inside it, so keep it to the folders listed above — never your profile
> (`C:\Users\<you>`), never `%APPDATA%` as a whole, never a drive root. That blinds
> Defender to everything else you do.
>
> If a detection named a **skill script** instead (something under
> `%USERPROFILE%\skills\`), a second exclusion limited to that one skill's folder
> is the narrow answer — but read that script first. Skills are code that Pengy
> runs on your behalf.

---

## Step 3 — Restore the files, or reinstall

With the exclusion in place, either restore what was quarantined or reinstall —
both work; restoring is faster.

**Restore from Protection history**

**Windows Security** → **Virus & threat protection** → **Protection history** →
expand the detection → **Actions** → **Restore**. If the card offers
**Allow on device**, take it — that also clears the standing verdict for that file.

**Restore from the command line** (elevated)

```powershell
& "$env:ProgramFiles\Windows Defender\MpCmdRun.exe" -Restore -Name "<ThreatName>" -All
```

Use the `ThreatName` reported by `Get-MpThreat` in Step 1 — not the ThreatID.
`-All` restores every quarantined copy of that threat; drop it to restore only the
most recent one. `MpCmdRun.exe` lives under
`C:\ProgramData\Microsoft\Windows Defender\Platform\<version>\` on some builds.

**Or reinstall**

```powershell
uv tool install --force "pengy[gui]"     # uv install
pip install --force-reinstall "pengy[gui]"   # pip / venv install
```

Then confirm the launcher is back where you expect it with
`(Get-Command pengy).Source`.

**Start Menu / taskbar shortcut still dead?** The Python edition creates menu
entries with `pengy --install-launcher`, which is Linux-only — on Windows you make
the shortcut yourself, and it points at `pengy-gui.exe` (the console-less launcher;
`pengy` keeps a console so `pengy --version` prints). After the file is back,
re-point or recreate the shortcut. If you run `pengy` from a VS Code task, a `.bat`
or a scheduled task, re-check that too — the old path may no longer resolve.

---

## Step 4 — If the download itself was blocked

A browser download carries Mark-of-the-Web, and that is a separate check from the
on-disk scan.

- **"Windows protected your PC"** → **More info** → **Run anyway**.
- **"This file came from another computer and might be blocked"** → right-click the
  file → **Properties** → tick **Unblock** → **OK**. Or:
  ```powershell
  Unblock-File .\some-pengy-skill.zip
  ```
- **Windows 11 Smart App Control** blocks unsigned, low-reputation executables
  outright. Check *Windows Security → App & browser control → Smart App Control*.
  Turning it off is a one-way change (it cannot be re-enabled without reinstalling
  Windows), so treat that as a last resort and prefer the exclusions above.

---

## Step 5 — If it comes back, or you are on a managed machine

- **Re-check the exclusions.** A Defender "restore defaults", a Windows feature
  update, or a third-party antivirus taking over as the primary engine can clear or
  bypass them. `Get-MpPreference` shows what is actually in force.
- **Every environment needs its own exclusion.** A `uv` tool install, a project
  venv and a `pip --user` install are three different folders; a fresh venv created
  next week is a fourth. If you install Pengy into many environments, either
  exclude the parent folder that holds them or accept that each new one is
  unscanned.
- **Work or school machine?** Exclusions are usually controlled by policy and the
  Security page will be greyed out. Ask IT to allow the executable — by install
  path or file hash — or to add it to the antivirus allow-list. Defender for
  Endpoint customers should submit it as an **indicator allow** in the portal.
- **Report the false positive.** Submitting the file to Microsoft
  (https://www.microsoft.com/en-us/wdsi/filesubmission, "Software developer" →
  false positive) is what eventually clears the verdict for everyone, and is worth
  doing even after your own machine is working.

---

## Reducing exposure instead of (or as well as) excluding

Pengy's tools are genuinely powerful, and an exclusion means Defender is no longer
watching that one folder. Two habits cost little and shrink the blast radius:

- **Keep tool confirmation at `All` or `Safe`**, not `None`, at least until you
  trust the model you are talking to. `Safe` auto-approves read-only tools and asks
  before anything that writes or executes.
- **Treat skills and prompts as code.** A skill is a markdown file plus optional
  scripts that the agent will run; a pasted "do this for me" instruction is the
  same thing by another route. Review before you install, and do not run a prompt
  from a source you would not run a script from.

For experiments you do not trust, use a VM or **Windows Sandbox** rather than
exclusions on your daily machine.

---

## What Pengy will never ask you to do

Pengy does not disable Defender, edit security policy, or install certificates. If
a message, page or agent claiming to be Pengy asks you to **turn off real-time
protection**, that is not us, and it is not the fix — exclusions are. The Python
edition also never needs administrator rights to install or run; it installs into
your user profile by design.
