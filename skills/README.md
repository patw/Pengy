# 🐧 Pengy Skills

> *"A motorcycle, not a car. Lean, minimal, no safety features — for expert users who want to fully personalize."*

---

## Philosophy

Pengy is a **local-first AI agent** that lives on your machine and does what you tell it. It has 11 built-in tools (read files, run bash, search the web, etc.), but the real power comes from **skills** — your custom instructions and scripts that teach Pengy how to do things *you* specifically need.

Skills are not a plugin system. There's no SDK, no manifest file, no packaging, no versioning, no dependency resolver. Skills are **markdown files and scripts in a directory** that you point Pengy at. That's it.

This is intentional. Pengy is designed for people who are comfortable at the terminal, who write their own scripts, who want their AI agent to reflect *their* environment, not some hypothetical generic one.

The trade-off: skills don't "just work" out of the box. You configure them for your machine, your API keys, your file paths. The payoff: skills can do *anything* you can describe in a prompt and a script.

---

## How Skills Work

Every skill lives in a directory following this convention:

```
skills/
├── skill_index.md          # ← The table of contents Pengy reads
├── plot/                   # ← A skill directory
│   ├── plot_skill.md       # ← The instructions (required)
│   └── make_plot.py        # ← A helper script (optional)
├── tts/
│   ├── tts_skill.md
│   └── speak.py
└── weather/
    ├── weather_skill.md
    └── get_weather_by_location.py
```

### The Contract

1. **Pengy reads `skill_index.md`** before using certain tools (web search, bash, code, URL fetch). This is configured in the system message:

    ```
    WARNING: Always look at ~/skills/skill_index.md before running
    web search, bash, running code or fetch_url! Skills should be used over tools!
    ```

2. **Each skill has a `skillname/skillname_skill.md`** file. This markdown file tells Pengy:
   - What the skill does
   - How to invoke it (command-line syntax, arguments)
   - Any dependencies or configuration needed
   - What output to expect

3. **Pengy reads the `_skill.md` instructions**, then runs whatever script or command they describe. It does **not** read the script source code first — the markdown is the interface.

4. **Scripts are optional.** A skill can be nothing more than a markdown file that teaches Pengy how to do something using its existing tools (bash, Python, web search, etc.). The `tts` skill is a good example — the markdown documents how to use `spd-say` directly, and the Python wrapper is just a convenience.

### The Index

`skill_index.md` is a simple markdown table. Pengy uses it as a quick reference to know what skills exist and what they do:

```markdown
# Skills Index

skill structure: skillname/skillname_skill.md
Read instructions found in the _skill.md first and run whatever script
it says without reading the script.

| Dir | What |
|-----|------|
| plot/ | matplotlib charts (line, bar, scatter, pie, hist) → PNGs |
| tts/ | Text-to-speech on Ubuntu (spd-say, espeak-ng, piper, gtts-cli) |
| weather/ | Fetch weather from tomorrow.io |
| repo_mapping/ | Map repository boundaries, entry points, features, tests, and build systems |
| test_orchestrator/ | Run focused/full checks, investigate failures, classify regressions, and summarize results |
| user_profile/ | User profile — who you are, where you live, what you do |
| pengy_bio/ | Pengy's own bio — who I am, where I'm installed, how I work |
```

---

## Getting Started: The 4 Example Skills

The skills in this directory are the same ones `patw` uses on his machines. They're here as **examples** — working, tested, real-world skills that demonstrate different patterns:

| Skill | Pattern | Complexity |
|-------|---------|-----------|
| `pengy_bio/` | Pure markdown (no script) — teaches Pengy about itself | ★☆☆ |
| `tts/` | Markdown + thin bash/Python wrapper | ★☆☆ |
| `plot/` | Markdown + Python script with dependencies | ★★☆ |
| `weather/` | Markdown + Python script + external API key | ★★★ |

Let's walk through each one.

---

### 1. 🧬 `pengy_bio/` — Pengy's Self-Knowledge

**Pattern:** Pure markdown description, no scripts.

This skill tells Pengy who and what it is — its name, creator, repository, installed locations, available interfaces, and design philosophy. It's the closest thing Pengy has to a README for itself.

