"""Settings dialog for Pengy — tabbed UI / LLM / Tools / Extras."""
import json
import threading
import urllib.request

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QTextEdit, QDialogButtonBox, QCheckBox, QLabel, QComboBox,
    QSpinBox, QPushButton, QHBoxLayout, QMessageBox, QTabWidget, QWidget,
    QFileDialog,
)
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from datetime import datetime

from pengy.core import skills_install
from pengy.core.about import (
    CATBEE_BLURB, CATBEE_URL, DESCRIPTION, GITHUB_URL, LICENSE_NAME, LICENSE_URL,
    WEBSITE_URL, copyright_line, edition_line,
)
from pengy.core.config import DEFAULTS, compaction_policy, set_compaction_policy
from pengy.core.model_cache import cached_models_for, load_model_cache, save_model_cache
from pengy.ui.theme import ACCENT_NAMES, THEME_MODES, get_theme, scaled_size
from pengy.ui.icons import apply_button_icon

#: Public skill registry, documented by the bundled botskills skill.
SKILL_REGISTRY_URL = "https://skills.catbee.ca"


def _label(text: str, tooltip: str) -> QLabel:
    """Create a QLabel with a hover tooltip."""
    lbl = QLabel(text)
    lbl.setToolTip(tooltip)
    return lbl


