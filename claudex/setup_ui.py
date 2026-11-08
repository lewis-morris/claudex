"""Project onboarding inside the existing terminal application."""
from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import Button, Checkbox, CheckboxList, Dialog, Label, RadioList, TextArea

from . import project_settings
from .engine import catalog_models, load_rules

MODE_LABELS = [("unrestricted", "Unrestricted — allow tools without asking"),
               ("auto", "Auto — rules decide; ask me about the rest"),
               ("ask", "Ask me — approve each tool request")]


class Setup:
    def __init__(self, tui):
        self.tui = tui
        e = tui.eng
        saved = project_settings.load(e.project_root)
        rules = load_rules(e.rules_path, e.project_root, e.models_file, model_preferences=False)
        choices = project_settings.model_choices(rules, catalog_models())
        self.description = TextArea(text=saved.get("description", ""), height=2, multiline=True,
                                    scrollbar=True, focus_on_click=True)
        self.prompt = TextArea(text=saved.get("base_prompt", ""), height=3, multiline=True,
                               scrollbar=True, focus_on_click=True)
        self.instructions = Checkbox(text="Create missing AGENTS.md and CLAUDE.md files", checked=False)
        self.mode = RadioList(MODE_LABELS, default=tui.pending_mode or e.mode)
        self.route = RadioList([("auto", "Both — let Claudex assign the work"),
                                ("claude", "Claude does the work"), ("codex", "Codex does the work")], default=e.route)
        self.checks = RadioList([("full", "Second opinions and reviews"), ("reviews", "Reviews only"),
                                 ("off", "No reviews")], default=e.checks)
        values = [((agent, model), f"{agent.title()}: {model}") for agent, models in choices.items() for model in models]
        enabled = saved.get("enabled_models", {})
        self.models = CheckboxList(values, default_values=[value for value, _ in values
                                  if value[1] in enabled.get(value[0], choices[value[0]])])
        self.models.window.height = 8
        self.error = Label(text="")
        self.page = 0

    def show(self, page=0):
        self.page = page
        self.error.text = ""
        if page == 0:
            fields = [Label("What is this project for?"), self.description,
                      Label("Base prompt — instructions to include with every job"), self.prompt,
                      self.instructions, Label("Existing instruction files are kept exactly as they are.")]
            focus = self.description
        elif page == 1:
            fields = [Label("Permissions for this project"), self.mode, Label("Who does the work?"), self.route,
                      Label("Checks (the other agent may still plan or review)"), self.checks]
            focus = self.mode
        else:
            fields = [Label("Models to use — Space ticks or unticks; arrows move"), self.models,
                      Label("Keep at least one model for each agent.\nChoices come from your local configuration; availability is checked when used."),
                      Label("Save applies these choices to this project only.\nYour brief and base prompt are included in future jobs.")]
            focus = self.models
        buttons = []
        if page:
            buttons.append(Button("Back", handler=lambda: self.show(page - 1)))
        buttons.append(Button("Save" if page == 2 else "Next", handler=self.save if page == 2 else lambda: self.show(page + 1)))
        buttons.append(Button("Later", handler=self.tui.close_setup))
        buttons.append(Button("Skip setup", handler=self.skip))
        self.tui.setup_dialog = Dialog(title=f"Project setup — {page + 1}/3 (Tab moves, Esc closes)",
                                      body=HSplit(fields + [self.error], padding=0), buttons=buttons,
                                      width=88, with_background=False)
        self.tui.app.layout.focus(focus)
        self.tui._refresh()

    def skip(self):
        try:
            project_settings.save(self.tui.eng.project_root, {"onboarded": True})
        except (OSError, ValueError) as exc:
            self.error.text = str(exc)
            return
        self.tui.close_setup()

    def save(self):
        enabled = {a: [m for agent, m in self.models.current_values if agent == a] for a in ("claude", "codex")}
        if any(not models for models in enabled.values()):
            self.error.text = "Choose at least one Claude model and one Codex model."
            return
        e = self.tui.eng
        values = {"description": self.description.text.strip(), "base_prompt": self.prompt.text.strip(),
                  "mode": self.mode.current_value, "route": self.route.current_value,
                  "checks": self.checks.current_value, "enabled_models": enabled, "onboarded": True}
        try:
            created = project_settings.create_instructions(e.project_root, values["description"], values["base_prompt"]) \
                if self.instructions.checked else []
            project_settings.save(e.project_root, values)
        except (OSError, ValueError) as exc:
            self.error.text = str(exc)
            return
        e.mode, e.route, e.checks = values["mode"], values["route"], values["checks"]
        self.tui.pending_mode = None
        e._rules = None
        self.tui.close_setup()
        self.tui._add("sys", "\n_Project setup saved. Use /setup to change it." +
                      (" Created " + ", ".join(created) + "." if created else "") + "_\n")