**Why this exists:** When you first set up Pengy, you can give it a system message that describes its personality and purpose. But that gets long. A `pengy_bio` skill keeps it in a separate file that Pengy can reference when asked "who are you?"

**Your takeaway:** This is the simplest possible skill. It's just information. You can make one for anything you want Pengy to know deeply about.

**`pengy_bio/pengy_bio_skill.md`** (abbreviated):
```markdown
# Pengy Bio — Who I Am 🐧

**Name:** Pengy 🐧
**Creator:** patw
**GitHub:** https://github.com/patw/Pengy
**PyPI:** pengy
**Language:** Python 3.10+
**GUI:** PySide6 (Qt6) — invoke with `pengy`
**CLI:** rich — invoke with `pengy-cli`

## My 11 Built-in Tools
read_file, write_file, replace_in_file, run_bash, run_python,
web_search, download_file, fetch_url, directory_tree,
search_content, read_multiple_files
...
```

---

### 2. 🔊 `tts/` — Text-to-Speech

**Pattern:** Markdown documenting a system command + optional wrapper script.

This skill teaches Pengy how to make your computer talk. On Ubuntu, `speech-dispatcher` and `spd-say` come pre-installed — no extra setup needed.

**`tts/tts_skill.md`:**
```markdown
# TTS Skill

Uses `spd-say` (speech-dispatcher, preinstalled on Ubuntu).

spd-say "text"
spd-say -r 20 -p 10 "faster, higher pitch"   # speed/pitch (-100..+100)
spd-say -t female1 "female voice"
echo "pipe" | spd-say -e
spd-say -L                                    # list voices
```

**Usage with Pengy:**
```
> Say "Hello, this is Pengy speaking"
→ *your computer speaks the text*
```

**The `speak.py` wrapper** is a thin convenience script:
```python
#!/usr/bin/env python3
import subprocess, sys
text = " ".join(sys.argv[1:])
if not text and not sys.stdin.isatty():
    text = sys.stdin.read().strip()
subprocess.run(["spd-say", text])
```

**Your takeaway:** Some skills are just reminders of commands you already have. The markdown tells Pengy the exact syntax so it doesn't have to guess or search the web.

---

### 3. 📊 `plot/` — Matplotlib Charts

**Pattern:** Markdown + standalone Python script with arguments.

This skill generates charts (line, bar, scatter, pie, histogram) as PNG files using matplotlib. It's a good example of a skill that takes structured input and produces a file.

**`plot/plot_skill.md`:**
```markdown
# Plot Skill

Generates matplotlib charts as PNGs in ~/Pictures.

python make_plot.py -t <type> -d <data> [options]

| Arg | Default | Desc |
|-----|---------|------|
| -t  | required | line, bar, scatter, pie, hist |
| -d  | required | JSON string or path to JSON file |
| --title | "" | Chart title |
| -o  | auto | Output filename (in ~/Pictures) |
| --dark | off | Dark theme |
```

**Data formats:**
- **Line/Bar:** `{"labels":["A","B"],"values":[12,19]}`
- **Scatter (multi):** `[{"label":"A","x":[1,2],"y":[3,4]},...]`
- **Hist:** `[1,2,2,3,3,3]`

**Dependencies:** `matplotlib` (`pip install matplotlib`)

**Usage with Pengy:**
```
> Plot a bar chart comparing Q1 sales of 45 and Q2 sales of 62
→ python make_plot.py -t bar -d '{"labels":["Q1","Q2"],"values":[45,62]}'
→ ~/Pictures/chart_bar_1712345678.png
```

**Your takeaway:** Skills can produce artifacts. The script handles the technical work; the markdown describes the interface so Pengy knows how to call it.

---

### 4. 🌤️ `weather/` — Weather Forecast (Advanced)

**Pattern:** Markdown + Python script + external API + secret management.

