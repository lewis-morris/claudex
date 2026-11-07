"""A little personality: the banner, start-up lines, and what Claudex says while it thinks."""
import random

BANNER = r"""
   ╭───────╮╭───────╮
   │ ◉   ◉ ││ ◉   ◉ │   Claudex
   │   ◡   ││   ◠   │   two brains, one plan
   ╰───┬───╯╰───┬───╯
       ╰──┬─────╯
"""

HELLO = [
    "Claude does the pretty bits, Codex does the sums. I do the shouting.",
    "Two agents, one plan, zero arguments. Well. Few arguments.",
    "Ready when you are. The agents are pretending to be.",
    "I've told them to play nicely. No promises.",
    "Today's special: code checked by someone who didn't write it.",
    "Fresh plans, made to order.",
]

PLANNING = [
    "Drawing up the plan", "Deciding who's on tea duty", "Reading the room (and the repo)",
    "Splitting the work fairly-ish", "Consulting the org chart", "Sharpening pencils",
]

REVIEWING = [
    "Getting a second pair of eyes", "Marking the homework", "Checking their working",
    "Squinting at the diff",
]

AGREE = [
    "Both agents agree. Rare, but beautiful.", "No objections from the other side.",
    "Second opinion: thumbs up.",
]

BYE = ["Bye for now.", "Off you pop.", "The agents have gone for a lie down.", "Cheerio."]


def pick(lines):
    return random.choice(lines)