class SettingsDialog(QDialog):
    """Settings dialog with four tabs: UI, LLM, Tools and Extras."""

    _models_fetched = Signal(list)
    _models_fetch_failed = Signal(str)
    #: Asks the main window to send a prompt (e.g. start the profile interview).
    prompt_requested = Signal(str)

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._models_fetched.connect(self._on_models_fetched)
        self._models_fetch_failed.connect(self._on_models_fetch_failed)
        self.config = config
        self._theme = get_theme(config)
        self.setWindowTitle("Settings")
        self.setModal(True)

        layout = QVBoxLayout(self)

        # ── tabs ──────────────────────────────────────────────────
        tabs = QTabWidget()

        # ── UI tab ────────────────────────────────────────────────
        ui_tab = QWidget()
        ui_layout = QFormLayout(ui_tab)
        ui_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.scale_combo = QComboBox()
        self._scale_values = [75, 100, 110, 125, 135, 150, 175, 200]
        for v in self._scale_values:
            self.scale_combo.addItem(f"{v}%", v)
        current_scale = config.get("ui_scale", 100)
        idx = self._scale_values.index(current_scale) if current_scale in self._scale_values else 1
        self.scale_combo.setCurrentIndex(idx)
        self.scale_combo.setToolTip("Scales the entire UI. A restart is needed for the change to take full effect.")
        ui_layout.addRow(_label("UI Scale:", "Scales the entire UI. Restart Pengy to apply a change."), self.scale_combo)
        scale_note = QLabel("UI scale changes take effect after restarting Pengy.")
        scale_note.setWordWrap(True)
        scale_note.setStyleSheet(f"color: {self._theme['muted']};")
        ui_layout.addRow("", scale_note)

        self.theme_mode_combo = QComboBox()
        theme_mode_labels = {"system": "System", "light": "Light", "dark": "Dark"}
        for mode in THEME_MODES:
            self.theme_mode_combo.addItem(theme_mode_labels.get(mode, mode.title()), mode)
        current_theme_mode = config.get("theme_mode", "system")
        for i in range(self.theme_mode_combo.count()):
            if self.theme_mode_combo.itemData(i) == current_theme_mode:
                self.theme_mode_combo.setCurrentIndex(i)
                break
        self.theme_mode_combo.setToolTip("System follows your OS theme; Light and Dark override it.")
        ui_layout.addRow(_label("Theme mode:", "System follows your OS theme; Light and Dark override it."), self.theme_mode_combo)

        self.theme_accent_combo = QComboBox()
        for accent in ACCENT_NAMES:
            self.theme_accent_combo.addItem(accent.title(), accent)
        current_accent = config.get("theme_accent", "default")
        for i in range(self.theme_accent_combo.count()):
            if self.theme_accent_combo.itemData(i) == current_accent:
                self.theme_accent_combo.setCurrentIndex(i)
                break
        self.theme_accent_combo.setToolTip("Highlight color for buttons, links, and selection highlights.")
        ui_layout.addRow(_label("Accent color:", "Highlight color for buttons, links, and selection highlights."), self.theme_accent_combo)

        tabs.addTab(ui_tab, "UI")

        # ── LLM tab ───────────────────────────────────────────────
        llm_tab = QWidget()
        llm_layout = QFormLayout(llm_tab)
        llm_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.base_url_input = QLineEdit(config.get("base_url", DEFAULTS["base_url"]))
        _base_url_tip = ("OpenAI-compatible API endpoint. The default is a local Ollama "
                         "server, which needs no API key — e.g. http://127.0.0.1:11434/v1 "
                         "(Ollama) or http://127.0.0.1:8080/v1 (llama.cpp).")
        self.base_url_input.setToolTip(_base_url_tip)
        llm_layout.addRow(_label("Base URL:", _base_url_tip), self.base_url_input)

        self.api_key_input = QLineEdit(config.get("api_key", ""))
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setToolTip("Bearer token sent in the Authorization header to the LLM provider.")
        llm_layout.addRow(_label("API Key:", "Bearer token sent in the Authorization header to the LLM provider."), self.api_key_input)

        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        current_model = config.get("model", DEFAULTS["model"])
        # Pre-populate from the persistent model cache so the dropdown
        # survives between sessions.  The list may be stale, but populated.
        cached = cached_models_for(self.base_url_input.text())
        self.model_cache_note = QLabel("")
        self.model_cache_note.setStyleSheet(f"color: {self._theme['muted']};")
        self.model_cache_note.setWordWrap(True)
        if current_model:
            self.model_combo.addItem(current_model)
            self.model_combo.setCurrentText(current_model)
        if cached:
            items = ([current_model] if current_model else []) + [m for m in cached if m != current_model]
            self.model_combo.clear()
            self.model_combo.addItems(items)
            self.model_combo.setCurrentText(current_model or items[0])
            self._update_cache_note(load_model_cache())
        elif not current_model:
            # The default model is deliberately empty (a local endpoint ships no
            # model of its own), so on a fresh install there is nothing to
            # pre-select.  Say what to do rather than showing an empty box.
            self.model_cache_note.setText(
                f"No model selected yet \u2014 press Fetch to list what {self.base_url_input.text()} offers.")
        self.model_combo.setToolTip("Model name sent in chat completion requests. Use Fetch to list available models from the endpoint.")
        model_row.addWidget(self.model_combo, 1)

        self.fetch_models_btn = QPushButton("Fetch")
        apply_button_icon(self.fetch_models_btn, "refresh", self._theme, size=15)
        self.fetch_models_btn.setToolTip("Fetch available models from the /models endpoint and update the saved list")
        self.fetch_models_btn.setFixedWidth(80)
        self.fetch_models_btn.clicked.connect(self._fetch_models)
        model_row.addWidget(self.fetch_models_btn)

        llm_layout.addRow(_label("Model:", "Model name sent in chat completion requests. Use Fetch to list available models from the endpoint."), model_row)
        llm_layout.addRow("", self.model_cache_note)

        self.system_message_input = QTextEdit(config.get("system_message", "You are a helpful assistant."))
        self.system_message_input.setMaximumHeight(scaled_size(100, self._theme))
        self.system_message_input.setToolTip("The system prompt that sets the assistant's behavior, tone, and constraints.")
        llm_layout.addRow(_label("System Message:", "The system prompt that sets the assistant's behavior, tone, and constraints."), self.system_message_input)

        self.reasoning_effort_combo = QComboBox()
        reasoning_options = [
            ("Provider default — do not send reasoning option", ""),
            ("Off / none", "none"),
            ("Low", "low"),
            ("Medium", "medium"),
            ("High", "high"),
            ("Extra high", "xhigh"),
            ("Max", "max"),
        ]
        for lbl, value in reasoning_options:
            self.reasoning_effort_combo.addItem(lbl, value)
        current_effort = config.get("reasoning_effort", "")
        for i in range(self.reasoning_effort_combo.count()):
            if self.reasoning_effort_combo.itemData(i) == current_effort:
                self.reasoning_effort_combo.setCurrentIndex(i)
                break
        self.reasoning_effort_combo.setToolTip("Optional best-effort reasoning depth hint. Only supported by some models/providers; others may reject unknown values.")
        llm_layout.addRow(_label("Reasoning effort:", "Optional best-effort reasoning depth hint. Only supported by some models/providers."), self.reasoning_effort_combo)

        self.preserve_reasoning_checkbox = QCheckBox("Keep reasoning fields in conversation history")
        self.preserve_reasoning_checkbox.setChecked(bool(config.get("preserve_reasoning", False)))
        self.preserve_reasoning_checkbox.setToolTip("When checked, reasoning_content / reasoning / reasoning_details fields returned by the provider are kept. Leave off if your proxy rejects unknown message fields.")
        llm_layout.addRow(_label("Preserve reasoning:", "When checked, reasoning_content / reasoning / reasoning_details fields returned by the provider are kept."), self.preserve_reasoning_checkbox)

        self.llm_timeout_spinbox = QSpinBox()
        self.llm_timeout_spinbox.setRange(1, 3600)
        self.llm_timeout_spinbox.setSuffix(" sec")
        self.llm_timeout_spinbox.setToolTip("HTTP timeout in seconds for each LLM API request. Increase if your model is slow to respond.")
        self.llm_timeout_spinbox.setValue(config.get("llm_timeout", 300))
        llm_layout.addRow(_label("LLM timeout:", "HTTP timeout in seconds for each LLM API request. Increase if your model is slow to respond."), self.llm_timeout_spinbox)

        self.compaction_mode = QComboBox()
        self.compaction_mode.setObjectName("compactionMode")
        for label, value in (("Off", "off"), ("Auto-discover", "auto"), ("Explicit window", "explicit")):
            self.compaction_mode.addItem(label, value)
        self.compaction_window = QSpinBox()
        self.compaction_window.setObjectName("compactionWindow")
        self.compaction_window.setRange(1, 2147483647)
        self.compaction_window.setSuffix(" tokens")
        self.compaction_percent = QSpinBox()
        self.compaction_percent.setObjectName("compactionPercent")
        self.compaction_percent.setRange(1, 100)
        self.compaction_percent.setSuffix(" %")
        self.compaction_percent.setValue(config.get("compact_at_percent", 75))
        llm_layout.addRow("Proactive compaction:", self.compaction_mode)
        llm_layout.addRow("Selected-model window:", self.compaction_window)
        llm_layout.addRow("Threshold (all models):", self.compaction_percent)
        note = QLabel("Applies to the selected model. Off keeps reactive recovery and existing checkpoints. Auto needs endpoint metadata; unknown windows stay reactive. Threshold is shared across models.")
        note.setWordWrap(True)
        llm_layout.addRow(note)
        self.compaction_window.setToolTip("Combined input/output window. Typical: 131072, 262144, or 1048576. Used only in Explicit mode.")
        self._compaction_drafts = {}
        self._compaction_model = ""
        self._compaction_dirty = False
        self.compaction_mode.currentIndexChanged.connect(self._compaction_changed)
        self.compaction_window.valueChanged.connect(self._compaction_changed)
        self.model_combo.currentTextChanged.connect(self._load_compaction)
        self._load_compaction(self.model_combo.currentText())

        tabs.addTab(llm_tab, "LLM")

        # ── Tools tab ─────────────────────────────────────────────
        tools_tab = QWidget()
        tools_layout = QFormLayout(tools_tab)
        tools_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.confirm_combo = QComboBox()
        self.confirm_combo.addItem("YOLO (All) — execute everything, no questions asked", "all")
        self.confirm_combo.addItem("Safe Only — auto-approve read-only tools; confirm write/execute", "safe")
        self.confirm_combo.addItem("None — confirm every tool before execution", "none")
        current_confirm = config.get("tool_confirmation", "none")
        for i in range(self.confirm_combo.count()):
            if self.confirm_combo.itemData(i) == current_confirm:
                self.confirm_combo.setCurrentIndex(i)
                break
        self.confirm_combo.setToolTip("YOLO runs everything without asking. Safe only confirms write/execute tools. None confirms every tool call.")
        tools_layout.addRow(_label("Tool Confirmation:", "YOLO runs everything without asking. Safe auto-approves read-only tools. None confirms every tool call."), self.confirm_combo)

        self.context_keep_turns_spinbox = QSpinBox()
        self.context_keep_turns_spinbox.setRange(0, 999)
        self.context_keep_turns_spinbox.setSpecialValueText("Keep all")
        self.context_keep_turns_spinbox.setSuffix(" turns")
        self.context_keep_turns_spinbox.setToolTip("Tool results older than N turns are elided to save context window. 0 = keep everything.")
        self.context_keep_turns_spinbox.setValue(config.get("context_keep_turns", 0))
        tools_layout.addRow(_label("Keep tool results:", "Tool results older than N turns are elided to save context window. 0 = keep everything."), self.context_keep_turns_spinbox)

        self.timeout_spinbox = QSpinBox()
        self.timeout_spinbox.setRange(-1, 3600)
        self.timeout_spinbox.setSpecialValueText("No timeout")
        self.timeout_spinbox.setSuffix(" sec")
        self.timeout_spinbox.setToolTip("Maximum wall-clock time a single tool invocation can run before being killed. -1 = no timeout.")
        self.timeout_spinbox.setValue(config.get("tool_timeout", 300))
        tools_layout.addRow(_label("Tool timeout:", "Maximum wall-clock time a single tool invocation can run before being killed. -1 = no timeout."), self.timeout_spinbox)

        self.tool_output_max_spinbox = QSpinBox()
        self.tool_output_max_spinbox.setRange(0, 500_000)
        self.tool_output_max_spinbox.setSpecialValueText("No limit")
        self.tool_output_max_spinbox.setSuffix(" chars")
        self.tool_output_max_spinbox.setToolTip("Tool output longer than this is snipped (head+tail) to avoid blowing up the context window. 0 = no limit.")
        self.tool_output_max_spinbox.setValue(config.get("tool_output_max_chars", 250000))
        tools_layout.addRow(_label("Max tool output:", "Tool output longer than this is snipped (head+tail) to avoid blowing up the context window. 0 = no limit."), self.tool_output_max_spinbox)

        self.download_max_spinbox = QSpinBox()
        self.download_max_spinbox.setRange(0, 1_000_000)
        self.download_max_spinbox.setSpecialValueText("No limit")
        self.download_max_spinbox.setSuffix(" MB")
        self.download_max_spinbox.setToolTip("Default maximum download size for download_file. 0 = no limit.")
        self.download_max_spinbox.setValue(config.get("download_max_mb", 100))
        tools_layout.addRow(_label("Max download:", "Default maximum download size for download_file. 0 = no limit."), self.download_max_spinbox)

        self.user_agent_input = QLineEdit(config.get("user_agent", "PengyAgent/1.0"))
        self.user_agent_input.setToolTip("HTTP User-Agent header sent with LLM API requests and any HTTP-based tool calls.")
        tools_layout.addRow(_label("User Agent:", "HTTP User-Agent header sent with LLM API requests and any HTTP-based tool calls."), self.user_agent_input)

        tabs.addTab(tools_tab, "Tools")

        # ── Extras tab ────────────────────────────────────────────
        # Skills are inert files until the system message points at the index,
        # so this tab does both jobs in one place: seed ~/skills, then add the
        # pointer sentence to the message. Nothing here writes outside the
        # user's home, and nothing happens until a button is pressed.
        extras_tab = QWidget()
        extras_layout = QVBoxLayout(extras_tab)
        extras_layout.setSpacing(6)

        skills_heading = QLabel("Skills")
        _heading_font = skills_heading.font()
        _heading_font.setBold(True)
        _heading_font.setPointSize(_heading_font.pointSize() + 1)
        skills_heading.setFont(_heading_font)
        extras_layout.addWidget(skills_heading)

        skills_blurb = QLabel(
            "Skills are folders of markdown instructions that teach Pengy what its "
            "built-in tools don't cover. Pengy reads the skill index at the start of "
            "a task, so the folder is only useful once the system message mentions it "
            "\u2014 installing fills in both."
        )
        skills_blurb.setWordWrap(True)
        skills_blurb.setStyleSheet(f"color: {self._theme['muted']};")
        extras_layout.addWidget(skills_blurb)

        self.skills_status_label = QLabel("")
        self.skills_status_label.setWordWrap(True)
        self.skills_status_label.setTextFormat(Qt.TextFormat.PlainText)
        extras_layout.addWidget(self.skills_status_label)

        self.skills_detail_label = QLabel("")
        self.skills_detail_label.setWordWrap(True)
        self.skills_detail_label.setTextFormat(Qt.TextFormat.PlainText)
        self.skills_detail_label.setStyleSheet(f"color: {self._theme['muted']};")
        extras_layout.addWidget(self.skills_detail_label)

        skills_path_row = QHBoxLayout()
        self.skills_path_input = QLineEdit(
            skills_install.display_path(skills_install.skills_root(config))
        )
        _skills_path_tip = (
            "Where skills live. Defaults to ~/skills. Point it at a synced or shared "
            "tree if you keep skills elsewhere \u2014 only this one folder is consulted."
        )
        self.skills_path_input.setToolTip(_skills_path_tip)
        self.skills_path_input.setPlaceholderText(skills_install.DEFAULT_SKILLS_DIR)
        self.skills_path_input.editingFinished.connect(lambda: self._refresh_skills_state())
        skills_path_row.addWidget(self.skills_path_input, 1)
        self.skills_browse_btn = QPushButton("Browse\u2026")
        self.skills_browse_btn.setToolTip("Choose an existing skills folder")
        self.skills_browse_btn.clicked.connect(lambda: self._browse_skills_folder())
        skills_path_row.addWidget(self.skills_browse_btn)
        extras_layout.addWidget(
            _label("Skills folder:", _skills_path_tip))
        extras_layout.addLayout(skills_path_row)

        skills_button_row = QHBoxLayout()
        self.install_skills_btn = QPushButton("Install Skills")
        self.install_skills_btn.setToolTip(
            "Copy the starter skills into the folder above and add the skills "
            "pointer to your system message. Existing files are never overwritten."
        )
        apply_button_icon(self.install_skills_btn, "save", self._theme, size=15)
        self.install_skills_btn.clicked.connect(lambda: self._install_skills())
        skills_button_row.addWidget(self.install_skills_btn)

        self.skills_pointer_btn = QPushButton("Add to System Message")
        self.skills_pointer_btn.setToolTip(
            "Skills already exist here \u2014 append the one-line pointer to the system "
            "message so Pengy actually reads the index."
        )
        self.skills_pointer_btn.clicked.connect(lambda: self._add_skills_pointer())
        skills_button_row.addWidget(self.skills_pointer_btn)

        self.skills_open_btn = QPushButton("Open Folder")
        self.skills_open_btn.setToolTip("Open the skills folder in your file manager")
        apply_button_icon(self.skills_open_btn, "file", self._theme, size=15)
        self.skills_open_btn.clicked.connect(lambda: self._open_skills_folder())
        skills_button_row.addWidget(self.skills_open_btn)
        skills_button_row.addStretch()
        extras_layout.addLayout(skills_button_row)

        # Row 2: what to do *next* with the folder that now exists.
        skills_next_row = QHBoxLayout()
        self.skills_profile_btn = QPushButton("Fill In My Profile")
        self.skills_profile_btn.setToolTip(
            "Sends one prompt: read the user_profile skill and interview me. "
            "Answers are written to user_profile/profile.md in the skills folder."
        )
        self.skills_profile_btn.clicked.connect(lambda: self._start_profile_interview())
        skills_next_row.addWidget(self.skills_profile_btn)

        self.skills_registry_btn = QPushButton("Browse BotSkills")
        self.skills_registry_btn.setToolTip(
            "Open skills.catbee.ca \u2014 the public skill registry \u2014 in your browser. "
            "The botskills skill documents how Pengy searches and installs from it."
        )
        self.skills_registry_btn.clicked.connect(lambda: self._open_skill_registry())
        skills_next_row.addWidget(self.skills_registry_btn)
        skills_next_row.addStretch()
        extras_layout.addLayout(skills_next_row)

        self.skills_pointer_check = QCheckBox("Add the skills pointer to my system message")
        self.skills_pointer_check.setChecked(True)
        self.skills_pointer_check.setToolTip(
            "When checked, installing also appends the pointer sentence shown below "
            "to the system message on the LLM tab. It is never added twice."
        )
        extras_layout.addWidget(self.skills_pointer_check)

        self.skills_pointer_preview = QLabel(
            skills_install.pointer(
                self._skills_root() / skills_install.INDEX_NAME))
        self.skills_pointer_preview.setWordWrap(True)
        self.skills_pointer_preview.setTextFormat(Qt.TextFormat.PlainText)
        self.skills_pointer_preview.setStyleSheet(
            f"color: {self._theme['muted']}; font-style: italic;")
        extras_layout.addWidget(self.skills_pointer_preview)

        extras_layout.addStretch()
        self._refresh_skills_state()

        tabs.addTab(extras_tab, "Extras")

        # ── About tab ─────────────────────────────────────────────
        about_tab = QWidget()
        about_layout = QVBoxLayout(about_tab)
        about_layout.setSpacing(8)

        version_label = QLabel(edition_line("Python"))
        version_font = version_label.font()
        version_font.setBold(True)
        version_font.setPointSize(version_font.pointSize() + 2)
        version_label.setFont(version_font)
        about_layout.addWidget(version_label)

        repo_label = QLabel(f'<a href="{GITHUB_URL}">{GITHUB_URL}</a>')
        repo_label.setOpenExternalLinks(True)
        about_layout.addWidget(repo_label)

        website_label = QLabel(f'<a href="{WEBSITE_URL}">{WEBSITE_URL}</a>')
        website_label.setOpenExternalLinks(True)
        about_layout.addWidget(website_label)

        description_label = QLabel(DESCRIPTION)
        description_label.setWordWrap(True)
        about_layout.addWidget(description_label)

        catbee_label = QLabel(f'{CATBEE_BLURB} <a href="{CATBEE_URL}">{CATBEE_URL}</a>')
        catbee_label.setWordWrap(True)
        catbee_label.setOpenExternalLinks(True)
        about_layout.addWidget(catbee_label)

        copyright_label = QLabel(
            f'{copyright_line()}<br><a href="{LICENSE_URL}">{LICENSE_NAME}</a>'
        )
        copyright_label.setOpenExternalLinks(True)
        about_layout.addWidget(copyright_label)

        about_layout.addStretch()
        tabs.addTab(about_tab, "About")

        layout.addWidget(tabs)
        layout.addStretch()

        # ── buttons ──────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.adjustSize()

    def _compaction_changed(self, *_):
        self._compaction_dirty = True
        self.compaction_window.setEnabled(bool(self._compaction_model.strip()) and self.compaction_mode.currentData() == "explicit")

    def _stash_compaction(self):
        if self._compaction_dirty and self._compaction_model.strip():
            self._compaction_drafts[self._compaction_model] = (self.compaction_mode.currentData(), self.compaction_window.value())
        self._compaction_dirty = False

    def _load_compaction(self, model):
        self._stash_compaction()
        self._compaction_model = model
        mode, window = self._compaction_drafts.get(model, compaction_policy(self.config, model))
        self.compaction_mode.blockSignals(True)
        self.compaction_window.blockSignals(True)
        self.compaction_mode.setCurrentIndex(self.compaction_mode.findData(mode))
        self.compaction_window.setValue(window or 262144)
        self.compaction_mode.blockSignals(False)
        self.compaction_window.blockSignals(False)
        self.compaction_mode.setEnabled(bool(model.strip()))
        self.compaction_window.setEnabled(bool(model.strip()) and mode == "explicit")
        self._compaction_dirty = False

    def _update_cache_note(self, cache: dict | None):
        """Show when the current model list was last fetched from the endpoint."""
        if not cache or not cache.get("fetched_at"):
            self.model_cache_note.setText("")
            return
        fetched = datetime.fromtimestamp(cache["fetched_at"]).strftime("%Y-%m-%d %H:%M")
        self.model_cache_note.setText(
            f"Model list last fetched {fetched}. Use Fetch to refresh."
        )

    # ── model fetch (unchanged logic) ─────────────────────────
    def _fetch_models(self):
        """Fetch available models from the endpoint's /v1/models (non-blocking)."""
        base_url = self.base_url_input.text().strip().rstrip("/")
        api_key = self.api_key_input.text()
        models_url = f"{base_url}/models"

        self.fetch_models_btn.setEnabled(False)
        self.fetch_models_btn.setText("...")

        def do_fetch():
            try:
                req = urllib.request.Request(models_url)
                req.add_header("Authorization", f"Bearer {api_key}")
                req.add_header("api-key", api_key)
                req.add_header("User-Agent", "PengyAgent/1.0")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                model_ids = sorted(
                    m.get("id", "") for m in data.get("data", [])
                    if m.get("id")
                )
                if model_ids:
                    save_model_cache(base_url, model_ids)
                self._models_fetched.emit(model_ids)
            except urllib.error.HTTPError as e:
                self._models_fetch_failed.emit(
                    f"HTTP {e.code} from {models_url}\n\nCheck your Base URL and API Key."
                )
            except Exception as e:
                self._models_fetch_failed.emit(str(e))

        threading.Thread(target=do_fetch, daemon=True).start()

    def _on_models_fetched(self, model_ids: list):
        self.fetch_models_btn.setEnabled(True)
        self.fetch_models_btn.setText("Fetch")
        if not model_ids:
            QMessageBox.information(self, "No Models", "The endpoint returned an empty model list.")
            return
        current = self.model_combo.currentText()
        self.model_combo.clear()
        self.model_combo.addItems(model_ids)
        if current in model_ids:
            self.model_combo.setCurrentText(current)
        elif model_ids:
            self.model_combo.setCurrentText(model_ids[0])
        self._update_cache_note(load_model_cache())

    def _on_models_fetch_failed(self, error: str):
        self.fetch_models_btn.setEnabled(True)
        self.fetch_models_btn.setText("Fetch")
        QMessageBox.warning(self, "Fetch Failed", error)

    # ── Extras tab: starter skills ────────────────────────────
    def _skills_root(self) -> Path:
        """The folder named in the path field (never the config default behind it)."""
        return skills_install.skills_root(
            {skills_install.SKILLS_DIR_KEY: self.skills_path_input.text().strip()})

    def _skills_config(self) -> dict:
        return {skills_install.SKILLS_DIR_KEY: self.skills_path_input.text().strip()}

    def _set_lit(self, button: QPushButton, lit: bool) -> None:
        """Accent-fill a button (the “this is the thing to press” state)."""
        icon_name = button.property("pengyIcon")
        if not lit:
            button.setStyleSheet("")
            if icon_name:
                apply_button_icon(button, icon_name, self._theme, size=15)
            return
        theme = self._theme
        button.setStyleSheet(
            f"QPushButton {{ background-color: {theme['primary']};"
            f" color: {theme['primary_fg']};"
            f" border: 1px solid {theme['primary']}; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {theme['primary_hover']};"
            f" border-color: {theme['primary_hover']}; }}"
            f"QPushButton:pressed {{ background-color: {theme['primary_hover']}; }}"
            f"QPushButton:disabled {{ background-color: {theme['panel']};"
            f" color: {theme['muted']}; border: 1px solid {theme['border_soft']};"
            " font-weight: normal; }"
        )
        if icon_name:
            # A dark icon on an accent fill disappears; invert it while lit.
            apply_button_icon(button, icon_name, theme, size=15,
                              color_role="primary_fg")

    def _refresh_skills_state(self, note: str = "") -> None:
        """Re-inspect the folder and decide which button should light up."""
        status = skills_install.scan(self._skills_config())
        shown = skills_install.display_path(status.path)
        self.skills_pointer_preview.setText(skills_install.pointer(status.index_path))
        message_has_pointer = skills_install.references_skills(
            self.system_message_input.toPlainText())

        if status.installed:
            self.skills_status_label.setText(f"✓ {status.summary()}")
        elif status.exists:
            self.skills_status_label.setText(
                f"⚠ {shown} exists but has no {skills_install.INDEX_NAME} — "
                "Pengy has nothing to read")
        else:
            self.skills_status_label.setText(f"⚠ No skills folder at {shown}")

        self.install_skills_btn.setEnabled(not status.installed)
        self.install_skills_btn.setText(
            "Skills Installed" if status.installed else "Install Skills")
        self._set_lit(self.install_skills_btn, not status.installed)

        needs_pointer = status.installed and not message_has_pointer
        self.skills_pointer_btn.setVisible(status.installed)
        self.skills_pointer_btn.setEnabled(needs_pointer)
        self._set_lit(self.skills_pointer_btn, needs_pointer)
        self.skills_open_btn.setEnabled(status.exists)
        needs_profile = skills_install.profile_needs_interview(status)
        self.skills_profile_btn.setEnabled("user_profile" in status.skills)
        # The tab always points at exactly one thing to press: install first,
        # then the interview, and nothing once both are done.
        self._set_lit(self.skills_profile_btn, needs_profile)
        self.skills_registry_btn.setEnabled(True)

        lines: list[str] = []
        if status.skills:
            lines.append("Skills found: " + ", ".join(status.skills))
        elif status.installed:
            lines.append("The index is there, but no skill folders were found.")
        if needs_profile:
            lines.append("Your profile is still a blank template — "
                         "press Fill In My Profile to answer a few questions.")
        if status.installed and message_has_pointer and not note:
            lines.append("Your system message already points at the skill index.")
        if note:
            lines.append(note)
        self.skills_detail_label.setText("\n".join(lines))

    def _browse_skills_folder(self) -> None:
        start = self._skills_root()
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose a skills folder",
            str(start if start.is_dir() else Path.home()))
        if chosen:
            self.skills_path_input.setText(chosen)
            self._refresh_skills_state()

    def _apply_pointer(self) -> bool:
        """Add the pointer sentence to the system message. True if it changed."""
        status = skills_install.scan(self._skills_config())
        if not status.installed:
            return False
        current = self.system_message_input.toPlainText()
        updated = skills_install.with_reference(current, status.index_path)
        if updated == current:
            return False
        self.system_message_input.setPlainText(updated)
        return True

    def _install_skills(self) -> None:
        config = self._skills_config()
        try:
            report = skills_install.install(config)
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(
                self, "Install Failed",
                f"Could not install the starter skills.\n\n{exc}")
            return
        status = skills_install.scan(config)
        note = (f"Installed {len(report.created)} file(s) into "
                f"{skills_install.display_path(status.path)}.")
        if report.skipped:
            note += f" Left {len(report.skipped)} existing file(s) untouched."
        if self.skills_pointer_check.isChecked():
            if self._apply_pointer():
                note += " System message updated — press OK to save it."
            elif status.installed:
                note += " System message already points at the index."
        self._refresh_skills_state(note)

    def _add_skills_pointer(self) -> bool:
        changed = self._apply_pointer()
        self._refresh_skills_state(
            "System message updated — press OK to save it." if changed
            else "The system message already points at the skill index.")
        return changed

    def _open_skills_folder(self) -> None:
        root = self._skills_root()
        if root.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _open_skill_registry(self) -> None:
        QDesktopServices.openUrl(QUrl(SKILL_REGISTRY_URL))

    def _start_profile_interview(self) -> None:
        """Ask for one prompt, then close Settings so the chat is usable.

        The prompt names the skill file rather than describing the task, which
        is the whole point of the system: the markdown is the contract.
        """
        root = skills_install.display_path(self._skills_root())
        prompt = (
            f"Read {root}/user_profile/user_profile_skill.md and interview me to "
            f"fill in {root}/user_profile/profile.md. Ask one question at a time, "
            "write the file as we go, and don't invent any answers."
        )
        self.prompt_requested.emit(prompt)
        self.accept()

    def get_config(self) -> dict:
        """Return updated configuration."""
        self._stash_compaction()
        for model, (mode, window) in self._compaction_drafts.items():
            set_compaction_policy(self.config, model, mode, window)
        self.config["compact_at_percent"] = self.compaction_percent.value()
        self.config["base_url"] = self.base_url_input.text()
        self.config["api_key"] = self.api_key_input.text()
        self.config["model"] = self.model_combo.currentText()
        self.config["system_message"] = self.system_message_input.toPlainText()
        self.config["tool_confirmation"] = self.confirm_combo.currentData()
        self.config["reasoning_effort"] = self.reasoning_effort_combo.currentData()
        self.config["preserve_reasoning"] = self.preserve_reasoning_checkbox.isChecked()
        self.config["context_keep_turns"] = self.context_keep_turns_spinbox.value()
        self.config["theme_mode"] = self.theme_mode_combo.currentData()
        self.config["theme_accent"] = self.theme_accent_combo.currentData()
        self.config["ui_scale"] = self.scale_combo.currentData()
        self.config["llm_timeout"] = self.llm_timeout_spinbox.value()
        self.config["tool_timeout"] = self.timeout_spinbox.value()
        self.config["tool_output_max_chars"] = self.tool_output_max_spinbox.value()
        self.config["download_max_mb"] = self.download_max_spinbox.value()
        self.config["user_agent"] = self.user_agent_input.text() or "PengyAgent/1.0"
        raw_skills_dir = self.skills_path_input.text().strip()
        # Keep the config clean: an explicit ~/skills is the default, so store ""
        # rather than pinning a value every future default change has to outvote.
        if raw_skills_dir in ("", skills_install.DEFAULT_SKILLS_DIR,
                              str(Path(skills_install.DEFAULT_SKILLS_DIR).expanduser())):
            raw_skills_dir = ""
        self.config[skills_install.SKILLS_DIR_KEY] = raw_skills_dir
        return self.config