This skill fetches weather data from [Tomorrow.io](https://www.tomorrow.io). It's the most "real-world" skill because it requires an API key and handles rate limiting.

**`weather/weather_skill.md`:**
```markdown
# Weather Skill

Fetches weather from tomorrow.io.

python get_weather_by_location.py <lat> <lon> [--days N] [--timezone TZ]

| Arg | Default | Desc |
|-----|---------|------|
| lat | required | Decimal lat |
| lon | required | Decimal lon |
| --days | 1 | Forecast days (max 5) |
| --timezone | America/Toronto | IANA tz |

**API key:** Read from TOMORROW_IO_KEY env var or ~/.secrets file.
```

#### Setting Up the API Key

1. **Sign up** at [tomorrow.io](https://www.tomorrow.io) (free tier: 25 calls/hour, 500/day).
2. **Get your API key** from the dashboard.
3. **Store it** in `~/.secrets`:

   ```bash
   echo "TOMORROW_IO_KEY=your_api_key_here" >> ~/.secrets
   chmod 600 ~/.secrets
   ```

   The `get_weather_by_location.py` script reads `~/.secrets` automatically:

   ```python
   def _read_secrets():
       secrets = {}
       secret_file = Path.home() / ".secrets"
       if secret_file.exists():
           for line in secret_file.read_text().splitlines():
               if line and not line.startswith("#") and "=" in line:
                   k, v = line.split("=", 1)
                   secrets[k.strip()] = v.strip()
       return secrets
   ```

   You can also set it as an environment variable:
   ```bash
   export TOMORROW_IO_KEY=your_api_key_here
   ```

#### Rate Limiting

The script handles Tomorrow.io's rate limits automatically:
- **3 requests/second** (350ms gap between calls)
- **25 requests/hour**
- **500 requests/day**

It uses a file lock (`/tmp/tomorrow_io_rate.lock`) and a usage tracker (`/tmp/tomorrow_io_usage.json`) so limits are respected even if multiple Pengy sessions call it simultaneously.

#### Usage with Pengy

```
> What's the weather in Toronto right now?
→ python get_weather_by_location.py 43.653 -79.383
→ JSON with current temp, feels-like, wind, precipitation, etc.
```

**Your takeaway:** Real skills need secrets. The `~/.secrets` pattern keeps API keys out of scripts and out of version control. You can use this same pattern for any skill that needs an API key — weather, OpenAI, GitHub, whatever.

---

## Making Your Own Skills

### Step 1: Pick a problem

What do you want Pengy to do that its built-in tools don't cover?

- Fetch data from an API
- Run a system administration command
- Generate an image, report, or file
- Control a device on your network
- Query a local database
- Send notifications

### Step 2: Create the skill directory

```bash
mkdir -p ~/skills/my_skill
```

### Step 3: Write the instructions

Create `my_skill/my_skill_skill.md`:

```markdown
# My Skill

Does this specific thing. Here's how to invoke it:

bash my_script.sh <arg1> [options]

| Arg | Default | Desc |
|-----|---------|------|
| arg1 | required | The first argument |
| --flag | false | An optional flag |

**Dependencies:** None (or what to install)

**Example:** bash my_script.sh "hello"
```

### Step 4: Write the script (optional)

If your skill needs more than a one-liner, write a script:

```bash
#!/usr/bin/env bash
# my_skill/my_script.sh
echo "Hello, $1!"
```

Make it executable:
```bash
chmod +x ~/skills/my_skill/my_script.sh
```

### Step 5: Add it to the index

Edit `skill_index.md` and add a row:

```markdown
| my_skill/ | Does this specific thing |
```

### Step 6: Point Pengy at it

Your system message (in `~/.config/pengy/settings.json`) should already reference the skills directory. If not, add:

```json
"system_message": "You are Pengy... WARNING: Always look at ~/skills/skill_index.md before running web search, bash, running code or fetch_url! Skills should be used over tools!"
```

### Step 7: Test it

Ask Pengy to use your new skill in a chat. If something doesn't work, tweak the markdown instructions — the markdown is the contract between you and Pengy.

---

## Using Pengy to Make Skills

One of the most meta things you can do is ask Pengy to *create skills for you*. Since Pengy has `write_file`, `run_bash`, and `search_content` tools, it can:

1. **Ask you what you want** the skill to do
2. **Write the `_skill.md` file** with proper documentation
3. **Write the script** (bash, Python, etc.)
4. **Update `skill_index.md`** with the new entry
5. **Test the skill** by running it

Example:
```
> I want a skill that tells me my disk usage in a human-readable format.
> Can you create it as a skill and add it to my index?
```

Pengy would then create `disk_usage/disk_usage_skill.md`, write a one-liner using `df -h`, and add `| disk_usage/ | Show disk usage in human-readable format |` to the index.

**This is the power of the system.** You don't need to learn a plugin API. You interact with Pengy, and Pengy writes its own skills. You're always in the loop because the markdown files are plain text you can read, edit, and version control.

---

## Example: Full Skill Index

Here's a complete `skill_index.md` for reference:

```markdown
# Skills Index

skill structure: skillname/skillname_skill.md
Read instructions found in the _skill.md first and run whatever script
it says without reading the script.

| Dir | What |
|-----|------|
| pengy_bio/ | Pengy's own bio — who I am, where I'm installed, how I work |
| tts/ | Text-to-speech on Ubuntu (spd-say, espeak-ng, piper, gtts-cli) |
| plot/ | matplotlib charts (line, bar, scatter, pie, hist) → PNGs |
| weather/ | Fetch weather from tomorrow.io |
| user_profile/ | User profile — who you are, what you do, your setup |
```

---

## 🎯 Your First Skill: `user_profile/`

The best way to understand skills is to make one. Create a `user_profile` skill that describes **you** — the person using Pengy.

This is a **pure markdown** skill (no script needed). It's just information that Pengy reads to know who it's talking to.

### What to include

- **Your name/handle** — what should Pengy call you?
- **Your location** — timezone, city/country (useful for weather, time queries)
- **What you do** — job, hobbies, interests
- **Your tech setup** — OS, hardware, languages you use
- **Your preferences** — how you like things explained, how formal/casual, etc.
- **Your projects** — what you're working on that Pengy might help with

### Starter template

Create `~/skills/user_profile/user_profile_skill.md`:

```markdown
# Your Name — Profile

**Name:** Your Name | **Alias:** your_handle
**Location:** City, Country | **TZ:** Your/Timezone

## Work
What you do professionally.

## Tech
Your OS, languages, tools, comfort level.

## Interests
What you're into — gaming, motorcycles, programming, gardening, whatever.

## Preferences
How you like Pengy to talk to you. Verbose? Concise? Formal? Casual?
```

Then add it to your index:

```markdown
| user_profile/ | My personal profile — who I am and what I do |
```

Now Pengy knows who it's talking to and can tailor its responses accordingly.

---

## Tips & Patterns

### Keep scripts short
Scripts should do one thing well. If a script is getting long, it might be doing too much.

### Use the right language
- **Bash** is best for one-liners, pipes, system commands, file operations
- **Python** is best for API calls, JSON processing, data manipulation
- **Markdown only** is best for reference information and documentation

### Handle errors gracefully
Scripts should exit with non-zero on failure and print errors to stderr. Pengy sees stderr output.

### Prefer `~/.secrets` over environment variables
The `~/.secrets` pattern (key=value pairs, `chmod 600`) keeps API keys readable by scripts without polluting your environment or leaking into shell history.

### Skills can reference each other
The `_skill.md` can link to other skills using relative paths, just like the scheduler skill links to the pengy_bio skill.

### Version control your skills
Your skills directory is just files. Put it in git. Share individual skill directories with other Pengy users — the format is portable.

---

## ⚠️ Warning

Skills give Pengy the ability to run arbitrary commands and scripts on your machine. This is by design — Pengy is a tool for power users who want full control.

- **Review scripts before running.** Even if Pengy wrote them.
- **Trust your API keys.** `~/.secrets` is a text file. Protect it.
- **Skills are not sandboxed.** A skill can delete files, send emails, or start processes.
- **Use tool confirmation modes.** The `tool_confirmation` setting in `settings.json` lets you choose: approve everything (YOLO), auto-approve reads only (Safe), or confirm every call (None).

This is the motorcycle. No airbags. No lane assist. But it'll take you anywhere you want to go.

---

*Happy building. 🐧*
